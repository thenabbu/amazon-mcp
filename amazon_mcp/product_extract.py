"""product_extract.py — amazon.in /dp/<ASIN> full extraction (MCP-ready).

Usage:
  python product_extract.py B0HK7QBFYM [B0ASIN2 ...]   # live fetch via AmzSession
  python product_extract.py --html saved.html [more]  # offline parse of saved page
  python product_extract.py --demo                     # self-check on saved fixtures

Keys returned: asin, title, price, price_value, list_price, savings_pct, images,
image_thumbs, bullets, description, aplus, details{}, variants[], availability,
rating, review_count, coupon, promo_offers, url,
plus (2026 stress-test add): brand, seller, breadcrumbs[], emi, offers[],
exchange_offer, warranty, fbt[], size_chart,
plus (2026 extraction-depth add): customers_say{summary,themes[]}, video_count,
video_thumb, aplus_images[], ships_from, add_to_cart, buying_options_count.
A+ note (amazon.in): NO iframe anywhere (30 saved pages) — A+ renders INLINE in
#aplus_feature_div when it exists (image-heavy; text = headings only, so
aplus_images carries the real payload); /dp/ASIN/aplus returns the dp shell.
'Dispatched by' is NOT rendered on amazon.in — the split is Ships from
(ships_from) vs Sold by (seller).
Dog/404 page -> {"error": "DOGPAGE"}.
"""
import sys, os, re, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bs4 import BeautifulSoup

DP_URL = "https://www.amazon.in/dp/{}"

# ---------------- images ----------------
GALLERY_RE = re.compile(r"'colorImages'\s*:\s*\{[^{]*?'initial'\s*:\s*A\.\$\s*\.parseJSON\('(\[.*?\])'\)", re.S)

def _js_json(s):
    """Unescape a JS single-quoted JSON string payload ('[{\\"k\\":v}]') -> obj."""
    s = s.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
    return json.loads(s)

def extract_images(html, soup):
    """colorImages['initial'] entries: {hiRes, thumb, large, main:{url:[h,w]}}."""
    imgs, thumbs, mains = [], [], []
    m = GALLERY_RE.search(html)
    if m:
        try:
            for e in _js_json(m.group(1)):
                hi = e.get("hiRes") or e.get("large")
                if hi and hi not in imgs:
                    imgs.append(hi)
                if e.get("thumb"):
                    thumbs.append(e["thumb"])
                if e.get("main"):
                    mains.append(e["main"])
        except (ValueError, KeyError):
            pass
    if not imgs:  # DOM fallback: main landing image attrs
        li = soup.select_one("#landingImage")
        if li is not None:
            if li.get("data-old-hires"):
                imgs.append(li["data-old-hires"])
            da = li.get("data-a-dynamic-image")
            if da:
                try:
                    imgs += [u for u in json.loads(da) if u not in imgs]
                except ValueError:
                    pass
    return imgs, thumbs, mains

def upscale(url, token=None):
    """Amazon image ops: '...I/41qFJxG-nEL._SS40_.jpg' (40px thumb).
    token=None -> strip token = native full-size; 'AC_SL1500' -> 1500px cap."""
    base = re.sub(r"\._[A-Z0-9_,]+_\.", ".", url)  # kills SS40 / SL1254 / SX38_SY50_CR,0,0,38,50 etc.
    if token:
        base = re.sub(r"(\.\w+)$", f"._{token}_\\1", base)
    return base

# ---------------- variants ----------------
def extract_variants(html, soup, page_asin):
    """Twister JS (dimensionValuesDisplayData) first; book tmm formats fallback."""
    variants, dims = [], []
    i = html.find('"dimensionValuesDisplayData"')
    if i >= 0:
        try:
            dvdd, _ = json.JSONDecoder().raw_decode(html, html.index("{", i))
        except ValueError:
            dvdd = {}
        dm = re.findall(r'"dimensions"\s*:\s*(\[[^\]]*\])', html[max(0, i - 3000):i])
        if dm:
            try:
                dims = json.loads(dm[-1])
            except ValueError:
                pass
        for asin, vals in dvdd.items():
            variants.append({"asin": asin, "values": vals, "current": asin == page_asin})
    if not variants and soup.select_one("#tmmSwatches"):  # books: format matrix
        for sw in soup.select("#tmmSwatches .swatchElement"):
            a = sw.select_one("a.a-button-text") or sw.select_one("a")
            href = (a.get("href") or "") if a is not None else ""
            m = re.search(r"/dp/([A-Z0-9]{10})", href)
            label = sw.select_one(".slot-title")
            variants.append({
                "asin": m.group(1) if m else page_asin,
                "values": [label.get_text(strip=True)] if label is not None else [],
                "price": (sw.select_one(".slot-price").get_text(strip=True)
                          if sw.select_one(".slot-price") is not None else None),
                "current": "selected" in (sw.get("class") or []),
            })
    return {"dimensions": dims, "items": variants}

# ---------------- text fields ----------------
def _txt(soup, sel, attr=None):
    for el in soup.select(sel):
        t = el.get(attr, "").strip() if attr else el.get_text(" ", strip=True)
        if t and t.lower() != "null":
            return t
    return None

def extract_price(soup):
    price = (_txt(soup, "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen")
             or _txt(soup, "#corePrice_feature_div .priceToPay .a-offscreen")
             or _txt(soup, "#apex_desktop .priceToPay .a-offscreen"))
    savings = _txt(soup, ".apex-savings-percentage") or None
    lbl = soup.select_one("#apex-pricetopay-accessibility-label")  # "₹X with N percent savings"
    if (not price) and lbl is not None:
        parts = lbl.get_text(strip=True).split(" with ")
        price = parts[0] or None
        savings = savings or (re.sub(r"[^0-9]", "", parts[1]) + "%" if len(parts) > 1 and "percent" in parts[1] else None)
    list_price = (_txt(soup, "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen")
                 or _txt(soup, 'span.a-price.a-text-price[data-a-strike="true"] .a-offscreen'))
    val = None
    if price:
        d = re.sub(r"[^0-9.]", "", price)
        val = float(d) if d else None
    return price, val, list_price, savings

def extract_details(soup):
    out = {}
    # prodDetTable class + the two id-prefixed spec tables (techSpec / detailBullets)
    for tr in soup.select('table.prodDetTable tr, table[id^="productDetails_techSpec_section"] tr,'
                          ' table[id^="productDetails_detailBullets_sections"] tr'):
        th, td = tr.find("th"), tr.find("td")
        if th is not None and td is not None:
            out[th.get_text(" ", strip=True)] = td.get_text(" ", strip=True)
    if not out:
        for li in soup.select("#detailBullets_feature_div li"):    # books use bullets-as-kv
            txt = li.get_text(" ", strip=True).replace("‏", "").replace("‎", "")
            if ":" in txt:
                k, v = txt.split(":", 1)
                if k.strip() and v.strip():
                    out[k.strip()] = v.strip()
    if not out:
        for tr in soup.select("#productDetails_db_sections tr, #productDetails table tr"):
            th, td = tr.find("th"), tr.find("td")
            if th is not None and td is not None:
                out[th.get_text(" ", strip=True)] = td.get_text(" ", strip=True)
    # product-overview po- grid (Brand/Model/Screen...): new sub-keys only,
    # existing spec keys win so fixture details stay byte-stable
    for tr in soup.select('#productOverview_feature_div tr'):
        td = tr.find_all("td")
        if len(td) >= 2:
            k, v = td[0].get_text(" ", strip=True), td[1].get_text(" ", strip=True)
            if k and v and k not in out:
                out[k] = v
    return out

# ---------------- 2026 stress-test fields ----------------
def _brand(soup, details):
    for k in ("Brand", "Brand Name"):
        if details.get(k):
            return details[k]
    b = _txt(soup, "#bylineInfo")
    if b:
        m = re.match(r"Visit the (.+?) Store", b) or re.search(r"Brand:\s*(.+)", b)
        if m:
            return m.group(1)
    return None

def _offers(soup):
    """Bank/cashback/partner offers: vsx carousel cards first, sopp rows (.ibd_tr) fallback."""
    out = []
    for c in soup.select("#vsxoffers_feature_div .a-cardui"):
        if c.select_one(".a-cardui"):  # outer wrapper: keep innermost per-offer cards only
            continue
        t = re.sub(r"\s+", " ", c.get_text(" ", strip=True))
        t = re.sub(r"^Offers Previous page\s*", "", t)
        t = re.sub(r"\s+\d+\s+offers?\s+[A-Za-z& ]+$", "", t)  # trailing "1 offer Cashback" dup label
        if t and t not in out:
            out.append(t)
    if not out:
        out = [re.sub(r"\s+", " ", r.get_text(" ", strip=True))
               for r in soup.select(".ibd_tr") if r.get_text(" ", strip=True)]
    return out or None

def _fbt(soup):
    """Frequently bought together: new p13n bundle widget (data-csa-c-item-id cards)."""
    g = soup.select_one('[aria-labelledby="similarities-product-bundle-widget-title"], #fbt')
    if not g:
        return None
    out = []
    for el in g.find_all(attrs={"data-csa-c-item-id": True}):
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        t = re.sub(r"^(This item: |\+ )", "", t)
        title = re.split(r"₹", t)[0].strip(" |+-")
        if title:
            out.append({"asin": el["data-csa-c-item-id"].replace("amzn1.asin.", ""),
                        "title": title[:200]})
    return out or None

def _size_chart(soup):
    el = soup.select_one("#fit-sizechartv2-0 table, #fit-sizechartv2-0-table-0")
    if el is None:
        return None
    if el.name != "table":
        el = el.find("table")
    if el is None:
        return None
    rows = [[c.get_text(strip=True) for c in r.select("th, td")] for r in el.select("tr")]
    # ponytail: cap 40 rows, full chart markup if size-picker QA ever needs every size
    rows = [r for r in rows if any(r)][:40]
    return rows or None

def _breadcrumbs(soup):
    el = soup.select_one("#wayfinding-breadcrumbs_feature_div")
    if el is None:
        return None
    crumbs = [li.get_text(" ", strip=True) for li in el.select("li")]
    crumbs = [c for c in crumbs if c and c != "›"]
    return crumbs or None

# ---------------- 2026 extraction-depth fields ----------------
MENTION_RE = re.compile(r"(\d+) customers mention ([^,\"<]{2,40}), (\d+) positive, (\d+) negative")

def _mention_themes(html):
    """AI-review theme chips: '262 customers mention quality, 233 positive, 29 negative'."""
    themes, seen = [], set()
    for n, theme, pos, neg in MENTION_RE.findall(html):
        if theme in seen:
            continue
        seen.add(theme)
        themes.append({"theme": theme.strip(), "mentions": int(n),
                       "positive": int(pos), "negative": int(neg)})
    return themes

def _customers_say(soup, html):
    """'Customers say' AI block: summary prose in div[data-testid=overall-summary]
    right after the h3[data-testid=heading]; themes = customers-mention chips."""
    head = next((e for e in soup.select('[data-testid="heading"]')
                 if e.get_text(strip=True) == "Customers say"), None)
    themes = _mention_themes(html)
    if head is None and not themes:
        return None
    ov = soup.select_one('div[data-testid="overall-summary"]')
    summary = ov.get_text(" ", strip=True) if ov is not None else None
    if summary == "Customers say":
        summary = None
    return {"summary": summary, "themes": themes or None}

def _videos(html):
    """ATF video slate config: 'totalVideoCount':'2' + videoIngressATFSlateThumbURL."""
    count = None
    m = re.search(r"'totalVideoCount':'(\d+)'", html)
    if m:
        count = int(m.group(1))
    thumb = None
    m = re.search(r"'videoIngressATFSlateThumbURL':'([^']+)'", html)
    if m and m.group(1):
        thumb = m.group(1)
    return count, thumb

def _aplus_images(soup):
    """A+ renders INLINE in #aplus_feature_div on amazon.in (no iframe in any saved
    page) and is image-heavy — the payload is the aplus-media-library image URLs
    (grey-pixel = lazy placeholder, dropped), not text."""
    box = soup.select_one("#aplus_feature_div")
    if box is None:
        return None
    out = []
    for img in box.select("img"):
        u = img.get("data-src") or img.get("src") or ""
        if not u or "grey-pixel" in u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        if u not in out:
            out.append(u)
    return out or None

def extract_product(html, asin_hint=None):
    if "validateCaptcha" in html and len(html) < 20000:
        # transient bot-check page — amz_lib auto-solves on retry; parser can't read it
        return {"error": "CAPTCHA", "asin": asin_hint, "url": DP_URL.format(asin_hint) if asin_hint else None}
    if is_dogpage(html):
        return {"error": "DOGPAGE", "asin": asin_hint, "url": DP_URL.format(asin_hint) if asin_hint else None}
    soup = BeautifulSoup(html, "lxml")
    page_asin = asin_hint or _txt(soup, "input#ASIN", "value")
    if not page_asin:
        el = soup.select_one("[data-csa-c-asin]")
        page_asin = el.get("data-csa-c-asin") if el else None
    price, price_val, list_price, savings = extract_price(soup)
    imgs, thumbs, mains = extract_images(html, soup)
    rating = None
    pop = soup.select_one("#acrPopover")
    if pop is not None and pop.get("title"):
        r = re.search(r"([\d.]+) out of", pop["title"])
        rating = float(r.group(1)) if r else None
    rc = None
    rtxt = _txt(soup, "#acrCustomerReviewText")
    if rtxt:
        d = re.sub(r"[^0-9]", "", rtxt)
        rc = int(d) if d else None
    details = extract_details(soup)
    avail = _txt(soup, "#availability") or _txt(soup, "#outOfStock")
    if not avail and soup.select_one("#add-to-cart-button"):
        avail = "In stock"
    warranty = next((v for k, v in details.items() if "warrant" in k.lower()), None)
    warranty = warranty or _txt(soup, '[data-name="WARRANTY"]')
    exchange = _txt(soup, "#tradeInFeature_feature_div, #buyBackAccordionRow")
    video_count, video_thumb = _videos(html)
    m = re.search(r"New \((\d+)\) from", html)   # aod ingress 'Other sellers on Amazon New (8) from'
    return {
        "asin": page_asin,
        "url": DP_URL.format(page_asin) if page_asin else None,
        "title": _txt(soup, "#productTitle"),
        "brand": _brand(soup, details),
        "price": price,
        "price_value": price_val,
        "list_price": list_price,
        "savings_pct": savings,
        "images": imgs,
        "image_thumbs": thumbs,
        "bullets": [li.get_text(" ", strip=True) for li in soup.select("#feature-bullets li span.a-list-item")],
        "description": _txt(soup, "#productDescription") or _txt(soup, "#bookDescription_feature_div")
                       or _txt(soup, "#productDescription_feature_div"),
        "aplus": _txt(soup, "#aplus_feature_div") or _txt(soup, "#aplus") or _txt(soup, "#aplusBrandStory"),
        "details": details,
        "variants": extract_variants(html, soup, page_asin),
        "availability": avail,
        "rating": rating,
        "review_count": rc,
        "coupon": _txt(soup, "#couponBadgeRegularVpc"),
        "promo_offers": _txt(soup, "#promoPriceBlockMessage_feature_div, #apex_promo"),
        "breadcrumbs": _breadcrumbs(soup),
        "emi": _txt(soup, "#inemi_feature_div, #inemi, #EMIcalculator"),
        "offers": _offers(soup),
        "exchange_offer": (re.sub(r"\s+", " ", exchange)[:300] if exchange else None),
        "seller": _txt(soup, "#sellerProfileTriggerId"),
        "warranty": warranty,
        "fbt": _fbt(soup),
        "size_chart": _size_chart(soup),
        "customers_say": _customers_say(soup, html),
        "video_count": video_count,
        "video_thumb": video_thumb,
        "aplus_images": _aplus_images(soup),
        "ships_from": _txt(soup, '.offer-display-feature-text'
                               '[offer-display-feature-name="desktop-fulfiller-info"]'
                               " .offer-display-feature-text-message"),
        "add_to_cart": bool(soup.select_one("#add-to-cart-button")),
        "buying_options_count": int(m.group(1)) if m else None,
    }

def is_dogpage(html):
    """404 'dog page': ~2KB, title 'Page Not Found', api-services-support comment.
    NOT the transient validateCaptcha 'Continue shopping' page (also small, also
    carries the api-services-support comment — check captcha first)."""
    if len(html) > 6000:
        return False
    if "validateCaptcha" in html:
        return False
    return ("<title>Page Not Found" in html or "kailey-kitty" in html
            or ("api-services-support@amazon.com" in html and len(html) < 4000))

# ---------------- CLI ----------------
def fetch_and_extract(session, asin, route="/dp/{}"):
    r = session.get(route.format(asin))
    out = extract_product(r.text, asin)
    out["http_status"] = r.status_code
    return out

def _demo():
    fx = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "tests", "fixtures")
    d = os.path.join(fx, "product")
    lap = extract_product(open(os.path.join(d, "prod_laptop1.html"), encoding="utf-8").read())
    assert lap["title"].startswith("HP Victus"), lap["title"]
    assert lap["price_value"] == 108990.0, lap["price"]
    assert lap["list_price"] == "₹2,38,629", lap["list_price"]
    assert len(lap["images"]) >= 3 and "m.media-amazon.com" in lap["images"][0]
    assert len(lap["variants"]["items"]) == 8, lap["variants"]
    assert lap["details"].get("ASIN") == "B0HK7QBFYM"
    assert lap["brand"] == "HP" and lap["seller"] == "Clicktech Retail Private Ltd"
    assert lap["breadcrumbs"][:2] == ["Computers & Accessories", "Laptops"], lap["breadcrumbs"]
    assert lap["emi"].startswith("EMI starts at ₹"), lap["emi"]
    assert lap["offers"] and "Offer" in lap["offers"][0], lap["offers"][:1]
    assert lap["fbt"] and lap["fbt"][0]["asin"] == "B0HK7QBFYM", lap["fbt"][:1]
    assert lap["exchange_offer"].startswith("With Exchange"), lap["exchange_offer"]
    assert lap["ships_from"] == "Amazon" and lap["add_to_cart"] is True, lap["ships_from"]
    assert lap["video_count"] == 0 and lap["video_thumb"] is None, lap["video_count"]
    dog = extract_product(open(os.path.join(d, "prod_dead.html"), encoding="utf-8").read(), "B000000000")
    assert dog["error"] == "DOGPAGE", dog
    assert upscale("https://m.media-amazon.com/images/I/41q._SS40_.jpg").endswith("41q.jpg")
    assert upscale("https://m.media-amazon.com/images/I/41q._SS40_.jpg", "AC_SL1500").endswith("41q._AC_SL1500_.jpg")
    # --- 2026 extraction-depth checks (fixtures = tests/fixtures/product + extra) ---
    extra = os.path.join(fx, "extra")
    bk = extract_product(open(os.path.join(d, "prod_book.html"), encoding="utf-8").read())
    assert bk["details"].get("Best Sellers Rank"), "BSR must land in details"
    assert bk["buying_options_count"] == 15, bk["buying_options_count"]   # 'New (15) from' aod link
    cs = bk["customers_say"]
    assert cs and cs["summary"] and len(cs["themes"]) >= 3, cs
    assert bk["aplus_images"] and all("grey-pixel" not in u for u in bk["aplus_images"]), bk["aplus_images"]
    ph = os.path.join(extra, "dp_B0DGJ7TGDR.html")   # live iPhone dp (2026-10)
    if os.path.exists(ph):
        ip = extract_product(open(ph, encoding="utf-8").read())
        assert ip["video_count"] == 2 and ip["video_thumb"], ip["video_count"]
        assert ip["details"].get("Operating System") == "iOS", "po- grid must merge into details"[:80]
        assert ip["details"].get("Item model number"), ip["details"].keys()   # techSpec table
        assert len(ip["customers_say"]["themes"]) >= 8, ip["customers_say"]["themes"]
        assert ip["add_to_cart"] is True and ip["buying_options_count"] is None
        bt = extract_product(open(os.path.join(extra, "dp_B0FZ9YD8MN.html"), encoding="utf-8").read())
        assert bt["aplus_images"] and len(bt["aplus_images"]) >= 3, bt["aplus_images"]  # A+ inline, image-heavy
    print("demo OK")

def main(argv):
    if "--demo" in argv:
        return _demo()
    if "--html" in argv:
        files = [a for a in argv if not a.startswith("--")]
        return [extract_product(open(f, encoding="utf-8").read()) for f in files]
    from amz_lib import AmzSession, polite
    s, out = AmzSession(), []
    for asin in [a for a in argv if not a.startswith("--")]:
        out.append(fetch_and_extract(s, asin))
        polite()
    return out

if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:]), indent=1, ensure_ascii=False)[:6000])
