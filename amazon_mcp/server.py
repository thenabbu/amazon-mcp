"""amazon.in MCP server — search, filters, pincode, product pages, reviews.

Thin tool layer over proven recon modules (amz_lib, search_parse, product_extract,
reviews_fetch, pincode_common). Single shared AmzSession with cookie-jar persistence
and polite rate limiting.

Run: python server.py   (stdio transport)
"""
import sys, os, re, json, time, random, functools, threading, secrets
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.provider import AccessToken
from amz_lib import AmzSession, polite, RateLimitError
import search_parse as sp
import product_extract as pe
import reviews_fetch as rf
import pincode_common as pc

COOKIE_JAR = os.path.join(str(pc.STATE_DIR), "cookies.json")

# Opt-in bearer auth for --http (review P0): set AMAZON_MCP_TOKEN and every
# /mcp request must carry `Authorization: Bearer <token>`; unset = open endpoint
# (README warns). Public URL feeds the OAuth protected-resource metadata only.
_HTTP_TOKEN = os.environ.get("AMAZON_MCP_TOKEN", "").strip()
_PUBLIC_URL = os.environ.get("AMAZON_MCP_PUBLIC_URL", "http://localhost:8787").rstrip("/")

class _StaticTokenVerifier:
    def __init__(self, tok: str):
        self._tok = tok
    async def verify_token(self, token: str):
        if secrets.compare_digest(token, self._tok):
            return AccessToken(token=token, client_id="static-token", scopes=[])
        return None

mcp = MCPServer("amazon-in", version="0.1.0",
                auth=(AuthSettings(issuer_url=_PUBLIC_URL, resource_server_url=_PUBLIC_URL,
                                   validate_token_resource=False) if _HTTP_TOKEN else None),
                token_verifier=(_StaticTokenVerifier(_HTTP_TOKEN) if _HTTP_TOKEN else None))

# ---------------------------------------------------------------- hardening helpers
_NET = threading.Lock()   # ponytail: global lock, per-session locks if throughput matters
_CURRENT_PIN: "str | None" = None   # session's currently-bound delivery pin (set_pincode / override)
_FIELD_CAP = 6000
_SORTS = {"relevanceblender", "price-asc-rank", "price-desc-rank",
          "review-rank", "date-desc-rank", "exact-aware-popularity-rank"}

def _match_brands(requested, avail):
    """Match requested brand names against amazon's filter list for this query.
    exact label > unique prefix > unique substring (OLD: bare substring matched
    'Mi' to 'Xiaomi'). -> (p_123 id list, warnings for unmatched/ambiguous)"""
    picked, warnings, seen = [], [], set()
    for bn in requested:
        q = bn.lower().strip()
        exact = [b for b in avail if b["label"].lower() == q]
        pref  = [b for b in avail if b["label"].lower().startswith(q)]
        sub   = [b for b in avail if q in b["label"].lower()]
        hit = exact[0] if exact else (pref[0] if len(pref) == 1 else (sub[0] if len(sub) == 1 else None))
        if hit is None:
            warnings.append(f"brand not found (or ambiguous) in amazon's filter list for this query: {bn!r} — its filter was NOT applied")
            continue
        tok = hit["token"].split(":", 1)[1]
        if tok not in seen:
            seen.add(tok)
            picked.append(tok)
    return picked, warnings

def _apply_min_rating(products, min_rating):
    """Exact threshold enforcement. amazon's upstream p_72 filter only means
    '4 Stars & Up' (bucket-level) — never the precise value the caller asked for.
    -> (kept, excluded_count) — unrated products cannot satisfy a threshold, so they drop."""
    if not min_rating:
        return products, 0
    kept = [p for p in products if p.get("rating") is not None and p["rating"] >= min_rating]
    return kept, len(products) - len(kept)

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
    """GET + reject an empty response body or rate-wall before any parser sees it."""
    r = s.get(path, params)
    if r.status_code == 503:
        # all tools route through here — one guard fixes every caller (eval 2026-10-23:
        # 11+ calls across 3 agents died as raw {"error":"HTTP 503"} / a mislabeled DOGPAGE)
        raise ToolError("RATE_LIMITED", retry_after=45,
                        detail="amazon robot-check rate wall — wait ~45-60s and retry; pace heavy calls")
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
        except RateLimitError as e:
            return _err("RATE_LIMITED", tool=fn.__name__, retry_after=e.retry_after, detail=str(e))
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
        os.makedirs(os.path.dirname(COOKIE_JAR), exist_ok=True)
        json.dump(dict(az().s.cookies.items()), open(COOKIE_JAR, "w"), indent=1)
    except Exception:
        pass

def _relaxed_polite():
    # eval 2026-10-23: 1.2-2.2s between multi-page fetches still tripped the Akamai
    # wall under load — 2.5-4.5s holds. (patched to no-op by test_hardening)
    if _az is not None:
        time.sleep(random.uniform(2.5, 4.5))

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
    if condition not in ("", "New"):
        # only amazon's New-condition token is verified; silently accepting
        # 'Used'/'Refurbished' would apply NO filter (review P3 silent no-op)
        return _err("INVALID_PARAM", param="condition",
                    detail=f"unknown condition {condition!r} — only '' (any) or 'New' have a verified amazon.in filter",
                    allowed=["", "New"])
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
    brand_warnings = []
    if brands:
        brand_ids, brand_warnings = _match_brands(brands, brands_avail)
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
    # exact min_rating enforcement + honest filter/warning reporting (review P3/P4)
    products, filtered_out = _apply_min_rating(list(seen.values()), min_rating)
    warnings = list(brand_warnings)
    if filtered_out:
        warnings.append(f"min_rating {min_rating}: excluded {filtered_out} of {len(seen)} products after an exact post-filter "
                        "(amazon's own upstream filter is bucket-level only)")
    no_price = sum(1 for p in products if p.get("price") in (None, "", 0))
    if no_price:
        warnings.append(f"PRICE_UNAVAILABLE: {no_price}/{len(products)} products expose no parseable price on this page")
    return json.dumps({
        "total": total, "source": source, "page": page, "pages_fetched": pages_fetched,
        "count_this_page": first_count, "count": len(products),
        "products": products, "available_filter_tokens": brands_avail,
        "filters_applied": {k: v for k, v in params.items() if k not in ("k", "page")},
        "filtered_out": filtered_out, "warnings": warnings
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
    # honest per-field reporting: a missing price is NOT a successful full extraction
    out["warnings"] = (["PRICE_UNAVAILABLE: no parseable price rendered on this page"]
                       if out.get("price_value") is None else [])
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
    # per-input normalisation like every other tool: users paste URLs/garbage
    a, bad = _need_asin(asin)
    if bad:
        return bad
    s = az()
    _relaxed_polite()
    params = {"pageNumber": str(page)}
    if sort_by:        params["sortBy"] = sort_by
    if filter_by_star: params["filterByStar"] = filter_by_star
    if reviewer_type:  params["reviewerType"] = reviewer_type
    if format_type:    params["formatType"] = format_type
    r = _get(s, f"/product-reviews/{a}/", params)
    gated = "/ap/signin" in str(r.url) or "Sign in or create account" in r.text[:60000]
    if gated:
        return json.dumps({"gated": True, "signin_url": str(r.url)[:250], "asin": a, "params": params,
                           "note": "Login required for paginated review list. Use get_reviews(asin) for no-login top reviews."})
    return json.dumps({"gated": False, "reviews": _clip_reviews(rf.parse_reviews(r.text)),
                       "ratings": rf.parse_ratings(r.text)}, ensure_ascii=False, default=str)

def _bind_pin(s, pin):
    """Bind session location to `pin`; returns the set_pincode-style result dict.
    Always runs under _NET (called from a guarded tool) so bind+fetch sequences
    in one tool call are atomic — the shared-session pincode race dies here."""
    pc.ensure_ubid(s)
    _relaxed_polite()
    pg = _get(s, "/s?k=laptop")
    csrf = pc.scrape_csrf(pg.text)
    if not csrf:
        return json.dumps({"error": "csrf not found", "hint": "page fetch may have failed"})
    _relaxed_polite()
    r = pc.set_pincode(s, pin, csrf)
    j = r.json() if "json" in (r.headers.get("content-type") or "") else {}
    _relaxed_polite()
    lab = pc.get_location_label(s, csrf)
    _save_jar()
    if str(lab.get("selectedDestination", {}).get("destinationValue")) == str(pin):
        return json.dumps({"success": True, "pincode": pin,
                           "city": j.get("address", {}).get("city"), "state": j.get("address", {}).get("state"),
                           "bound_pin": lab.get("selectedDestination", {}).get("destinationValue")})
    return json.dumps({"success": False, "response": j, "label": lab.get("selectedDestination")})

@mcp.tool()
@_guarded
def set_pincode(pincode: str) -> str:
    """Set delivery pincode (6-digit Indian PIN, first digit 1-9). Returns success + readback: {"success": bool, "pincode":..., "city":..., "state":..., "bound_pin":...}. Invalid pincodes ("12345", "abcdef", "000000") return {"error": ...} before any network request. All subsequent product/search calls in this session are personalized to the pincode."""
    global _CURRENT_PIN
    if not pc.valid_pincode(pincode):
        return json.dumps({"error": f"invalid pincode {pincode!r}",
                           "hint": "expected 6 digits, first digit 1-9 (e.g. 400001) — no request sent"})
    res = _bind_pin(az(), pincode)
    j = json.loads(res)
    if j.get("success"):
        _CURRENT_PIN = pincode
    return res

@mcp.tool()
@_guarded
def get_delivery_info(asin: str, pincode: str | None = None) -> str:
    """Get delivery estimate for an ASIN. pincode: optional 6-digit override — bound and fetched atomically inside this call, so concurrent set_pincode() sessions can never cross-contaminate it (race-free); an override also becomes the session's current pin context. Returns {asin:..., bound_pin:..., glow: [...], dp_zip:..., delivery_info: [...]}"""
    global _CURRENT_PIN
    a, bad = _need_asin(asin)
    if bad:
        return bad
    s = az()
    if pincode is not None and pincode != _CURRENT_PIN:
        if not pc.valid_pincode(pincode):
            return _err("INVALID_PARAM", param="pincode",
                        detail=f"invalid pincode {pincode!r} — expected 6 digits, first digit 1-9")
        res = json.loads(_bind_pin(s, pincode))
        if not res.get("success"):
            return json.dumps({"error": "PINBIND_FAILED", "requested_pin": pincode, **res}, ensure_ascii=False, default=str)
        _CURRENT_PIN = pincode
    _relaxed_polite()
    r = _get(s, f"/dp/{a}")
    return json.dumps({
        "asin": a,
        "bound_pin_context": _CURRENT_PIN,
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

def main():
    """Console-script entry point.

      amazon-mcp                 stdio (local agent configs)
      amazon-mcp --http          streamable-http on 127.0.0.1:8787 (web connectors, path /mcp)
      amazon-mcp --http --port 9000 --host 0.0.0.0
    """
    if "--http" in sys.argv:
        argv = sys.argv[1:]
        def arg(flag, default):
            return argv[argv.index(flag) + 1] if flag in argv else default
        from mcp.server.transport_security import TransportSecuritySettings
        if not _HTTP_TOKEN:
            print("[amazon-mcp] WARNING: AMAZON_MCP_TOKEN unset — HTTP endpoint is "
                  "UNAUTHENTICATED. Set the env var to require `Authorization: Bearer <token>`.",
                  file=sys.stderr)
        # tunneled/web-connector requests arrive with a public Host/Origin header;
        # DNS-rebinding protection is a localhost concern and --http is an explicit
        # opt-in to exposure — the URL itself is open unless AMAZON_MCP_TOKEN is set
        mcp.run(transport="streamable-http",
                host=arg("--host", "127.0.0.1"),
                port=int(arg("--port", "8787")),
                stateless_http=True,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=False))
        return
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
