"""Hardening self-checks for the amazon.in MCP server — ALL OFFLINE (zero network).

Run: /opt/data/cache/scratch/amzvenv/bin/python test_hardening.py
Covers: ASIN/URL normalization, invalid-input rejection before network, corrupted
cookie-jar recovery, challenge-retry cap, max_pages pagination + dedupe + cap,
output clipping, empty response body, catch-all JSON errors, thread serialization,
and the demo self-check.
"""
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "amazon_mcp"))
import server   # noqa: E402
import amz_lib  # noqa: E402

URL_EXAMPLE = ("https://www.amazon.in/15C-Segments-Silicone-Sealing-Replacement/"
               "dp/B0G2B2QVLL/ref=sr_1_3?crid=ABC123&keywords=ring&qid=1700000000")
SEARCH_HTML = open(os.path.join(HERE, "amazon_mcp", "probe_cache", "earbuds_base.html"),
                   encoding="utf-8").read()


class FakeResp:
    def __init__(self, text="", status=200, url="https://www.amazon.in/s?k=x"):
        self.text, self.status_code, self.url = text, status, url
        self.headers = {}


def bump_asins(html, tag):
    """Give every result tile a fresh asin so a 'page' looks different from the last."""
    n = [0]
    def rep(m):
        n[0] += 1
        return f'data-asin="{tag}{n[0]:04d}XXXX"'   # tag(2) + 4 digits + pad = 10 chars
    return re.sub(r'data-asin="([A-Za-z0-9]{10})"', rep, html)


class FakeSession:
    """Stands in for AmzSession: canned bodies, counts calls, never touches network."""
    def __init__(self, text="", mode="same", status=200):
        self.text, self.mode, self.status = text, mode, status
        self.calls = []

    def get(self, path, params=None, **kw):
        self.calls.append((path, dict(params or {})))
        body = bump_asins(self.text, f"U{len(self.calls)}") if self.mode == "unique" else self.text
        return FakeResp(body, self.status)


def use_session(sess):
    server._az = sess
    return sess


def decoded(result):
    assert isinstance(result, str), f"tool returned {type(result)}, expected JSON str"
    obj = json.loads(result)
    assert isinstance(obj, dict), f"tool returned non-object: {obj!r}"
    return obj


# ---------------------------------------------------------------- 1. asin normalization
def test_asin_normalize():
    n = server.normalize_asin
    # accepted forms
    assert n("B0G2B2QVLL") == "B0G2B2QVLL"                       # bare ASIN
    assert n("  B0G2B2QVLL  ") == "B0G2B2QVLL"                    # whitespace
    assert n("b0g2b2qvll") == "B0G2B2QVLL"                        # lowercase
    assert n(URL_EXAMPLE) == "B0G2B2QVLL"                         # full URL + /ref= + query
    assert n("https://www.amazon.in/dp/B0G2B2QVLL?th=1&psc=1") == "B0G2B2QVLL"
    assert n("https://www.amazon.in/gp/product/B0G2B2QVLL") == "B0G2B2QVLL"
    assert n("https://www.amazon.in/foo/product/B0G2B2QVLL") == "B0G2B2QVLL"
    assert n("https://www.amazon.in/gp/aw/d/B0G2B2QVLL") == "B0G2B2QVLL"
    assert n("www.amazon.in/dp/B0G2B2QVLL#reviews") == "B0G2B2QVLL"  # no scheme + fragment
    assert n("8121923476") == "8121923476"                        # ISBN-10
    assert n("/dp/B0G2B2QVLL") == "B0G2B2QVLL"                    # path only
    # rejected
    for bad in ("", "   ", "not-an-asin!", "<script>alert(1)</script>", None, 12345,
                "https://www.amazon.in/gp/bad/B0G2B2QVLL", "B0G2B2QVLLL",   # 11 chars
                "no asin here", "https://www.amazon.in/s?k=laptop"):
        assert n(bad) is None, f"normalize_asin({bad!r}) should be None, got {n(bad)!r}"
    print("PASS test_asin_normalize")


# ---------------------------------------------------------------- 2. INVALID_ASIN wired, no network
def test_invalid_asin_rejected_before_network():
    for tool in (server.get_product, server.get_reviews, server.get_delivery_info):
        for bad in ("", "not-an-asin!", "<script>", URL_EXAMPLE.split("/dp/")[0]):
            sess = use_session(FakeSession(SEARCH_HTML))
            try:
                obj = decoded(tool(bad))
                assert obj.get("error") == "INVALID_ASIN", f"{tool.__name__}({bad!r}) -> {obj}"
                assert not sess.calls, f"{tool.__name__} hit network for invalid input"
            finally:
                server._az = None
    # valid URL flows through as a bare /dp/ request
    sess = use_session(FakeSession(SEARCH_HTML))
    try:
        decoded(server.get_delivery_info(URL_EXAMPLE))
        assert sess.calls and sess.calls[0][0] == "/dp/B0G2B2QVLL", sess.calls
    finally:
        server._az = None
    print("PASS test_invalid_asin_rejected_before_network")


# ---------------------------------------------------------------- 3. corrupted cookie jar
def test_corrupted_cookie_recovery():
    tmp = tempfile.mktemp(prefix="amzjar_", dir=os.path.join(HERE, "amazon_mcp"))
    real_jar, real_az = server.COOKIE_JAR, server._az
    server.COOKIE_JAR, server._az = tmp, None
    try:
        open(tmp, "wb").write(b"\x80\x81\xff garbage \xfe not json")
        sess = server.az()                       # must not raise
        assert sess is not None
        open(tmp, "w").write("[1, 2, 3]")        # valid JSON, wrong type
        server._az = None
        sess = server.az()                       # must not raise
        assert sess is not None
        server._save_jar()                       # recreate a healthy jar
        jar = json.loads(open(tmp).read())
        assert isinstance(jar, dict), f"jar not recreated as dict: {type(jar)}"
    finally:
        server.COOKIE_JAR, server._az = real_jar, real_az
        if os.path.exists(tmp):
            os.remove(tmp)
    print("PASS test_corrupted_cookie_recovery")


# ---------------------------------------------------------------- 4. challenge retry cap
def test_challenge_retry_cap():
    def run(body, solve):
        calls = [0]
        def fake_get(url, params=None, timeout=None, **kw):
            calls[0] += 1
            return FakeResp(body, url=url)
        stub = amz_lib.AmzSession.__new__(amz_lib.AmzSession)
        stub.s = types.SimpleNamespace(get=fake_get)
        stub._solve = solve
        stub._solve_captcha = solve
        r = amz_lib.AmzSession.get(stub, "/dp/B000000000")   # default tries=4
        return calls[0], r

    n, r = run("x bm-verify challenge", lambda text, url: True)      # interstitial keeps coming
    assert n == 4, f"interstitial path did not share the tries=4 cap (got {n} fetches)"
    assert r is not None
    n, r = run("validateCaptcha form", lambda text, url: True)       # captcha keeps coming
    assert n == 4, f"captcha path did not share the tries=4 cap (got {n} fetches)"
    def boom(text, url):
        raise RuntimeError("verify POST failed")
    n, r = run("x bm-verify challenge", boom)                        # solver itself fails
    assert n == 1 and r is not None, f"solver exception escaped or looped (n={n})"
    print("PASS test_challenge_retry_cap")


# ---------------------------------------------------------------- 5. max_pages
def test_max_pages():
    sig = inspect.signature(server.search_products)
    assert "max_pages" in sig.parameters, "max_pages missing from signature"
    assert sig.parameters["max_pages"].default == 1

    # cap: max_pages=99 fetches exactly 7 pages (fresh asins each page)
    sess = use_session(FakeSession(SEARCH_HTML, mode="unique"))
    try:
        obj = decoded(server.search_products("laptop", max_pages=99))
        assert len(sess.calls) == 7, f"max_pages=99 -> {len(sess.calls)} fetches (want 7)"
        assert obj["pages_fetched"] == 7
        pages = [p["page"] for _, p in sess.calls]
        assert pages == ["1", "2", "3", "4", "5", "6", "7"], pages
        n1 = 22  # canned page tile count (per demo self-check)
        assert obj["count"] == 7 * n1, f"aggregation wrong: {obj['count']} != {7 * n1}"
        assert obj["count_this_page"] == n1
        asins = [p["asin"] for p in obj["products"]]
        assert len(asins) == len(set(asins)), "dedupe by asin failed"
    finally:
        server._az = None

    # multi-page: max_pages=3 fetches 3 pages and aggregates
    sess = use_session(FakeSession(SEARCH_HTML, mode="unique"))
    try:
        obj = decoded(server.search_products("laptop", max_pages=3))
        assert len(sess.calls) == 3, f"max_pages=3 -> {len(sess.calls)} fetches"
        assert obj["count"] == 3 * 22 and obj["pages_fetched"] == 3
    finally:
        server._az = None

    # duplicate page (amazon ignoring ?page=) stops early instead of grinding
    sess = use_session(FakeSession(SEARCH_HTML, mode="same"))
    try:
        obj = decoded(server.search_products("laptop", max_pages=5))
        assert len(sess.calls) == 2, f"same-page stall not detected: {len(sess.calls)} fetches"
        assert obj["count"] == 22 and obj["pages_fetched"] == 2
    finally:
        server._az = None

    # page start respected + invalid max_pages rejected offline
    sess = use_session(FakeSession(SEARCH_HTML, mode="unique"))
    try:
        decoded(server.search_products("laptop", page=2, max_pages=2))
        assert [p["page"] for _, p in sess.calls] == ["2", "3"], sess.calls
        for bad in (0, -1, "2", None):
            obj = decoded(server.search_products("laptop", max_pages=bad))
            assert obj.get("error") == "INVALID_PARAM", f"max_pages={bad!r} -> {obj}"
        assert len(sess.calls) == 2, "invalid max_pages hit network"
    finally:
        server._az = None
    print("PASS test_max_pages")


# ---------------------------------------------------------------- 6. output clipping
def test_clip():
    assert server._clip("short") == "short"
    assert server._clip(None) is None
    big = "x" * 9000
    clipped = server._clip(big)
    assert len(clipped) < 6100 and clipped.startswith("x" * 6000) and "clipped" in clipped
    assert server._clip("x" * 6000) == "x" * 6000, "cap should not touch exactly-6000 strings"

    real_extract, real_rat, real_rev = server.pe.extract_product, server.rf.parse_ratings, server.rf.parse_reviews
    sess = use_session(FakeSession("body"))
    try:
        server.pe.extract_product = lambda html, asin=None: {
            "asin": asin, "title": "t", "description": "D" * 9000, "aplus": "A" * 9000}
        obj = decoded(server.get_product("B0G2B2QVLL"))
        assert len(obj["description"]) <= 6100, f"description not clipped: {len(obj['description'])}"
        assert len(obj["aplus"]) <= 6100, f"aplus not clipped: {len(obj['aplus'])}"
        assert obj["description"].endswith("[clipped 3000 chars]")

        sess.text = "x" * 200001   # get_reviews demands a >=200000b dp body
        server.rf.parse_ratings = lambda html: {"average": 4.0, "total_ratings": 10}
        server.rf.parse_reviews = lambda html: [{"body": "B" * 9000, "rating": 5}]
        obj = decoded(server.get_reviews("B0G2B2QVLL"))
        assert len(obj["top_reviews"][0]["body"]) <= 6100, "review body not clipped"
    finally:
        server.pe.extract_product, server.rf.parse_ratings, server.rf.parse_reviews = real_extract, real_rat, real_rev
        server._az = None
    print("PASS test_clip")


# ---------------------------------------------------------------- 7. fuzz: no exception escapes
def test_fuzz_no_exceptions():
    # rejected BEFORE network (0 fake calls each)
    offline = [
        (server.search_products, (None,), "INVALID_PARAM"),
        (server.search_products, ("",), "INVALID_PARAM"),
        (server.search_products, ("q" * 500,), "INVALID_PARAM"),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"page": 0}),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"page": -1}),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"page": 999999}),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"min_price": -100}),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"min_price": 999999999}),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"min_price": 5000, "max_price": 100}),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"sort": "garbage"}),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"category": "<script>"}),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"min_rating": 99}),
        (server.search_products, ("laptop",), "INVALID_PARAM", {"brands": [""]}),
        (server.discover_brands, ("",), "INVALID_PARAM"),
        (server.get_suggestions, ("",), "INVALID_PARAM"),
        (server.get_suggestions, ("q" * 500,), "INVALID_PARAM"),
        (server.get_reviews, ("",), "INVALID_ASIN"),
        (server.get_reviews, ("not-an-asin!",), "INVALID_ASIN"),
        (server.get_product, ("",), "INVALID_ASIN"),
        (server.get_delivery_info, ("<script>",), "INVALID_ASIN"),
        (server.get_all_reviews, ("B0G2B2QVLL",), "INVALID_PARAM", {"page": 0}),
        (server.set_pincode, ("12345",), "error"),
        (server.set_pincode, ("abcdef",), "error"),
    ]
    for entry in offline:
        tool, args, want = entry[0], entry[1], entry[2]
        kw = entry[3] if len(entry) > 3 else {}
        sess = use_session(FakeSession(SEARCH_HTML))
        try:
            obj = decoded(tool(*args, **kw))
            if want == "error":
                assert "error" in obj, f"{tool.__name__}{args}{kw} -> {obj}"
            else:
                assert obj.get("error") == want, f"{tool.__name__}{args}{kw} -> {obj}"
            assert not sess.calls, f"{tool.__name__}{args}{kw} hit network"
        finally:
            server._az = None

    # reaches the (fake) network and still returns valid JSON
    online = [
        (server.search_products, ("🔍 emoji 🚀 query",), {"max_pages": 2}),
        (server.search_products, ("laptop",), {"category": "electronics", "min_rating": 4.0}),
        (server.discover_brands, ("boat",), {}),
        (server.get_suggestions, ("wireless ear",), {}),
        # invalid star/sort vocab must still attempt + handle the response
        (server.get_all_reviews, ("B0G2B2QVLL",),
         {"sort_by": "garbage_sort", "filter_by_star": "garbage_star"}),
    ]
    for tool, args, kw in online:
        sess = use_session(FakeSession(SEARCH_HTML))
        try:
            obj = decoded(tool(*args, **kw))
            assert sess.calls, f"{tool.__name__} did not attempt the request"
            assert "traceback" not in json.dumps(obj).lower()
        finally:
            server._az = None

    # catch-all: a parser explosion still comes back as JSON
    sess = use_session(FakeSession(SEARCH_HTML))
    real = server.sp.parse_products
    try:
        server.sp.parse_products = lambda html: (_ for _ in ()).throw(ValueError("boom parse"))
        obj = decoded(server.search_products("laptop"))
        assert obj.get("error") == "EXCEPTION" and "boom parse" in obj["detail"], obj
    finally:
        server.sp.parse_products = real
        server._az = None
    print("PASS test_fuzz_no_exceptions")


# ---------------------------------------------------------------- 8. empty response body
def test_empty_response_body():
    for tool, args in [(server.search_products, ("laptop",)),
                       (server.get_product, ("B0G2B2QVLL",)),
                       (server.get_delivery_info, ("B0G2B2QVLL",)),
                       (server.get_suggestions, ("laptop",)),
                       (server.discover_brands, ("laptop",))]:
        sess = use_session(FakeSession(""))     # network returned an empty string body
        try:
            obj = decoded(tool(*args))
            assert obj.get("error") == "EMPTY_RESPONSE", f"{tool.__name__} -> {obj}"
            assert sess.calls, f"{tool.__name__} did not attempt the request"
        finally:
            server._az = None
    print("PASS test_empty_response_body")


# ---------------------------------------------------------------- 9. thread serialization
def test_thread_serialization():
    state = {"inside": 0, "overlap": 0}
    lock = threading.Lock()

    class SlowSession(FakeSession):
        def get(self, path, params=None, **kw):
            with lock:
                state["inside"] += 1
                if state["inside"] > 1:
                    state["overlap"] += 1
            time.sleep(0.05)                    # releases the GIL — overlap without a real lock
            with lock:
                state["inside"] -= 1
            return super().get(path, params, **kw)

    server._az = SlowSession(SEARCH_HTML)
    results = []
    try:
        threads = [threading.Thread(target=lambda: results.append(decoded(server.search_products("laptop"))))
                   for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert len(results) == 4, f"{len(results)}/4 tool calls returned"
        assert state["overlap"] == 0, f"concurrent network ops overlapped {state['overlap']}x"
    finally:
        server._az = None
    print("PASS test_thread_serialization")


# ---------------------------------------------------------------- 10. demo stays green
def test_demo_green():
    obj = decoded(server.demo())
    for k in ("search", "product", "reviews"):
        assert obj[k].get("ok") is True, f"demo[{k}] not ok: {obj[k]}"
    print("PASS test_demo_green")


# ---------------------------------------------------------------- 4b. rate-limit gate
def test_rate_limit_gate():
    # (a) a 503 robot-check page maps to structured RATE_LIMITED, not raw HTTP 503 / DOGPAGE
    class S:
        def get(self, path, params=None):
            return FakeResp("api-services-support@amazon.com robot check", status=503)
    try:
        server._get(S(), "/s")
        raise AssertionError("503 not mapped to RATE_LIMITED")
    except server.ToolError as e:
        assert e.code == "RATE_LIMITED" and e.kw.get("retry_after") == 45, e.__dict__
    # (b) sticky cooldown: a wall active longer than the wait cap fails fast, zero fetches
    saved = amz_lib._COOLDOWN_UNTIL
    try:
        amz_lib._COOLDOWN_UNTIL = time.time() + 5000
        calls = [0]
        stub = amz_lib.AmzSession.__new__(amz_lib.AmzSession)
        def fake_get(*a, **k):
            calls[0] += 1
            return FakeResp("ok")
        stub.s = types.SimpleNamespace(get=fake_get)
        try:
            amz_lib.AmzSession.get(stub, "/s")
            raise AssertionError("cooldown gate did not raise")
        except amz_lib.RateLimitError as e:
            assert e.retry_after > 0 and calls[0] == 0, (e.retry_after, calls[0])
        # (c) a 503 mid-flight marks the shared cooldown for every other caller
        amz_lib._COOLDOWN_UNTIL = 0.0
        stub.s = types.SimpleNamespace(get=lambda *a, **k: FakeResp(
            "api-services-support@amazon.com", status=503))
        real_sleep = amz_lib.time.sleep
        amz_lib.time.sleep = lambda s: None      # backoff sleeps are the point, not the test
        try:
            amz_lib.AmzSession.get(stub, "/s")
        finally:
            amz_lib.time.sleep = real_sleep
        assert amz_lib._COOLDOWN_UNTIL > time.time() + 30, "503 did not set shared cooldown"
    finally:
        amz_lib._COOLDOWN_UNTIL = saved
    print("PASS test_rate_limit_gate")


TESTS = [test_asin_normalize, test_invalid_asin_rejected_before_network,
         test_corrupted_cookie_recovery, test_challenge_retry_cap, test_rate_limit_gate,
         test_max_pages, test_clip, test_fuzz_no_exceptions, test_empty_response_body,
         test_thread_serialization, test_demo_green]


def main():
    server._relaxed_polite = lambda: None   # offline runs: no politeness sleeps
    for t in TESTS:
        t()
    print(f"ALL {len(TESTS)} HARDENING TESTS PASS")


if __name__ == "__main__":
    main()
