"""amazon.in pincode/glow — working code for the MCP server.

Flow: AmzSession -> ensure ubid-main -> GET any page (csrf) -> POST address-change
      -> (optional) GET get-location-label to read back -> fetch pages (personalized).

Why ubid-main: amazon.in (acbin cohort) never issues ubid-main to non-browser clients;
without it the address-change POST returns successful:1 but Sembu binds nothing and
every page stays on the IP-geolocation default. Self-minting one makes it stick.
"""
import re, json, random, sys, pathlib, html as H
# amz_lib lives in this package — never prepend the recon scratch dir (stale copies there
# shadowed this module when server.py imported it after reviews_fetch)
from amz_lib import AmzSession, BASE, polite

OUT = pathlib.Path("/opt/data/cache/scratch/amazon_recon")
CHANGE_URL = BASE + "/gp/delivery/ajax/address-change.html"
LABEL_URL = BASE + "/portal-migration/hz/glow/get-location-label"

def valid_pincode(pin):
    """6 digits, first digit 1-9. Rejects '12345'/'abcdef'/'000000' BEFORE any POST."""
    return bool(re.fullmatch(r"[1-9]\d{5}", str(pin).strip()))

def scrape_csrf(html):
    """anti-csrftoken-a2z lives in page HTML (<meta>), NOT in cookies."""
    m = re.search(r'name="anti-csrftoken-a2z"\s+content="([^"]+)"', html)
    return m.group(1) if m else None

def ensure_ubid(az):
    """Self-mint ubid-main (XXX-XXXXXXX-XXXXXX) if absent — REQUIRED for binding."""
    jar = dict(az.s.cookies.items())
    if "ubid-main" not in jar:
        az.s.cookies.update({"ubid-main": f"132-{random.randint(1000000, 9999999)}-{random.randint(100000, 999999)}"})
    return dict(az.s.cookies.items())["ubid-main"]

def set_pincode(az, pin, csrf, page_code="Search"):
    """POST glow address-change. Response JSON: isValidAddress/isAddressUpdated/sembuUpdated."""
    return az.s.post(CHANGE_URL, data={
        "locationType": "LOCATION_INPUT", "zipCode": str(pin),
        "deviceType": "web", "pageCode": page_code, "actionSource": "glow",
    }, headers={
        "anti-csrftoken-a2z": csrf, "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": BASE + "/", "Origin": BASE, "Accept": "*/*",
    })

def get_location_label(az, csrf, page_type="Search"):
    """Readback of server-side destination (Sembu): selectedDestination.destinationValue."""
    r = az.s.get(LABEL_URL, params={"storeContext": "NoStoreName", "pageType": page_type,
                                    "actionSource": "desktop-modal"},
                headers={"X-Requested-With": "XMLHttpRequest", "anti-csrftoken-a2z": csrf,
                         "Referer": BASE + "/", "Accept": "application/json"})
    return json.loads(r.text) if r.text.strip().startswith("{") else {}

def glow_location(html):
    """Nav glow lines. Anonymous-no-destination: ('Delivering to New D... 110001','Update location').
    Bound: ('Deliver to','New Delhi 110001&zwnj;') — line1/line2 swap roles."""
    def clean(s):  # strip tags + entity leftovers (zwnj leaked into output before)
        return H.unescape(re.sub(r"<[^>]+>", "", s)).replace("\u200c", "").strip()
    out = []
    for id_ in ("glow-ingress-line1", "glow-ingress-line2"):
        m = re.search(rf'id="{id_}">\s*(.*?)\s*</span>', html)
        out.append(clean(m.group(1)) if m else None)
    return tuple(out)

def delivery_info(html):
    """Per-product delivery text with the DATE intact: Amazon puts dates in nested
    <span class="a-text-bold">/<b>, so the match window must cross one closing tag before
    tags are stripped. Scripts stripped first — inline JS carries 'Currently unavailable'
    strings that would bleed into the window. Empty list = no promises rendered."""
    html = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    out = []
    anchors = [r"FREE [Dd]elivery", r"[Gg]et it by", r"Currently unavailable",
               r"This item cannot be shipped", r"cannot be delivered", r"[Nn]ot deliverable"]
    for a in anchors:
        for m in re.finditer(a, html):
            cuts = [x for x in (html.find(t, m.start()) for t in ("</span>", "</div>", "</b>", "</strong>")) if x > 0]
            end = min(cuts + [m.start() + 200])
            t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[m.start():end])).strip(" .")
            if t and t not in out:
                out.append(t)
    return out[:12]

def dp_zipcode(html):
    m = re.search(r'"zipCode":"(\d{6})"', html)
    return m.group(1) if m else None

def save_jar(az, name="cookies.json"):
    (OUT / name).write_text(json.dumps(dict(az.s.cookies.items()), indent=1))

def load_jar(az, name="cookies.json"):
    p = OUT / name
    if p.exists():
        az.s.cookies.update(json.loads(p.read_text()))

if __name__ == "__main__":  # self-check: set pin, verify binding via dp page
    pin = sys.argv[1] if len(sys.argv) > 1 else "110001"
    az = AmzSession()
    load_jar(az)
    ubid = ensure_ubid(az)
    polite()
    pg = az.get("/s?k=laptop")
    csrf = scrape_csrf(pg.text)
    print("before:", glow_location(pg.text))
    polite()
    r = set_pincode(az, pin, csrf)
    j = r.json() if "json" in (r.headers.get("content-type") or "") else {}
    print("POST:", {k: j.get(k) for k in ("isValidAddress", "isAddressUpdated", "sembuUpdated", "successful")},
          "| address:", (j.get("address") or {}).get("city"), (j.get("address") or {}).get("zipCode"))
    polite()
    lab = get_location_label(az, csrf)
    assert str(lab.get("selectedDestination", {}).get("destinationValue")) == str(pin), "binding failed"
    print("label:", lab.get("selectedDestination"))
    polite()
    dp = az.get("/dp/B0D2642MBF")
    print("dp zip:", dp_zipcode(dp.text), "| glow:", glow_location(dp.text), "| delivery:", delivery_info(dp.text)[:6])
