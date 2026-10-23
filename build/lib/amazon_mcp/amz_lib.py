"""Shared amazon.in probe lib: session with Akamai interstitial solver."""
import re, time, random
from curl_cffi import requests

BASE = "https://www.amazon.in"
VENV_PY = "/opt/data/cache/scratch/amzvenv/bin/python"

# Sticky rate-wall gate: once amazon 503s, EVERY later call waits it out instead
# of re-hammering (eval 2026-10-23: concurrent callers each burned 4 retries and
# re-triggered the wall for minutes; one shared cooldown fixes the cascade).
_COOLDOWN_UNTIL = 0.0
_COOLDOWN_SECS = 45
_MAX_WAIT = 90          # longer waits fail fast with RateLimitError instead of blocking the tool call

class RateLimitError(RuntimeError):
    """Amazon rate wall active for longer than we're willing to block."""
    def __init__(self, retry_after):
        self.retry_after = int(retry_after)
        super().__init__(f"RATE_LIMITED: amazon robot-check wall active, retry after {self.retry_after}s")


class AmzSession:
    def __init__(self):
        self.s = requests.Session(impersonate="chrome131")
        self.s.headers.update({
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        })

    def _solve(self, text, url):
        m = re.search(r'var i = (\d+); var j = i \+ Number\("(\d+)" \+ "(\d+)"\)', text)
        if not m:
            return False
        pow_val = int(m.group(1)) + int(m.group(2) + m.group(3))
        mt = re.search(r'JSON.stringify\(\{"bm-verify": "([^"]+)"', text)
        if not mt:
            return False
        vr = self.s.post(
            f"{BASE}/_sec/verify?provider=interstitial",
            json={"bm-verify": mt.group(1), "pow": pow_val},
            headers={"Content-Type": "application/json", "Referer": url, "Origin": BASE},
            timeout=25,
        )
        print("  [verify]", vr.status_code, vr.text[:80])
        return True

    def _solve_captcha(self, html, url):
        # transient 'Continue shopping' GET-form captcha (no image puzzle)
        if 'validateCaptcha' not in html:
            return False
        import urllib.parse, html as _h
        g = lambda n: (re.search(rf'name="{n}" value="([^"]+)"', html) or [None, None])[1]
        amzn, amznr, fk = g("amzn"), g("amzn-r"), g("field-keywords")
        if not amzn:
            return False
        amznr = _h.unescape(amznr or "")
        time.sleep(random.uniform(1.0, 2.0))
        self.s.get(BASE + "/errors_page/validateCaptcha",
                   params={"amzn": amzn, "amzn-r": amznr, "field-keywords": fk},
                   headers={"Referer": url}, timeout=25)
        print("  [captcha solved]")
        return True

    def get(self, path, params=None, tries=4, **kw):
        """GET with Akamai challenge solving. ONE shared `tries` cap covers the
        bm-verify interstitial, the validateCaptcha form and the 503 robot-check —
        a challenge amazon keeps re-serving returns the last response after `tries`."""
        global _COOLDOWN_UNTIL
        # sticky gate: wait out an active rate wall (fail fast if it's too long)
        wait = _COOLDOWN_UNTIL - time.time()
        if wait > 0:
            if wait > _MAX_WAIT:
                raise RateLimitError(wait)
            time.sleep(wait)
        url = path if path.startswith("http") else BASE + path
        for attempt in range(tries):
            r = self.s.get(url, params=params, timeout=25, **kw)
            if "bm-verify" in r.text and len(r.text) < 5000:
                try:
                    if self._solve(r.text, str(r.url)):
                        continue
                except Exception as e:
                    print("  [verify failed]", e)
            if "validateCaptcha" in r.text and len(r.text) < 20000:
                try:
                    if self._solve_captcha(r.text, str(r.url)):
                        continue
                except Exception as e:
                    print("  [captcha failed]", e)
            if r.status_code == 503 and "api-services-support@amazon" in r.text:
                # robot-check wall: mark cooldown for ALL callers + back off hard
                # (eval: 5/10/15/20s burns the whole budget in ~50s while the
                # wall lasts minutes — real cool-downs are 1-15 min)
                _COOLDOWN_UNTIL = time.time() + _COOLDOWN_SECS
                print("  [503 robot-check, cooling down]")
                if attempt < tries - 1:
                    time.sleep([20, 40, 60][min(attempt, 2)])
                continue
            return r
        return r

    def post(self, path, **kw):
        url = path if path.startswith("http") else BASE + path
        kw.setdefault("timeout", 25)
        return self.s.post(url, **kw)

def polite():
    time.sleep(random.uniform(1.5, 3.0))
