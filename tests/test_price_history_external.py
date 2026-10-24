"""Offline tests for the pricehistory.app integration (zero real network)."""
import json, os, pathlib, sys, tempfile, time, types

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
FIX = HERE / "fixtures" / "pricehistory"
sys.path.insert(0, str(REPO / "amazon_mcp"))
import server            # noqa: E402
import pricehistory as ph  # noqa: E402


def _html(name):
    return (FIX / name).read_text(errors="replace")


def _json(name):
    return json.loads((FIX / name).read_text())


def _norm(pts):
    """(day, price) pairs, order-independent — page copy is day-truncated,
    api copy carries timestamps; values must agree."""
    return sorted((str(p["x"])[:10], p["y"]) for p in pts if p.get("y"))


# 1. primary decode path == api payload (3 real product pairs)
def test_ph_decode_matches_api():
    pairs = [("page_token.html", "price_ok.json", 93, 6),
             ("page_boat.html", "price_boat.json", 413, 6),
             ("page_rich.html", "price_offer_rich.json", 17, 3)]
    for page, apif, n, noffers in pairs:
        p = ph.decode_page(_html(page))
        assert p, page
        a = _json(apif)
        assert len(_norm(p["History"]["Price"])) == n == len(_norm(a["History"]["Price"])), page
        assert _norm(p["History"]["Price"]) == _norm(a["History"]["Price"]), page
        assert p["Price"]["MinPrice"] == a["Price"]["MinPrice"], page
        assert p["Price"]["MaxPrice"] == a["Price"]["MaxPrice"], page
        assert p["Price"]["TotalPrice"] == a["Price"]["TotalPrice"], page
        assert len(_norm(p["History"]["OfferPrice"])) == noffers, page
        assert len(p["Event"]) == len(a["Event"]) > 0, page
        assert p["HistoryUpdated"] == a["HistoryUpdated"], page
    # page copy: day-truncated x, dense offers, no PriceDays (documented shape)
    p = ph.decode_page(_html("page_boat.html"))
    assert p["History"]["Price"][0]["x"] == "2025-03-04"           # no time part
    assert "PriceDays" not in p
    assert p["History"]["OfferPrice"][0].get("Offers") == []        # dense, labelled
    assert any(o.get("Offers") for o in p["History"]["OfferPrice"] if o.get("y"))
    print("PASS test_ph_decode_matches_api")


# 2. code checksum (their algorithm) — valid codes pass, garbage dies offline
def test_ph_codes_and_checksum():
    for c in ["G2N6dpw8", "gjo9Jv2h", "4MFpdsG5", "Ljd3ArIm", "8r8O1vyp"]:
        assert ph.is_valid_code(c), c
    for bad in ["", "has space", "aaaa/bb", "x" * 11, "!!!!!!!!!!", None, 123]:
        assert not ph.is_valid_code(bad), bad
    print("PASS test_ph_codes_and_checksum")


# 3. page helpers: token + meta fallback extraction
def test_ph_page_helpers():
    tok = ph._token_from(_html("page_token.html"))
    assert tok and len(tok) == 64 and all(c in "0123456789abcdef" for c in tok)
    assert ph._token_from("no token here") is None
    m = ph._meta_aggs(_html("page_rich.html"))
    assert m and m["lowest"] == 131.49 and m["avg"] == 187 and m["highest"] == 250.22, m
    assert m["mrp"] == 675 and m["price"] == 131.49, m
    assert ph._meta_aggs("<html>nothing</html>") is None
    print("PASS test_ph_page_helpers")


# 4. normalize: aggregates all-time, series clipped to days, labels kept
def test_ph_normalize_and_clip():
    p = ph.decode_page(_html("page_token.html"))
    ext = ph.normalize(p, "B0G2B2QVLL", 30, product_name="Redmi 15C")
    assert ext["points"] == 93 and ext["granularity"] == "daily"
    assert ext["product_name"] == "Redmi 15C" and ext["code"] == "G2N6dpw8"
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
    assert ext["series"] and all(pt["t"][:10] >= cutoff for pt in ext["series"]), "window clip failed"
    assert all(o["t"][:10] >= cutoff for o in ext["offer_series"]), "offer clip failed"
    assert ext["lowest_ever"] == {"price": 13249, "on": "2026-02-21"}   # full-archive, unclipped
    assert ext["highest_ever"] == {"price": 21578, "on": "2026-08-06"}
    assert ext["avg"] == round(1634985 / 93)
    assert ext["current"]["price"] == 17999 and ext["current"]["mrp"] == 31999
    assert any(any("Coupon" in lab for lab in o["offers"])
               for o in ph.normalize(p, "B0G2B2QVLL", 3650)["offer_series"]), "labelled coupons missing"
    full = ph.normalize(p, "B0G2B2QVLL", 3650)
    assert len(full["series"]) == 93 and full["sales"]
    # 20 raw events; the 2015-10-13 one falls outside any 3650d window -> 19 kept (window logic correct)
    assert len(full["sales"]) == 19
    cut3650 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3650 * 86400))
    assert all(e["start"] >= cut3650 or e["end"] >= cut3650 for e in full["sales"])
    assert all(e["name"] for e in full["sales"])
    # fractional prices survive (hair-brush 131.49)
    rich = ph.normalize(ph.decode_page(_html("page_rich.html")), "B0XXXX", 3650)
    assert rich["current"]["price"] == 131.49 and rich["current"]["coupon"] == "5%"
    print("PASS test_ph_normalize_and_clip")


# 5. their 4-branch verdict rule, exact boundaries
def test_ph_verdict():
    lo, hi, avg = 13249, 21578, 17580
    assert ph.verdict(13249 * 1.02, lo, hi, avg)["key"] == "yes"          # at the 1.02 rail
    assert ph.verdict(17580, lo, hi, avg)["key"] == "okay"                # at avg
    assert ph.verdict(19000, lo, hi, avg)["key"] == "wait"                # inside 0.6 band
    assert ph.verdict(21000, lo, hi, avg)["key"] == "skip"                # over the band
    v = ph.verdict(17999, lo, hi, avg)
    # 17999 > avg 17580, inside the 0.6 band -> their rule says wait (matches the live boAt demo)
    assert v["key"] == "wait" and v["label"] == "Better to wait" and "formula" in v and v["price"] == 17999
    assert ph.verdict(None, lo, hi, avg) is None
    assert ph.verdict(10, 0, 0, 0) is None
    # price below recorded low is still computable: 10 <= 20*1.02 -> yes
    assert ph.verdict(10, 20, 30, None)["key"] == "yes"
    print("PASS test_ph_verdict")


# 6. pipeline statuses (ALL network stubbed) + cooldown + disable + caches
def test_ph_pipeline_statuses():
    calls = {"search": 0, "page": 0}
    real_state = ph.pc.STATE_DIR
    ph.pc.STATE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="phmap_"))
    real_search, real_page, real_cache = ph._search, ph._page, dict(ph._payload_cache)
    real_api_price, real_polite = ph._api_price, ph._polite
    ph._api_price = lambda code, tok: None     # fallback-2 target: never hit their server in tests
    ph._polite = lambda: None                  # no 1.5-3s politeness sleeps in the offline suite
    real_cool = ph._cooldown_until
    real_env = os.environ.get("AMAZON_MCP_DISABLE_PH")
    try:
        os.environ.pop("AMAZON_MCP_DISABLE_PH", None)
        ph._payload_cache.clear()
        ph._cooldown_until = 0.0

        # disabled -> no network at all
        os.environ["AMAZON_MCP_DISABLE_PH"] = "1"
        assert ph.get("B0GGGGGGGG") == (None, "disabled")
        os.environ.pop("AMAZON_MCP_DISABLE_PH")

        # cooldown -> fail fast, search untouched
        ph._cooldown_until = time.time() + 30
        assert ph.get("B0GGGGGGGG") == (None, "cooldown")
        ph._cooldown_until = 0.0
        assert calls["search"] == 0

        # not_tracked (their status:false)
        ph._search = lambda asin: (calls.__setitem__("search", calls["search"] + 1) or
                                   {"status": False, "message": "nope"})
        assert ph.get("B0GGGGGGGG")[1] == "not_tracked"

        # unavailable (search itself failed)
        ph._search = lambda asin: None
        assert ph.get("B0HHHHHHHH")[1] == "unavailable"

        # happy path: search -> page -> decode; cache MISS (age 0)
        def ok_search(asin):
            calls["search"] += 1
            return {"status": True, "code": "X" + asin[2:9], "name": "T " + asin}   # unique code per asin
        def ok_page(code):
            calls["page"] += 1
            return _html("page_token.html")
        ph._search, ph._page = ok_search, ok_page
        base = dict(calls)
        ext, st = ph.get("B0G2B2QVLL", days=30)
        assert st == "ok" and ext["points"] == 93 and ext["series"], (st, ext)
        assert ext["cache"] == {"hit": False, "age_s": 0}, ext["cache"]
        assert calls["search"] == base["search"] + 1 and calls["page"] == 1, calls
        # 2nd call: memory cache -> zero network
        ext2, st2 = ph.get("B0G2B2QVLL", days=30)
        assert st2 == "ok" and ext2["cache"]["hit"] is True, ext2.get("cache")
        assert calls["search"] == base["search"] + 1 and calls["page"] == 1, "memory cache miss"
        # "restart": memory cleared -> PERSISTENT sqlite cache still serves (0 network)
        ph._payload_cache.clear()
        ext3, st3 = ph.get("B0G2B2QVLL")
        assert st3 == "ok" and ext3["cache"]["hit"] is True and ext3["cache"]["age_s"] >= 0, \
            ext3.get("cache")
        assert calls["search"] == base["search"] + 1 and calls["page"] == 1, \
            "sqlite persistence broken (restarted process would re-fetch)"
        # expired row (>6h) -> fresh fetch; code map still saves the search call
        import sqlite3 as _sq
        with _sq.connect(str(ph.pc.STATE_DIR / "prices.db")) as _c:
            _c.execute("UPDATE ph_cache SET fetched_at = ?", (time.time() - 7 * 3600,))
        ph._payload_cache.clear()
        ext4, st4 = ph.get("B0G2B2QVLL")
        assert st4 == "ok" and ext4["cache"]["hit"] is False, ext4.get("cache")
        assert calls["search"] == base["search"] + 1 and calls["page"] == 2, calls

        # fallback #3: page without payload/token -> meta aggregates
        ph._page = lambda code: "<meta name=\"description\" content=\"Lowest Price: ₹131.49 | "
        ph._page = lambda code: ('<meta name="description" content="Lowest Price: ₹131.49 | '
                                 'Average Price: ₹187 | Highest Price: ₹250.22 | MRP: ₹675 | '
                                 'Amazon Price in India on 24/10/2026: ₹131.49.">')
        ph._payload_cache.clear()
        ext4, st4 = ph.get("B0IIIIIIII")
        assert st4 == "degraded_meta_only" and ext4["lowest_ever"]["price"] == 131.49
        assert ext4["points"] == 0 and ext4["avg"] == 187

        # fallback #2 shape: token present but api_price fails + meta absent -> unavailable
        ph._page = lambda code: 'FetchHeaders.append("token","aabbccdd00112233445566778899aabbccdd00112233445566778899aabbccdd") html-only'
        ph._payload_cache.clear()
        ext5, st5 = ph.get("B0JJJJJJJJ")
        assert (ext5, st5) == (None, "unavailable"), (ext5, st5)

        # 403 trips shared cooldown
        ph._trip()
        assert ph.cooling()
        ph._cooldown_until = 0.0
    finally:
        ph._search, ph._page = real_search, real_page
        ph._api_price, ph._polite = real_api_price, real_polite
        ph._payload_cache.clear(); ph._payload_cache.update(real_cache)
        ph.pc.STATE_DIR = real_state
        ph._cooldown_until = real_cool
        if real_env is None:
            os.environ.pop("AMAZON_MCP_DISABLE_PH", None)
        else:
            os.environ["AMAZON_MCP_DISABLE_PH"] = real_env
    print("PASS test_ph_pipeline_statuses")


# 7. tool: external-only contract, honest notes, degrade keeps the reason
def test_ph_tool_merge():
    real_get = ph.get
    try:
        stub = {"source": "pricehistory.app", "points": 93, "granularity": "daily",
                "lowest_ever": {"price": 13249, "on": "2026-02-21"},
                "cache": {"hit": False, "age_s": 0}}
        ph.get = lambda a, d=90: (dict(stub), "ok")
        r = json.loads(server.get_price_history("B0G2B2QVLL", days=30))
        assert r["external_status"] == "ok" and r["external"]["points"] == 93
        assert r["window_days"] == 30 and "93 daily pts" in r["note"], r["note"]
        assert "cache miss" in r["note"], r["note"]
        assert "local" not in r and "observations" not in r, "local block must be gone"
        # cache hit surfaces age in the note
        stub2 = dict(stub, cache={"hit": True, "age_s": 1234})
        ph.get = lambda a, d=90: (stub2, "ok")
        r = json.loads(server.get_price_history("B0G2B2QVLL"))
        assert "cache hit 1234s old" in r["note"], r["note"]
        # external unavailable -> reason preserved, no data, no fabrication
        ph.get = lambda a, d=90: (None, "unavailable")
        r = json.loads(server.get_price_history("B0G2B2QVLL"))
        assert r["external"] is None and r["external_status"] == "unavailable"
        assert "no data returned" in r["note"], r["note"]
        # module blows up -> caught, status says unavailable
        ph.get = lambda a, d=90: (_ for _ in ()).throw(RuntimeError("boom"))
        r = json.loads(server.get_price_history("B0G2B2QVLL"))
        assert r["external"] is None and r["external_status"].startswith("unavailable")
        # invalid days still rejected pre-network
        r = json.loads(server.get_price_history("B0G2B2QVLL", days=0))
        assert r.get("error") == "INVALID_PARAM"
    finally:
        ph.get = real_get
    print("PASS test_ph_tool_merge")


TESTS = [test_ph_decode_matches_api, test_ph_codes_and_checksum, test_ph_page_helpers,
         test_ph_normalize_and_clip, test_ph_verdict, test_ph_pipeline_statuses,
         test_ph_tool_merge]


def main():
    for t in TESTS:
        t()
    print(f"ALL {len(TESTS)} PRICEHISTORY TESTS PASS")


if __name__ == "__main__":
    main()
