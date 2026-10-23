"""amazon.in reviews extraction — working recipe for the MCP (no browser, no login).

Established by recon (2026-10, amazon.in):
- ALL dedicated review-LIST pages are login-gated (302 -> /ap/signin), for every param combo:
  /product-reviews/<ASIN>/ (pageNumber, sortBy, filterByStar, reviewerType, formatType),
  /portal/customer-reviews/<ASIN>/ (the new path the dp histogram links to), /gp/aw/reviews/<ASIN>.
- The dp page /dp/<ASIN> needs NO login and embeds: top-reviews batch (8-13 reviews, full fields),
  the full star histogram, avg rating and total count.
- Internal ajax endpoints are useless w/o login: /hz/reviews-render/ajax/medley-reviews/get/ -> 404
  (GET+POST, ±/portal-migration prefix); /portal/customer-reviews/ajax/lazy-widgets/stream and
  /hz/reviews-render/ajax/lazy-widgets/stream -> 200 with EMPTY body (args JSON + csrf tried both
  encodings). cr-top-reviews JS bundle (91dYm4fdMXL.js) contains no pagination fetch at all.
So: primary source = dp page. Paginated/deep history requires authenticated cookies (out of scope).
"""
import sys, re, json, os, tempfile, html as H
# amz_lib lives in this package — never prepend the recon scratch dir (it shadowed
# pincode_common/reviews_fetch with stale recon copies when imported via server.py)
from amz_lib import AmzSession, polite

BASE = "https://www.amazon.in"
LIST_PATHS = {
    "product-reviews": "/product-reviews/{asin}/",
    "portal":          "/portal/customer-reviews/{asin}/",
    "legacy_mweb":     "/gp/aw/reviews/{asin}",
}
# Param vocab (valid on the gated pages; filterByStar values are confirmed by the dp histogram hrefs):
SORT_BY = {"recent": "newest first", "helpful": "most helpful", "top": "default top reviews"}
FILTER_BY_STAR = ["one_star", "two_star", "three_star", "four_star", "five_star",
                 "positive", "critical", "all_stars"]  # positive=4-5 star, critical=1-3 star
REVIEWER_TYPE = ["all_reviews", "avp_only_reviews"]


class AmazonReviews:
    def __init__(self):
        self.s = AmzSession()

    def dp_html(self, asin):
        r = self.s.get(f"{BASE}/dp/{asin}")
        if r.status_code != 200 or len(r.text) < 200000:
            raise RuntimeError(f"dp fetch failed: {r.status_code} {len(r.text)}b")
        return r.text

    def top_reviews(self, asin):
        """No-login review source: the dp 'Top reviews' batch. ~8-13 reviews, one batch only."""
        return parse_reviews(self.dp_html(asin))

    def ratings(self, asin):
        return parse_ratings(self.dp_html(asin))

    def product(self, asin):
        h = self.dp_html(asin)
        return {"asin": asin, "ratings": parse_ratings(h), "top_reviews": parse_reviews(h)}

    def list_page(self, asin, pageNumber=1, sortBy=None, filterByStar=None,
                  reviewerType=None, formatType=None, path="product-reviews"):
        """Dedicated list/pagination path. 2026 reality: login-gated for every combo.
        Returns {'gated': True, ...}; with an authenticated session this same call would
        return parsed reviews (parse_reviews works on list HTML too, hooks are shared)."""
        params = {"pageNumber": str(pageNumber)}
        if sortBy:        params["sortBy"] = sortBy
        if filterByStar:  params["filterByStar"] = filterByStar
        if reviewerType: params["reviewerType"] = reviewerType
        if formatType:    params["formatType"] = formatType
        r = self.s.get(BASE + LIST_PATHS[path].format(asin=asin), params=params)
        gated = "/ap/signin" in str(r.url) or ("Sign in or create account" in r.text[:60000])
        if not gated:
            return {"gated": False, "reviews": parse_reviews(r.text),
                    "ratings": parse_ratings(r.text)}
        return {"gated": True, "signin_url": str(r.url)[:250], "asin": asin, "params": params}


# ---------------------------------------------------------------- parsing (dp + list share hooks)
def _txt(s):
    return H.unescape(re.sub(r"\s+", " ", s or "")).strip()

def _body(blk):
    """Review text with the tag/attr run stripped correctly.
    Card-deck dp markup (2026): text sits inside reviewRichContentContainer, AFTER two
    hidden teaser divs ('Brief content visible, double tap to read full content.') — starting
    the slice at the data-hook attribute itself used to leak attrs + teaser into the body.
    Classic markup: text inside the reviewText span. Start after the opening '>' in both."""
    a = blk.find('data-hook="reviewRichContentContainer"')
    if a < 0:
        a = blk.find('data-hook="reviewText"')
    if a < 0:
        return ""
    gt = blk.find(">", a)
    a = gt + 1 if gt > 0 else a
    b = blk.find('reviewExpandButtonContainer', a)
    if b > 0:
        lt = blk.rfind("<", a, b)          # expand button starts mid-tag at the marker; cut at its '<'
        b = lt if lt > 0 else b
    body = re.sub(r"<br\s*/?>", "\n", blk[a:b if b > 0 else a + 30000])
    t = _txt(re.sub(r"<[^>]+>", " ", body))
    t = re.sub(r"^(?:Brief|Full) content visible, double tap to read (?:full|brief) content\.\s*", "", t)
    return re.sub(r"\s*(?:Read more|Read less)\s*$", "", t).strip()

def parse_reviews(html_text):
    """Per-review fields via data-hooks (identical hooks on dp top-reviews and on the
    login-gated list pages): review(ReviewTitle)/review-star-rating/reviewTitle/
    review-by-line+review-date/avp-badge/format-strip/reviewText/helpful-vote-statement/
    review-image-tile + a-profile author."""
    revs = []
    pos = [m.start() for m in re.finditer(r'data-hook="review"\s', html_text)]
    for k, start in enumerate(pos):
        blk = html_text[start: pos[k + 1] if k + 1 < len(pos) else start + 60000]
        r = {}
        m = re.search(r'/portal/customer-reviews/srp/-/([A-Z0-9]{10,14})', blk) or \
            re.search(r'customer_review-([A-Z0-9]{10,14})', blk)
        r["review_id"] = m and m.group(1)
        m = re.search(r'data-hook="review-star-rating"[^>]*>.*?<span class="a-icon-alt">([\d.]+) out of 5', blk, re.S)
        r["rating"] = float(m.group(1)) if m else None
        m = re.search(r'data-hook="reviewTitle"[^>]*>([^<]+)', blk)
        r["title"] = _txt(m.group(1)) if m else ""
        m = re.search(r'<a[^>]+href="(/gp/profile/[^"]+)"[^>]*class="a-profile[^"]*".*?a-profile-name">([^<]*)<', blk, re.S)
        r["author"] = _txt(m and m.group(2) or "")
        r["author_profile"] = (BASE + m.group(1).split("?")[0]) if m else None
        m = re.search(r'Reviewed in (.*?) on ([^<]+)</span>', blk)
        r["country"] = _txt(m and m.group(1) or "")
        r["date"] = _txt(m and m.group(2) or "")
        r["verified_purchase"] = 'data-hook="avp-badge"' in blk
        m = re.search(r'data-hook="format-strip"[^>]*>.*?<span[^>]*>([^<]+)<', blk, re.S)
        r["variation"] = _txt(m and m.group(1) or "")
        r["body"] = _body(blk)
        m = re.search(r'data-hook="helpful-vote-statement"[^>]*>([^<]+)<', blk)
        if not m:
            r["helpful_count"] = 0
        else:
            t = m.group(1)
            n = re.search(r"(\d[\d,]*)", t)
            r["helpful_count"] = 1 if "One person" in t else (int(n.group(1).replace(",", "")) if n else 0)
        imgs = [g.group(1) for im in re.findall(r'<img[^>]*data-hook="review-image-tile"[^>]*>', blk)
                if (g := re.search(r'src="([^"]+)"', im))]
        r["images"] = imgs
        # thumbs are _S<X><N>_ scaled; drop the size suffix for the full-res original
        r["images_hi_res"] = [re.sub(r"\._S[A-Z]\d+_", "", u) for u in imgs]
        r["has_image"] = bool(r["images"])
        revs.append(r)
    return revs

def parse_ratings(html_text):
    """Histogram via #histogramTable rows (aria-label), avg via #acrPopover title,
    total via #acrCustomerReviewText '(95,266)' (Indian digit grouping)."""
    hist = {int(s): int(p) for p, s in re.findall(r'aria-label="(\d+) percent of reviews have (\d) stars"', html_text)}
    m = re.search(r'id="acrPopover"[^>]*title="([\d.]+) out of 5', html_text)
    avg = float(m.group(1)) if m else None
    m = re.search(r'id="acrCustomerReviewText"[^>]*>\s*\(?([\d,]+)\)?<', html_text)
    total = int(m.group(1).replace(",", "")) if m else 0
    return {"average": avg, "total_ratings": total,
            "histogram_percent": hist,
            "histogram_counts": {k: round(v * total / 100) for k, v in hist.items()}}

def parse_ratings_extras(html_text):
    """dp-section extras beyond the histogram (which is NOT duplicated here —
    star distribution IS #histogramTable, already in parse_ratings):
    - reviews_with_images: distinct review ids inside #cm_cr_carousel_images_section
      (the 'Reviews with images' carousel; None when the widget is absent)
    - mention_chips: AI keyword chips 'N customers mention <theme>, P positive, M negative'."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "lxml")
    rwim = None
    sec = soup.select_one("#cm_cr_carousel_images_section")
    if sec is not None:
        ids = {el.get("data-reviewid") for el in sec.find_all(attrs={"data-reviewid": True})} - {None}
        rwim = len(ids) or None
    chips, seen = [], set()
    for n, theme, pos, neg in re.findall(
            r'(\d+) customers mention ([^,"<]{2,40}), (\d+) positive, (\d+) negative', html_text):
        if theme in seen:
            continue
        seen.add(theme)
        chips.append({"theme": theme.strip(), "mentions": int(n),
                      "positive": int(pos), "negative": int(neg)})
    return {"reviews_with_images": rwim, "mention_chips": chips}


def demo():
    """Self-check against recon-saved HTML (no requests). Run live with an ASIN arg to fetch."""
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tests", "fixtures", "reviews")
    for f, n_rev, avg, total, h5 in [("dp.html", 13, 4.0, 95266, 54), ("dp2.html", 8, 3.4, 42, 50)]:
        h = open(os.path.join(d, f), encoding="utf-8").read()
        revs = parse_reviews(h)
        rat = parse_ratings(h)
        assert len(revs) == n_rev, (f, len(revs))
        assert all(x["rating"] and x["title"] and x["date"] for x in revs), f
        # body quality: real text, no attr/teaser leakage (2026 card-deck markup)
        assert all(x["body"] and len(x["body"]) >= 3 and "Brief content" not in x["body"]
                   and "data-hook=" not in x["body"] and "div data-csa" not in x["body"] for x in revs), f
        assert all(not re.search(r"\._S[A-Z]\d+_", u) for x in revs for u in x["images_hi_res"]), f
        assert rat["average"] == avg and rat["total_ratings"] == total, (f, rat)
        assert rat["histogram_percent"][5] == h5, rat
        assert revs[0]["author"] and revs[0]["country"] and "review_id" in revs[0]
        print(f"{f}: {len(revs)} reviews, avg {rat['average']}, total {rat['total_ratings']:,}, "
              f"hist {rat['histogram_percent']}")
        print("  first:", json.dumps({k: revs[0][k] for k in
              ("review_id", "rating", "title", "author", "country", "date", "verified_purchase", "helpful_count")}))
        print("  titles:", [x["title"][:45] for x in revs[:4]])
        print("  with images:", sum(x["has_image"] for x in revs), "| verified:", sum(x["verified_purchase"] for x in revs))
    # evidence file for the caller
    h = open(os.path.join(d, "dp.html"), encoding="utf-8").read()
    json.dump({"ratings": parse_ratings(h), "top_reviews": parse_reviews(h)},
              open(os.path.join(tempfile.gettempdir(), "reviews_dump.json"), "w"), indent=1)
    print("wrote reviews_dump.json")

if __name__ == "__main__":
    if len(sys.argv) > 1:  # live: fetch a product's no-login review surface
        cl = AmazonReviews()
        print(json.dumps(cl.product(sys.argv[1]), indent=1)[:2000])
        polite()
        print(json.dumps(cl.list_page(sys.argv[1], pageNumber=2, sortBy="recent"), indent=1))
    else:
        demo()
