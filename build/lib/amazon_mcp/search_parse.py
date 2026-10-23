"""search_parse.py — amazon.in /s search page parser. Pure functions, no network.
Handles the 2025 'puis/glux' layout (no data-cel-widget; data-component-type="s-search-result").
"""
import re, json
from urllib.parse import urlparse, parse_qs, unquote
from bs4 import BeautifulSoup

BASE = "https://www.amazon.in"


def _inr(text):
    """'₹69,990' -> 69990 ; '₹1,04,990.00' -> 104990 (paise dropped, Indian commas kept working)."""
    if not text:
        return None
    m = re.search(r"(\d[\d,]*)(?:\.\d+)?", text)
    return int(m.group(1).replace(",", "")) if m else None


def _strike(scope, price=None):
    """Struck M.R.P value, or None. Skips per-unit prices ('(₹459.90/100 g)' — '/unit'
    suffix in the parent) and format-strip 'was' prices (e.g. Audible ₹0 tiles); prefers
    the 'M.R.P:'-labelled block, else the first label-less strike >= the current price."""
    best = None
    for p in scope.select("span.a-price.a-text-price"):
        s = p.select_one("span.a-offscreen")
        if not s:
            continue
        ptxt = p.parent.get_text(" ", strip=True)
        if "/" in ptxt:
            continue
        v = _inr(s.get_text())
        if v is None:
            continue
        if "M.R.P" in ptxt:
            return v
        if best is None and (price is None or v >= price):
            best = v
    return best


def parse_metadata(html):
    """P.declare('s\\-metadata', {...}) blob: totalResultCount, asinOnPageCount, page, keywords, qid, rid."""
    m = re.search(r"P\.declare\('s\\-metadata',\s*(\{.*?\})\);", html, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def is_no_results(html):
    """amazon.in no-results page shows 6 sponsored tiles and NO human message.
    Sole reliable signature: s-metadata.totalResultCount == 0."""
    md = parse_metadata(html)
    return md.get("totalResultCount") == 0


def parse_total(html):
    """(count, source) — exact from s-metadata JSON; fallback DOM info-bar (rounded)."""
    md = parse_metadata(html)
    if md.get("totalResultCount") is not None:
        return int(md["totalResultCount"]), "s-metadata"
    m = re.search(r"(?:over\s+)?([\d,]+)\+?\s+results for", html)
    if m:
        return int(m.group(1).replace(",", "")), "dom-info-bar"
    if is_no_results(html):
        return 0, "no-results"
    return None, "unknown"


def _hires(img_url):
    """https://m.media-amazon.com/images/I/71UBs8H1FnL._AC_UY218_.jpg -> .../71UBs8H1FnL._AC_SL1500_.jpg"""
    m = re.search(r"/images/I/([A-Za-z0-9+\-_.]+?)\.[^./]*\.jpg", img_url or "")
    if not m:
        return img_url
    return f"{BASE_SET}/images/I/{m.group(1)}._AC_SL1500_.jpg"


BASE_SET = "https://m.media-amazon.com"


def parse_products(html):
    """List of dicts: asin, title, url, price, list_price (integer ₹, paise dropped), discount_pct,
    rating, rating_count, bought, image, image_hires, sponsored, amazons_choice, deal_badge,
    low_stock, coupon, coupon_text, prime, delivery,
    plus (2026 extraction-depth add): delivery_date ('Fri, 23 Oct' | 'Tomorrow' | 'Today'
    parsed from delivery text), bank_offer (tile offer/EMI line, e.g. 'Save extra with
    No Cost EMI' — 'Bank Offer' text does not render on amazon.in search tiles)."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for tile in soup.select('div[data-component-type="s-search-result"]'):
        asin = tile.get("data-asin") or ""
        if not asin or "/" in asin:
            continue
        d = {"asin": asin}
        cls = " ".join(tile.get("class") or [])
        d["sponsored"] = ("AdHolder" in cls) or bool(tile.select_one(".puis-sponsored-label-text"))

        h2 = tile.select_one("h2")
        t = (h2.get("aria-label") or h2.get_text(" ", strip=True)) if h2 else None
        d["title"] = t.replace("Sponsored Ad - ", "", 1) if t else None

        d["url"] = f"{BASE}/dp/{asin}"
        sspa = tile.select_one('a[href*="/sspa/click"]')
        if sspa:
            try:
                q = parse_qs(urlparse(sspa["href"]).query)
                if q.get("url"):
                    d["url"] = BASE + unquote(q["url"][0])
            except Exception:
                pass

        pr = tile.select_one('[data-cy="price-recipe"]') or tile
        cur = pr.select_one('span.a-price:not(.a-text-price) span.a-offscreen')
        d["price"] = _inr(cur.get_text() if cur else None)
        # M.R.P can sit outside price-recipe; _strike filters per-unit & format-strip prices
        d["list_price"] = _strike(tile, d["price"])
        m = re.search(r"\((\d+)% off\)", pr.get_text(" ", strip=True))
        d["discount_pct"] = int(m.group(1)) if m else None

        rb = tile.select_one('[data-cy="reviews-block"]')
        d["rating"] = None
        d["rating_count"] = None
        if rb:
            alt = rb.select_one("span.a-icon-alt")
            m = re.match(r"([\d.]+) out of 5", alt.get_text()) if alt else None
            if m:
                d["rating"] = float(m.group(1))
            else:
                s = rb.select_one("span.a-size-small.a-color-base")
                if s:
                    try:
                        d["rating"] = float(s.get_text(strip=True))
                    except ValueError:
                        pass
            for a in rb.select("a[aria-label]"):
                m = re.match(r"([\d,]+) ratings?", a["aria-label"])
                if m:
                    d["rating_count"] = int(m.group(1).replace(",", ""))
                    break

        txt = tile.get_text(" ", strip=True)
        m = re.search(r"([\d.,]+K?\+?) bought in past month", txt)
        d["bought"] = m.group(1) if m else None
        m = re.search(r"(Only \d+ left in stock|Only \d+ left|Almost gone)", txt)
        d["low_stock"] = m.group(1) if m else None

        img = tile.select_one("img.s-image")
        d["image"] = img["src"] if img else None
        d["image_hires"] = _hires(d["image"])

        # Amazon's Choice renders as a status badge with TWO a-badge-text spans ("Amazon's",
        # "Choice") — keep it out of deal_badge, surface it as its own flag
        d["amazons_choice"] = bool(tile.select_one('[aria-label*="Amazon"][aria-label*="Choice"]'))
        badge = next((b for b in tile.select("span.a-badge-text")
                      if b.get_text(strip=True)
                      and not b.find_parent(attrs={"id": re.compile(r"amazons-choice$")})), None)
        d["deal_badge"] = badge.get_text(strip=True) if badge else None

        cc = tile.select_one('[data-component-type="s-coupon-component"]')
        if cc:
            ctext = re.sub(r"\s+", " ", cc.get_text(" ", strip=True))
            m = re.search(r"You pay\s*₹\s?([\d,]+(?:\.\d+)?)", ctext)
            d["coupon"] = float(m.group(1).replace(",", "")) if m else True
            d["coupon_text"] = ctext[:60]
        else:
            d["coupon"] = None
            d["coupon_text"] = None

        d["prime"] = bool(tile.select_one("i.a-icon-prime")) or ("✓prime" in txt) or bool(tile.select_one(".s-prime"))
        # first delivery block is sometimes an empty placeholder — take the first with text
        dv = next((e for e in tile.select('[data-cy="delivery-block"], .udm-primary-delivery-message')
                   if e.get_text(strip=True)), None)
        d["delivery"] = re.sub(r"\s+", " ", dv.get_text(" ", strip=True))[:80] if dv else None
        # structured delivery date: calendar date beats relative 'Tomorrow/Today'
        m = re.search(r"\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{1,2} "
                      r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))\b", d["delivery"] or "")
        if m:
            d["delivery_date"] = m.group(1)
        elif d["delivery"] and re.search(r"\bTomorrow\b", d["delivery"]):
            d["delivery_date"] = "Tomorrow"
        elif d["delivery"] and re.search(r"\bToday\b", d["delivery"]):
            d["delivery_date"] = "Today"
        else:
            d["delivery_date"] = None
        # bank/EMI offer line lives in the tile's truncate span (exact phrase, no parsing)
        off = next((t for t in (e.get_text(" ", strip=True) for e in tile.select("span.a-truncate-full"))
                    if t.startswith("Save extra with") or "bank offer" in t.lower()), None)
        if off is None:
            m = re.search(r"Bank Offer[^₹<]{0,45}", txt)
            off = m.group(0).strip() if m else None
        d["bank_offer"] = off[:60] if off else None
        out.append(d)
    return out


def parse_filters(html):
    """All rh= tokens present on page -> {key: [{token, label}]} (labels from anchor text)."""
    soup = BeautifulSoup(html, "lxml")
    groups = {}
    for a in soup.select('a[href*="rh="]'):
        try:
            rh = parse_qs(urlparse(a["href"]).query).get("rh", [""])[0]
        except Exception:
            continue
        label = a.get("aria-label") or a.get_text(" ", strip=True)
        for tok in rh.split(","):
            tok = tok.strip()
            if not tok:
                continue
            key = tok.split(":", 1)[0]
            entry = {"token": tok, "label": label[:60]}
            if entry not in groups.setdefault(key, []):
                groups[key].append(entry)
    return groups


def parse_sort_options(html):
    """select#s-result-sort-select -> [(value, label)]"""
    return re.findall(r'<option[^>]*value="([a-z\-]+)"[^>]*>([^<]+)</option>',
                      re.search(r'<select name="s".*?</select>', html, re.S).group(0)) if '<select name="s"' in html else []


def parse_departments(html):
    """search-alias dropdown values (used as &i=<alias>)."""
    return re.findall(r'value="search-alias=([a-z\-]+)"', html)


def has_price_form(html):
    """Dedicated low-price/high-price inputs present?"""
    return ('name="low-price"' in html) and ('name="high-price"' in html)
