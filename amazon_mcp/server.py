"""amazon.in MCP server — search, filters, pincode, product pages, reviews.

Thin tool layer over proven recon modules (amz_lib, search_parse, product_extract,
reviews_fetch, pincode_common). Single shared AmzSession with cookie-jar persistence
and polite rate limiting.

Run: python server.py   (stdio transport)
Self-check: python server.py --demo
"""
import sys, os, re, json, time, random, functools, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer
from amz_lib import AmzSession, polite
import search_parse as sp
import product_extract as pe
import reviews_fetch as rf
import pincode_common as pc

COOKIE_JAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.json")
mcp = MCPServer("amazon-in")

# ---------------------------------------------------------------- hardening helpers
_NET = threading.Lock()   # ponytail: global lock, per-session locks if throughput matters
_FIELD_CAP = 6000
_SORTS = {"relevanceblender", "price-asc-rank", "price-desc-rank",
          "review-rank", "date-desc-rank", "exact-aware-popularity-rank"}

def _err(error, **kw):
    return json.dumps({"error": error, **kw}, ensure_ascii=False, default=str)

def _clip(v, n=_FIELD_CAP):
    """Cap huge text fields so one monster page can't blow the MCP message size."""
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"...[clipped {len(v) - n} chars]"
    return v

def _clip_reviews(revs):
    for r in revs or []:
        r["body"] = _clip(r.get("body"))
    return revs

_ASIN_OK = re.compile(r"^[A-Za-z0-9]{10}$")
_ASIN_PAT = re.compile(r"(?:^|/)(?:dp|gp/product|product|gp/aw/d)/([A-Za-z0-9]{10})(?=[/?#]|$)", re.I)

def normalize_asin(raw):
    """Bare ASIN / ISBN-10 / amazon.in URL -> 10-char ASIN, else None. Query+fragment stripped."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if _ASIN_OK.match(s):
        return s.upper()
    m = _ASIN_PAT.search(s.split("#", 1)[0].split("?", 1)[0])
    return m.group(1).upper() if m else None

def _need_asin(raw):
    a = normalize_asin(raw)
    if a:
        return a, None
    return None, _err("INVALID_ASIN", input=str(raw)[:200],
                      hint="expected a 10-char ASIN (e.g. B0G2B2QVLL), ISBN-10, or an "
                           "amazon.in URL containing /dp/, /gp/product/ or /product/")

def _validate_query(query):
    if not isinstance(query, str) or not query.strip():
        return _err("INVALID_PARAM", param="query", detail="query must be a non-empty string")
    if len(query) > 200:
        return _err("INVALID_PARAM", param="query", detail=f"query too long ({len(query)} > 200 chars)")
    return None

def _validate_category(category):
    if not category:
        return None
    if not isinstance(category, str) or not re.fullmatch(r"[a-z0-9\-]{1,40}", category):
        return _err("INVALID_PARAM", param="category", detail="expected a search-alias like 'electronics'")
    return None

def _validate_page(page):
    if not isinstance(page, int) or not 1 <= page <= 500:
        return _err("INVALID_PARAM", param="page", detail="page must be an integer 1-500")
    return None

class ToolError(Exception):
    """Domain error with a stable code; _guarded renders it as {"error": <code>}."""
    def __init__(self, code, **kw):
        super().__init__(code)
        self.code, self.kw = code, kw

def _get(s, path, params=None):
    """GET + reject an empty response body before any parser sees it."""
    r = s.get(path, params)
    if not (r.text or "").strip():
        raise ToolError("EMPTY_RESPONSE", detail="network returned an empty body")
    return r

def _guarded(fn):
    """Serialize all network work on one global lock and turn ANY escape into JSON.
    MCP clients invoke tools concurrently; the shared AmzSession/cookie jar isn't thread-safe."""
    @functools.wraps(fn)
    def w(*a, **kw):
        try:
            with _NET:
                return fn(*a, **kw)
        except ToolError as e:
            return _err(e.code, tool=fn.__name__, **e.kw)
        except Exception as e:
            return _err("EXCEPTION", tool=fn.__name__, detail=f"{type(e).__name__}: {e}")
    return w

# ---------------------------------------------------------------- session singleton
_az = None
def az():
    global _az
    if _az is None:
        _az = AmzSession()
        if os.path.exists(COOKIE_JAR):
            try:
                _az.s.cookies.update(json.loads(open(COOKIE_JAR).read()))
            except Exception:
                pass
    return _az

def _save_jar():
    try:
        json.dump(dict(az().s.cookies.items()), open(COOKIE_JAR, "w"), indent=1)
    except Exception:
        pass

def _relaxed_polite():
    # ponytail: 1.5-3s jitter is enough; skip on first call
    if _az is not None:
        time.sleep(random.uniform(1.2, 2.2))

# ---------------------------------------------------------------- tools
@mcp.tool()
@_guarded
def search_products(query: str, page: int = 1, category: str = "",
                    min_price: int = 0, max_price: int = 0,
                    min_rating: float = 0.0, brands: list[str] | None = None,
                    sort: str = "", free_delivery: bool = False,
                    include_out_of_stock: bool = False,
                    country_of_origin: str = "", cod: bool = False,
                    condition: str = "", max_pages: int = 1) -> str:
    """Search amazon.in with full filter control. Returns products + total count + available filter tokens.
    category: search-alias (aps, electronics, computers, stripbooks, apparel, kitchen, hpc, beauty, appliances, baby, automotive, shoes, sporting, toys, videogames, watches, jewelry, luggage, office-products, pets, grocery, furniture, lawngarden, home-improvement, mi, software, mobile-apps, instant-video, digital-text, audible, todays-deals, gift-cards, fashion, alexa-skills, amazon-devices)
    sort: relevanceblender(Featured), price-asc-rank, price-desc-rank, review-rank(Avg. Customer Review), date-desc-rank(Newest Arrivals), exact-aware-popularity-rank(Best Sellers)
    min_price/max_price: rupees. min_rating: 4.0 for 4 Stars & Up. brands: use discover_brands() first.
    max_pages: fetch up to 7 pages starting at `page`, aggregated and deduped by ASIN.
    Per-tile extras: delivery_date (structured 'Fri, 23 Oct' | 'Tomorrow' | 'Today' parsed from delivery text) and bank_offer (offer/EMI line like 'Save extra with No Cost EMI'; plain 'Bank Offer' text does not render on amazon.in tiles).
    Returns: {"total": N, "products": [...], "available_filter_tokens": {...}}"""
    for bad in (_validate_query(query), _validate_page(page), _validate_category(category)):
        if bad:
            return bad
    if not isinstance(min_price, int) or not 0 <= min_price <= 10_000_000:
        return _err("INVALID_PARAM", param="min_price", detail="min_price must be 0-10000000 rupees")
    if not isinstance(max_price, int) or not 0 <= max_price <= 10_000_000:
        return _err("INVALID_PARAM", param="max_price", detail="max_price must be 0-10000000 rupees")
    if min_price and max_price and min_price > max_price:
        return _err("INVALID_PARAM", param="min_price", detail=f"min_price {min_price} > max_price {max_price}")
    if not isinstance(min_rating, (int, float)) or not 0 <= min_rating <= 5:
        return _err("INVALID_PARAM", param="min_rating", detail="min_rating must be 0-5")
    if sort and sort not in _SORTS:
        return _err("INVALID_PARAM", param="sort", detail=f"unknown sort {sort!r}", allowed=sorted(_SORTS))
    if not isinstance(max_pages, int) or max_pages < 1:
        return _err("INVALID_PARAM", param="max_pages", detail="max_pages must be an integer >= 1")
    if brands is not None and (not isinstance(brands, list)
                               or not all(isinstance(b, str) and b.strip() for b in brands)):
        return _err("INVALID_PARAM", param="brands", detail="brands must be a list of non-empty strings")
    max_pages = min(max_pages, 7)
    s = az()
    params = {"k": query.strip(), "page": str(page)}
    if category:  params["i"] = category
    if min_price: params["low-price"] = str(min_price)
    if max_price: params["high-price"] = str(max_price)
    if min_rating >= 4.0: params["rh"] = "p_72:1318476031"
    if free_delivery:     params["rh"] = (params.get("rh", "") + "," if "rh" in params else "") + "p_n_free_shipping_eligible:205563695031"
    if include_out_of_stock: params["rh"] = (params.get("rh", "") + "," if "rh" in params else "") + "p_n_availability:1318485031"
    if cod:               params["rh"] = (params.get("rh", "") + "," if "rh" in params else "") + "p_n_is_cod_eligible:4931671031"
    if condition == "New": params["rh"] = (params.get("rh", "") + "," if "rh" in params else "") + "p_n_condition-type:8609960031"
    if country_of_origin: params["rh"] = (params.get("rh", "") + "," if "rh" in params else "") + "p_country_of_origin-bin:" + country_of_origin
    if sort:              params["s"] = sort

    def fetch(pg):
        params["page"] = str(pg)
        _relaxed_polite()
        return _get(s, "/s", params)

    r = fetch(page)
    if r.status_code != 200:
        return _err(f"HTTP {r.status_code}", body_len=len(r.text))
    if sp.is_no_results(r.text):
        return json.dumps({"total": 0, "products": [], "note": "no results"})
    total, source = sp.parse_total(r.text)
    # brand token discovery -> token-map filter (only known after first fetch)
    brands_avail = sp.parse_filters(r.text).get("p_123", [])
    if brands:
        brand_ids = [b["token"].split(":", 1)[1] for b in brands_avail
                     if any(bn.lower() in b["label"].lower() for bn in brands)]
        if brand_ids:
            params["rh"] = (params.get("rh", "") + "," if "rh" in params else "") + "p_123:" + ",".join(brand_ids)
            r = fetch(page)
            if r.status_code == 200:
                total, source = sp.parse_total(r.text)
    seen: dict = {}
    def merge(text):
        for it in sp.parse_products(text):
            seen.setdefault(it["asin"], it)
    merge(r.text)
    first_count = len(seen)
    pages_fetched = 1
    for pg in range(page + 1, page + max_pages):
        r2 = fetch(pg)
        if r2.status_code != 200:
            break
        pages_fetched += 1
        before = len(seen)
        merge(r2.text)
        if len(seen) == before:
            break   # amazon served the same tiles again — stop instead of grinding to max_pages
    return json.dumps({
        "total": total, "source": source, "page": page, "pages_fetched": pages_fetched,
        "count_this_page": first_count, "count": len(seen),
        "products": list(seen.values()), "available_filter_tokens": brands_avail
    }, ensure_ascii=False, default=str)

@mcp.tool()
@_guarded
def discover_brands(query: str, category: str = "") -> str:
    """Discover brand filter tokens (p_123:<id>) for a query. Use before search_products(brands=[...]).
    Returns: [{"token": "p_123:214020", "label": "boAt"}, ...]"""
    for bad in (_validate_query(query), _validate_category(category)):
        if bad:
            return bad
    s = az()
    params = {"k": query.strip()}
    if category: params["i"] = category
    _relaxed_polite()
    r = _get(s, "/s", params)
    tokens = sp.parse_filters(r.text)
    return json.dumps({"brands": tokens.get("p_123", [])}, ensure_ascii=False)

@mcp.tool()
@_guarded
def get_product(asin: str) -> str:
    """Get full product detail for an ASIN: title, brand, price, images (hi-res), bullets, description, A+ content, details table, variants (color/size), availability, rating, review_count, breadcrumbs, seller, EMI line, offers (bank/cashback/partner list), exchange offer, warranty, frequently-bought-together (fbt), size chart.
    New (extraction-depth): customers_say {summary, themes[]} (AI 'Customers say' block), video_count + video_thumb, aplus_images[] (A+ is inline image-heavy on amazon.in — no iframe; aplus holds its text headings), ships_from (the 'Dispatched by' slot does not render on amazon.in — Ships from vs Sold by is the split), add_to_cart (bool), buying_options_count (from the 'Other sellers/New (N) from' link; None when single/absent).
    details now merges table.prodDetTable + productDetails_techSpec_section* + productDetails_detailBullets_sections* + the product-overview po- grid (existing keys win; 'Best Sellers Rank' lands from the spec tables when rendered — 'Date First Available' does not render on amazon.in pages seen).
    Returns: {asin, title, brand, price, price_value, list_price, savings_pct, images, bullets, description, aplus, details, variants, availability, rating, review_count, breadcrumbs, seller, emi, offers, exchange_offer, warranty, fbt, size_chart, customers_say, video_count, video_thumb, aplus_images, ships_from, add_to_cart, buying_options_count}
    `asin` also accepts a full amazon.in product URL (/dp/, /gp/product/, /product/, /gp/aw/d/)
    or a 10-digit ISBN-10 — the ASIN is extracted; garbage returns {"error": "INVALID_ASIN"}
    before any request. description/aplus are capped at 6000 chars (see _clip).
    404/dead ASIN: {"error": "DOGPAGE"} (small body, 'Page Not Found')."""
    a, bad = _need_asin(asin)
    if bad:
        return bad
    s = az()
    _relaxed_polite()
    r = _get(s, f"/dp/{a}")
    out = pe.extract_product(r.text, a)
    out["description"] = _clip(out.get("description"))
    out["aplus"] = _clip(out.get("aplus"))
    out["http_status"] = r.status_code
    return json.dumps(out, ensure_ascii=False, default=str)

@mcp.tool()
@_guarded
def get_reviews(asin: str) -> str:
    """Get ratings histogram + top reviews for an ASIN (no login). Returns: {"asin":..., "ratings": {"average":..., "total_ratings":..., "histogram_percent": {...}, "histogram_counts": {...}}, "ratings_extras": {"reviews_with_images": <distinct reviews in the 'Reviews with images' carousel, null if widget absent>, "mention_chips": [{"theme":..., "mentions":..., "positive":..., "negative":...}, ...]}, "top_reviews": [{"review_id":..., "rating":..., "title":..., "author":..., "country":..., "date":..., "verified_purchase":..., "body":..., "helpful_count":..., "images": [...], "images_hi_res": [...], "has_image": bool}, ...]}
    Top reviews: up to ~13 domestic + foreign batch. 0-review product: ratings total 0, histogram {}, top_reviews []. Star distribution IS the histogram (no separate widget). For deep paginated history see get_all_reviews."""
    a, bad = _need_asin(asin)
    if bad:
        return bad
    s = az()
    _relaxed_polite()
    r = _get(s, f"/dp/{a}")
    if r.status_code != 200 or len(r.text) < 200000:
        return json.dumps({"error": f"dp fetch failed: {r.status_code} {len(r.text)}b"})
    ratings, revs = rf.parse_ratings(r.text), rf.parse_reviews(r.text)
    if not revs and ratings["total_ratings"]:
        # observed once: 91k-rating dp served a variant with 0 review blocks — retry once
        _relaxed_polite()
        r = _get(s, f"/dp/{a}")
        ratings, revs = rf.parse_ratings(r.text), rf.parse_reviews(r.text)
    out = {"asin": a, "ratings": ratings, "ratings_extras": rf.parse_ratings_extras(r.text),
           "top_reviews": _clip_reviews(revs)}
    if not revs and ratings["total_ratings"]:
        out["note"] = f"review widget not rendered on this page ({ratings['total_ratings']} ratings exist) — retry"
    return json.dumps(out, ensure_ascii=False, default=str)

@mcp.tool()
@_guarded
def get_all_reviews(asin: str, page: int = 1, sort_by: str = "", filter_by_star: str = "",
                    reviewer_type: str = "", format_type: str = "") -> str:
    """Paginated reviews via /product-reviews/<ASIN>/. 2026 reality: login-gated (302 -> /ap/signin) without authenticated cookies. Returns {"gated": true, "signin_url": ..., "note": ...} if unauthenticated. Param vocabulary (accepted on gated pages): sort_by: recent|helpful|top; filter_by_star: one_star|two_star|three_star|four_star|five_star|positive|critical|all_stars; reviewer_type: all_reviews|avp_only_reviews; format_type: all_formats|current_format.
    With an authenticated session this same call returns parsed reviews (hooks are identical to get_reviews)."""
    # sort_by/filter_by_star vocab is NOT pre-validated: unknown values are passed
    # through and the response handled (gated page reports the accepted vocabulary).
    bad = _validate_page(page)
    if bad:
        return bad
    s = az()
    _relaxed_polite()
    params = {"pageNumber": str(page)}
    if sort_by:        params["sortBy"] = sort_by
    if filter_by_star: params["filterByStar"] = filter_by_star
    if reviewer_type:  params["reviewerType"] = reviewer_type
    if format_type:    params["formatType"] = format_type
    r = _get(s, f"/product-reviews/{asin}/", params)
    gated = "/ap/signin" in str(r.url) or "Sign in or create account" in r.text[:60000]
    if gated:
        return json.dumps({"gated": True, "signin_url": str(r.url)[:250], "asin": asin, "params": params,
                           "note": "Login required for paginated review list. Use get_reviews(asin) for no-login top reviews."})
    return json.dumps({"gated": False, "reviews": _clip_reviews(rf.parse_reviews(r.text)),
                       "ratings": rf.parse_ratings(r.text)}, ensure_ascii=False, default=str)

@mcp.tool()
@_guarded
def set_pincode(pincode: str) -> str:
    """Set delivery pincode (6-digit Indian PIN, first digit 1-9). Returns success + readback: {"success": bool, "pincode":..., "city":..., "state":..., "bound_pin":...}. Invalid pincodes ("12345", "abcdef", "000000") return {"error": ...} before any network request. All subsequent product/search calls in this session are personalized to the pincode."""
    if not pc.valid_pincode(pincode):
        return json.dumps({"error": f"invalid pincode {pincode!r}",
                           "hint": "expected 6 digits, first digit 1-9 (e.g. 400001) — no request sent"})
    s = az()
    ubid = pc.ensure_ubid(s)
    _relaxed_polite()
    pg = _get(s, "/s?k=laptop")
    csrf = pc.scrape_csrf(pg.text)
    if not csrf:
        return json.dumps({"error": "csrf not found", "hint": "page fetch may have failed"})
    _relaxed_polite()
    r = pc.set_pincode(s, pincode, csrf)
    j = r.json() if "json" in (r.headers.get("content-type") or "") else {}
    _relaxed_polite()
    lab = pc.get_location_label(s, csrf)
    _save_jar()
    if str(lab.get("selectedDestination", {}).get("destinationValue")) == str(pincode):
        return json.dumps({"success": True, "pincode": pincode,
                           "city": j.get("address", {}).get("city"), "state": j.get("address", {}).get("state"),
                           "bound_pin": lab.get("selectedDestination", {}).get("destinationValue")})
    return json.dumps({"success": False, "response": j, "label": lab.get("selectedDestination")})

@mcp.tool()
@_guarded
def get_delivery_info(asin: str) -> str:
    """Get delivery estimate for an ASIN in current session's pincode context. Returns {asin:..., bound_pin:..., glow: [...], dp_zip:..., delivery_info: [...]}"""
    a, bad = _need_asin(asin)
    if bad:
        return bad
    s = az()
    _relaxed_polite()
    r = _get(s, f"/dp/{a}")
    return json.dumps({
        "asin": a,
        "glow": pc.glow_location(r.text),
        "dp_zip": pc.dp_zipcode(r.text),
        "delivery_info": pc.delivery_info(r.text)
    }, ensure_ascii=False, default=str)

@mcp.tool()
@_guarded
def get_suggestions(query: str) -> str:
    """Get search suggestions for a query prefix. Returns related-search chips from a real search page (6 per query)."""
    bad = _validate_query(query)
    if bad:
        return bad
    s = az()
    _relaxed_polite()
    r = _get(s, "/s", {"k": query.strip()})
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "lxml")
    chips = [a.get_text(strip=True) for a in soup.select('a[href*="rsl_sug"]')]
    return json.dumps({"query": query, "suggestions": chips})

@mcp.tool()
@_guarded
def demo() -> str:
    """Run all offline self-checks (no network). Returns {"search": ..., "product": ..., "reviews": ..., "pincode": ...}"""
    out = {}
    try:
        h = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_cache", "earbuds_base.html"), encoding="utf-8").read()
        out["search"] = {"total": sp.parse_total(h)[0], "items": len(sp.parse_products(h)), "ok": True}
        ph = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extra_html", "search_iphone.html")
        if os.path.exists(ph):   # extraction-depth: delivery_date / bank_offer on live tiles
            pt = sp.parse_products(open(ph, encoding="utf-8").read())
            assert any(t["delivery_date"] for t in pt), "no delivery_date parsed"
            assert any(t["bank_offer"] for t in pt), "no bank_offer parsed"
            out["search"]["delivery_date"] = next(t["delivery_date"] for t in pt if t["delivery_date"])
            out["search"]["bank_offer"] = next(t["bank_offer"] for t in pt if t["bank_offer"])
    except Exception as e:
        out["search"] = {"ok": False, "error": str(e)}
    try:
        lap = pe.extract_product(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "product_html", "prod_laptop1.html"), encoding="utf-8").read())
        dog = pe.extract_product(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "product_html", "prod_dead.html"), encoding="utf-8").read(), "B000000000")
        out["product"] = {"title": lap["title"][:40], "price": lap["price"], "variants": len(lap["variants"]["items"]), "dogpage": dog.get("error"), "ok": True}
    except Exception as e:
        out["product"] = {"ok": False, "error": str(e)}
    try:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reviews_html")
        h1 = open(os.path.join(d, "dp.html"), encoding="utf-8").read()
        h2 = open(os.path.join(d, "dp2.html"), encoding="utf-8").read()
        r1 = rf.parse_reviews(h1); r2 = rf.parse_reviews(h2)
        rat1 = rf.parse_ratings(h1); rat2 = rf.parse_ratings(h2)
        ex1 = rf.parse_ratings_extras(h1)   # extraction-depth: images count + mention chips
        assert ex1["reviews_with_images"] and ex1["mention_chips"], ex1
        out["reviews"] = {"dp1": {"n": len(r1), "avg": rat1["average"], "total": rat1["total_ratings"],
                                  "images": ex1["reviews_with_images"], "chips": len(ex1["mention_chips"])},
                          "dp2": {"n": len(r2), "avg": rat2["average"], "total": rat2["total_ratings"]}, "ok": True}
    except Exception as e:
        out["reviews"] = {"ok": False, "error": str(e)}
    out["pincode"] = {"note": "live test only — run set_pincode('110001') to verify",
                      "validation": {p: pc.valid_pincode(p) for p in ("110001", "400001", "12345", "abcdef", "000000")}}
    assert not pc.valid_pincode("12345") and not pc.valid_pincode("abcdef") \
        and not pc.valid_pincode("000000") and pc.valid_pincode("400001"), "pincode validation broken"
    return json.dumps(out, ensure_ascii=False, default=str)

def main():
    """Console-script entry point (amazon-mcp)."""
    mcp.run(transport="stdio")

if __name__ == "__main__":
    if "--demo" in sys.argv:
        print(demo())
    else:
        mcp.run(transport="stdio")
