# Amazon MCP

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio-green)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

An MCP server for amazon.in — search, filter, read product pages, reviews, and pincode-based delivery info. **Zero configuration**: no API key, no login, no account, no environment variables.

**Works with:** any MCP client (Claude Desktop, Cursor, Codex, Hermes, etc.) · **Marketplace:** amazon.in · **Python:** 3.11+

## Tools

| Tool | What it does |
|---|---|
| `search_products` | Search with filters: price range, min rating, brands, sort order, category, free delivery, COD, condition, country of origin. Paginates across multiple pages (`max_pages`). |
| `discover_brands` | List brand filter tokens for a query (run before `search_products(brands=[...])`) |
| `get_product` | Full product page: title, price + MRP + savings, hi-res images, feature bullets, description, A+ content, spec details, variants (color/size), brand, seller, warranty, EMI, bank offers, exchange offer, breadcrumbs, size chart, frequently-bought-together. Accepts a bare ASIN **or a full amazon.in product URL**. |
| `get_reviews` | Ratings histogram (avg, total, per-star counts) + top reviews (rating, title, body, author, date, country, verified flag, helpful votes, images hi-res, variation) |
| `get_all_reviews` | Paginated review list with sort/star filters — requires an authenticated session (see Limits) |
| `set_pincode` | Set a 6-digit delivery PIN. Every later call in the session is personalized to it. Validates input before any network call. |
| `get_delivery_info` | Delivery estimate for a product at the current pincode ("FREE delivery Friday, 23 October" or non-deliverable) |
| `get_suggestions` | Related search suggestions for a query |
| `demo` | Offline self-check — runs parsers against bundled fixtures, no network |

## Quickstart

**One command, no clone** (needs [`uv`](https://docs.astral.sh/uv/)):

```bash
uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp
```

Or install locally (needs Python 3.11+):

```bash
git clone https://github.com/thenabbu/amazon-mcp.git
cd amazon-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/amazon-mcp          # starts on stdio
```

Self-check without network:

```bash
.venv/bin/python amazon_mcp/server.py --demo
```

```json
{"search": {"total": 65756, "items": 22, "ok": true}, "product": {"title": "HP Victus 15 Smartchoice...", "price": "₹1,08,990.00", "variants": 8, "dogpage": "DOGPAGE", "ok": true}, "reviews": {"dp1": {"n": 13, "avg": 4.0, "total": 95266}, "dp2": {"n": 8, "avg": 3.4, "total": 42}, "ok": true}, "pincode": {"validation": {"110001": true, "400001": true, "12345": false, "abcdef": false, "000000": false}}}
```

### Connect it to your MCP client

**Any client that speaks stdio** (Claude Desktop, Cursor, Cline, Windsurf, VS Code, Goose, …) — add via your client's MCP settings UI or config file:

```json
{
  "mcpServers": {
    "amazon": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/thenabbu/amazon-mcp", "amazon-mcp"]
    }
  }
}
```

Prefer a local checkout instead? Swap the command:

```json
{
  "mcpServers": {
    "amazon": {
      "command": "/path/to/amazon-mcp/.venv/bin/amazon-mcp"
    }
  }
}
```

**CLI clients** — one-liners:

```bash
# Claude Code
claude mcp add amazon uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp

# Codex
codex mcp add amazon -- uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp

# Hermes
hermes mcp add amazon --command uvx --connect-timeout 90 --args --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp
```

Windows note: for `uvx`-based entries run it directly (no `cmd /c` wrapper needed); if your client doesn't ship `uvx` on PATH, use `command: "cmd", args: ["/c", "uvx", "--from", "git+https://github.com/thenabbu/amazon-mcp", "amazon-mcp"]`.

Restart your client after editing, then look for the 9 `amazon` tools in its tool picker.

## Example

`get_product` on a phone URL:

```
title:    Redmi 15C 5G Prime Edition Dusk Purple 6GB + 128GB | Massive 6000mAh Battery | ...
price:    ₹17,999.00   MRP: ₹31,999   savings: -44%
brand:    Redmi        seller: Dawntech Etel!
rating:   4.0 (744)    availability: In stock
images:   11 hi-res    variants: 4 (2 colors x 2 storages)
emi:      EMI starts at ₹633 per month. No Cost EMI available
offers:   Cashback up to ₹539 (Amazon Pay ICICI) / No Cost EMI / Partner Offers
exchange: With Exchange up to 15,500 off
details:  40 spec keys   breadcrumbs: Electronics > Mobiles > Smartphones
```

`get_reviews` on the same product:

```
avg 4.0, 744 ratings, histogram 5*54% 4*21% 3*8% 2*3% 1*14%
8 top reviews, e.g. "Battery lasts me 2 days" — Aditi, 16 October 2026 (verified purchase)
```

## Example prompts

- "Search amazon.in for wireless earbuds under ₹2,000, sort by price, show the top 5 with ratings"
- "I'm in Delhi (110000). How long would delivery take for ASIN B0G2B2QVLL?"
- "Pull the full spec sheet and all reviews for this phone: <paste any amazon.in product URL>"
- "Compare the 3 cheapest 5G phones from search page 2 — storage, battery, review sentiment"
- "Find laptops with 4+ star ratings, discover which brands exist for 'gaming laptop', and filter to ASUS + Lenovo"

## How it survives Amazon's bot wall

Amazon sits behind Akamai Bot Manager. Plain HTTP clients get a 503 or a proof-of-work challenge page. The server uses `curl_cffi` (real browser TLS fingerprint) and solves both known challenges automatically:

- the `bm-verify` interstitial (compute the small proof-of-work, POST `/_sec/verify`, retry)
- the transient "Continue shopping" captcha form (single GET, retry)

Plus polite pacing (1.5–3 s between requests) so the server stays under the radar.

## Limits (honest)

- **Deep review pagination needs login.** Amazon redirects `/product-reviews/` to sign-in for anonymous visitors (re-verified Oct 2026). Without cookies you get the ~13 top reviews from the product page + the full ratings histogram. `get_all_reviews` accepts the params and returns parsed reviews if you drop authenticated cookies into the session.
- **Search results cap at ~100 pages** (Amazon's own ceiling; deeper pages return empty).
- **Prime badge / Climate Pledge** are not rendered on amazon.in search pages — nothing to extract.
- A+ content is sometimes an empty header when Amazon lazy-loads the rest.
- Rate-limit yourself: heavy bursts will trigger challenges (the server auto-solves, but don't hammer).

## Troubleshooting

- **Tools don't show up in the client** — restart the client after editing its MCP config; the server only connects on launch. Check `uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp` runs standalone first.
- **Occasional "challenge" or slow first request** — Amazon's bot checks (proof-of-work/captcha) are solved automatically; the first call also builds a session, so expect ~3-8 s. If requests start failing repeatedly, you're fetching too fast — the built-in 1.5-3 s pacing should prevent this.
- **`demo` fails after editing parsers** — `--demo` runs the parsers against bundled fixtures with no network; a failure means a parser change broke an assertion.

## Development

```bash
.venv/bin/python amazon_mcp/server.py --demo     # offline parser self-check
python test_hardening.py                          # input-validation asserts
```

Parser modules (`search_parse`, `product_extract`, `reviews_fetch`, `pincode_common`) are pure functions over saved HTML — fixtures under `amazon_mcp/`, so the demo runs offline.

## Project layout

```
amazon_mcp/
  server.py           # MCP tool layer (9 tools, input validation, thread-safe)
  amz_lib.py          # HTTP session: TLS fingerprinting + Akamai challenge solvers
  search_parse.py     # search-results parser (pure functions over HTML)
  product_extract.py  # product-page parser
  reviews_fetch.py    # reviews + ratings parser
  pincode_common.py   # delivery-pincode binding
  *_html/ probe_cache/  # saved page fixtures used by the offline self-check
test_hardening.py     # input-validation + resilience tests (no network)
```

## License

MIT — see [LICENSE](LICENSE).
