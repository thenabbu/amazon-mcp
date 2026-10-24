"""pricehistory.app client: free long-window amazon.in price history.

Primary path (2 anonymous calls, no auth, no token):
  1. POST /api/search {"url": "https://www.amazon.in/dp/<ASIN>"} -> {code, name}
  2. GET  /p/<code>  -> decode PagePriceHistoryDataSet (base64 XOR CachedKey —
     the algorithm their own Chrome extension ships) -> full payload.
Fallbacks: fresh page token -> POST /api/price/<code>; meta-description aggregates.
Reads only. Cache: asin->code forever (sqlite), payload 6 h. Any 403/429/5xx trips
a sticky 45 s cooldown -> callers degrade to local data. AMAZON_MCP_DISABLE_PH=1 off.
"""
import base64, json, os, random, re, sqlite3, time

from curl_cffi import requests as phreq
import pincode_common as pc

SITE = "https://pricehistory.app"
_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_TTL = 6 * 3600
_COOLDOWN_SECS = 45
_FORMULA = ("price<=lowest*1.02 -> yes; <=avg(or (lowest+highest)/2) -> okay; "
            "<=avg+(highest-avg)*0.6 -> wait; else skip")
_cooldown_until = 0.0
_sess = None
_payload_cache = {}   # L1 memory: code -> (fetched_at, payload); L2 = sqlite ph_cache (survives restarts)


def disabled():
    return bool(os.environ.get("AMAZON_MCP_DISABLE_PH"))


def cooling():
    return time.time() < _cooldown_until


def _trip():
    global _cooldown_until
    _cooldown_until = time.time() + _COOLDOWN_SECS


def _session():
    global _sess
    if _sess is None:
        _sess = phreq.Session(impersonate="chrome131")
    return _sess


def _polite():
    time.sleep(random.uniform(1.5, 3.0))


def _ok(r):
    """Explicit block/abuse response -> trip shared cooldown, signal caller."""
    if r.status_code in (403, 429) or r.status_code >= 500:
        _trip()
        return False
    return True


# ------------------------------------------------------------- network (patchable in tests)
def _search(asin):
    """-> {"status": bool, "code"?, "name"?} | None. Bare ASINs are rejected by
    their API — always wrap in a /dp/ URL (messy tracking params are fine)."""
    try:
        r = _session().post(SITE + "/api/search",
                            json={"url": f"https://www.amazon.in/dp/{asin}"},
                            headers={"Content-Type": "application/json",
                                     "Origin": SITE, "Referer": SITE + "/"},
                            timeout=10)
        if not _ok(r):
            return None
        return r.json()
    except Exception:
        return None


def _page(code):
    """Product page HTML (slug not validated server-side; /p/x-<code> works)."""
    try:
        r = _session().get(SITE + "/p/x-" + code, timeout=15)
        if not _ok(r):
            return None
        return r.text
    except Exception:
        return None


def _api_price(code, token):
    """Fallback #2 — needs a token minted by a page render within minutes."""
    try:
        r = _session().post(SITE + "/api/price/" + code, json={},
                            headers={"Content-Type": "application/json",
                                     "page": code, "token": token, "Origin": SITE},
                            timeout=15)
        if not _ok(r):
            return None
        return r.json()
    except Exception:
        return None


# -------------------------------------------------------------------- pure helpers
def is_valid_code(code):
    """Their base62 checksum (ported from the extension's phcode.js): rejects
    garbage codes offline, before any network call."""
    if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Za-z]{1,10}", code):
        return False
    n = 0
    for ch in reversed(code):
        n = n * 62 + _B62.index(ch)
    full = str(n)
    return len(full) == 14 and int(full[0]) == (int(full[1:]) % 9) + 1 and full[10] == full[13]


def decode_page(html):
    """PRIMARY: extract the payload XOR-embedded in the product page."""
    d = re.search(r'PagePriceHistoryDataSet\s*=\s*["\']([A-Za-z0-9+/=]+)["\']', html or "")
    k = re.search(r'CachedKey\s*=\s*["\']([^"\']+)["\']', html or "")
    if not d or not k:
        return None
    try:
        raw = base64.b64decode(d.group(1))
        key = k.group(1).encode()
        return json.loads(bytes(b ^ key[i % len(key)] for i, b in enumerate(raw)).decode("utf-8", "replace"))
    except Exception:
        return None


def _token_from(html):
    m = re.search(r'FetchHeaders\.append\("token","([a-f0-9]{64})"', html or "")
    return m.group(1) if m else None


def _meta_aggs(html):
    """Fallback #3 — the page's meta description carries the 4 aggregates."""
    m = re.search(r"Lowest Price: ₹([\d,.]+) \| Average Price: ₹([\d,.]+) \| "
                  r"Highest Price: ₹([\d,.]+) \| MRP: ₹([\d,.]+)", html or "")
    if not m:
        return None
    num = lambda s: float(s.replace(",", "").rstrip("."))
    price = None
    p = re.search(r"Amazon Price in India on [\d/]+: ₹([\d,]+(?:\.\d+)?)", html or "")
    if p:
        price = num(p.group(1))
    return {"lowest": num(m.group(1)), "avg": num(m.group(2)),
            "highest": num(m.group(3)), "mrp": num(m.group(4)), "price": price}


def verdict(price, lowest, highest, average):
    """Their buy/hold rule (verdict.js), transparent 4-branch — no hidden weights."""
    if not (price and lowest and highest) or price <= 0 or lowest <= 0 or highest <= 0:
        return None
    usual = average or (lowest + highest) / 2
    if price <= lowest * 1.02:
        key, label = "yes", "Good time to buy"
    elif price <= usual:
        key, label = "okay", "Okay to buy"
    elif price <= usual + (highest - usual) * 0.6:
        key, label = "wait", "Better to wait"
    else:
        key, label = "skip", "Skip for now"
    return {"price": price, "key": key, "label": label, "formula": _FORMULA}


# ------------------------------------------------------------------ mapping + payload caches
def _map_db():
    pc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(pc.STATE_DIR / "prices.db"))
    c.execute("CREATE TABLE IF NOT EXISTS ph_map (asin TEXT PRIMARY KEY, code TEXT, name TEXT)")
    return c


def _map_get(asin):
    try:
        with _map_db() as c:
            row = c.execute("SELECT code, name FROM ph_map WHERE asin=?", (asin,)).fetchone()
        return (row[0], row[1]) if row else (None, None)
    except Exception:
        return (None, None)


def _map_put(asin, code, name):
    try:
        with _map_db() as c:
            c.execute("INSERT OR REPLACE INTO ph_map VALUES (?,?,?)", (asin, code, name))
    except Exception:
        pass


def _cache_db():
    pc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(pc.STATE_DIR / "prices.db"))
    c.execute("CREATE TABLE IF NOT EXISTS ph_cache (code TEXT PRIMARY KEY, fetched_at REAL, payload TEXT)")
    return c


def _cache_get(code):
    """-> (payload, age_s) | None. Memory first, then sqlite — so repeat calls and
    container restarts inside the 6h TTL never touch the network."""
    now = time.time()
    m = _payload_cache.get(code)
    if m and now - m[0] < _TTL:
        return m[1], int(now - m[0])
    try:
        with _cache_db() as c:
            row = c.execute("SELECT fetched_at, payload FROM ph_cache WHERE code=?", (code,)).fetchone()
        if row and now - row[0] < _TTL:
            _payload_cache[code] = (row[0], json.loads(row[1]))
            return _payload_cache[code][1], int(now - row[0])
    except Exception:
        pass
    return None


def _cache_put(code, payload):
    at = time.time()
    _payload_cache[code] = (at, payload)
    try:
        with _cache_db() as c:
            c.execute("INSERT OR REPLACE INTO ph_cache VALUES (?,?,?)",
                      (code, at, json.dumps(payload, ensure_ascii=False)))
    except Exception:
        pass   # persistence is best-effort; the memory layer still serves this process


# ------------------------------------------------------------------------- normalize
def normalize(payload, asin, days, product_name=None):
    """Payload -> external contract. *_ever/avg are FULL-ARCHIVE; series/offer_series/sales
    are clipped to `days`. Points are daily (their granularity)."""
    P = payload.get("Price") or {}
    M = payload.get("Main") or {}
    hist = payload.get("History") or {}

    def pts(key, with_offers=False):
        out = []
        for x in (hist.get(key) or []):
            y, t = x.get("y"), str(x.get("x") or "")
            if y is None or t in ("", "None"):
                continue
            item = {"t": t[:16], "price": y}
            if with_offers:
                item["offers"] = x.get("Offers") or []
            out.append(item)
        return out

    series, offers = pts("Price"), pts("OfferPrice", with_offers=True)
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    w_series = [p for p in series if p["t"][:10] >= cutoff]
    w_offers = [o for o in offers if o["t"][:10] >= cutoff]
    count = int(P.get("Count") or 0) or len(series)
    total = P.get("TotalPrice")
    avg = round(total / count) if total and count else None
    cur = P.get("Price") or M.get("Price")
    low, high = P.get("MinPrice"), P.get("MaxPrice")
    sales = []
    for e in payload.get("Event") or []:
        s, en = str(e.get("Start") or "")[:10], str(e.get("End") or "")[:10]
        if s and en and s != "None" and (s >= cutoff or en >= cutoff):
            sales.append({"name": e.get("Name"), "start": s, "end": en})
    wins = [p["t"][:10] for p in series]
    return {
        "source": "pricehistory.app",
        "code": payload.get("Code"), "asin": asin, "product_name": product_name,
        "points": len(series), "granularity": "daily",
        "window": {"from": wins[0], "to": wins[-1]} if wins else None,
        "lowest_ever": {"price": low, "on": (str(P.get("MinPriceOn"))[:10] or None) if P.get("MinPriceOn") else None},
        "highest_ever": {"price": high, "on": (str(P.get("MaxPriceOn"))[:10] or None) if P.get("MaxPriceOn") else None},
        "avg": avg,
        "current": {"price": cur, "mrp": P.get("MRP") or M.get("MRP"),
                    "coupon": M.get("Coupon"), "deal": P.get("DealPrice") or M.get("DealType")},
        "lowest_offer_ever": P.get("MinOfferPrice"),
        "series": w_series, "offer_series": w_offers, "sales": sales,
        "verdict": verdict(cur, low, high, avg),
        "updated_at": payload.get("HistoryUpdated"),
    }


def _normalize_meta(m, asin, days, product_name):
    """Fallback #3: aggregates-only payload shape (Count=1, TotalPrice=avg keeps
    the avg math honest; no series to clip)."""
    synth = {"Code": None, "HistoryUpdated": None,
             "Price": {"MinPrice": m["lowest"], "MinPriceOn": None,
                       "MaxPrice": m["highest"], "MaxPriceOn": None,
                       "Price": m["price"], "MRP": m["mrp"],
                       "Count": 1, "TotalPrice": m["avg"]},
             "Main": {"MRP": m["mrp"], "Price": m["price"]},
             "History": {"Price": [], "OfferPrice": []}, "Event": []}
    return normalize(synth, asin, days, product_name)


# ------------------------------------------------------------------------- pipeline
def get(asin, days=90):
    """-> (external dict | None, status) — status: ok | not_tracked | degraded_meta_only |
    unavailable | cooldown | disabled. Never raises; never hammers through a block."""
    if disabled():
        return None, "disabled"
    if cooling():
        return None, "cooldown"
    code, name = _map_get(asin)
    if not code:
        s = _search(asin)
        if s is None:
            return None, "unavailable"
        if not s.get("status") or not s.get("code"):
            return None, "not_tracked"
        code, name = s["code"], s.get("name")
        _map_put(asin, code, name)
        _polite()
    hit = _cache_get(code)
    if hit is not None:
        payload, age = hit
        ext = normalize(payload, asin, days, name)
        ext["cache"] = {"hit": True, "age_s": age}
        return ext, "ok"
    html = _page(code)
    if html is None:
        return None, "unavailable"
    payload = decode_page(html)
    if payload is None:                      # fallback #2: fresh token -> api/price
        _polite()
        tok = _token_from(html)
        payload = _api_price(code, tok) if tok else None
        if payload is None:                  # fallback #3: meta aggregates only
            m = _meta_aggs(html)
            if m:
                ext = _normalize_meta(m, asin, days, name)
                ext["cache"] = {"hit": False, "age_s": 0}
                return ext, "degraded_meta_only"
            return None, "unavailable"
    _cache_put(code, payload)
    ext = normalize(payload, asin, days, name)
    ext["cache"] = {"hit": False, "age_s": 0}
    return ext, "ok"
