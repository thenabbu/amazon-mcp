# Amazon MCP

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio%20%7C%20http-green)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

An MCP server for **amazon.in** — search with full filters, read product pages, reviews, and pincode-based delivery info. **Zero configuration**: no API key, no login, no account, no environment variables.

Works over **stdio** with any local agent (Claude, Codex, Cursor, Gemini, …) and over **HTTP** for web connectors (ChatGPT, Claude.ai).

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
| `demo` | Offline self-check — runs the parsers against bundled sample pages, no network |

## Install

One command, no clone (needs [`uv`](https://docs.astral.sh/uv/)):

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

Verify the install (offline, no network):

```bash
.venv/bin/amazon-mcp --demo
```

```json
{"search": {"total": 65756, "items": 22, "ok": true}, "product": {"title": "HP Victus 15 Smartchoice...", "price": "₹1,08,990.00", "variants": 8, "dogpage": "DOGPAGE", "ok": true}, "reviews": {"dp1": {"n": 13, "avg": 4.0, "total": 95266}, "dp2": {"n": 8, "avg": 3.4, "total": 42}, "ok": true}, "pincode": {"validation": {"110001": true, "400001": true, "12345": false, "abcdef": false, "000000": false}}}
```

## Connect it (stdio — local agents)

The same entry works in every stdio client. Replace `<URL>` with `git+https://github.com/thenabbu/amazon-mcp`.

**Claude Desktop / Cline / Windsurf / Gemini CLI** — add to their MCP config file (`claude_desktop_config.json`, `cline_mcp_settings.json`, `.codeium/windsurf/mcp_config.json`, `~/.gemini/settings.json`):

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

**VS Code / Copilot** — `.vscode/mcp.json`:

```json
{
  "servers": {
    "amazon": { "type": "stdio", "command": "uvx", "args": ["--from", "git+https://github.com/thenabbu/amazon-mcp", "amazon-mcp"] }
  }
}
```

**CLI one-liners:**

```bash
# Claude Code
claude mcp add amazon uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp

# Codex
codex mcp add amazon -- uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp

# Hermes
hermes mcp add amazon --command uvx --connect-timeout 90 --args --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp
```

**Cursor / Goose** — Settings → MCP (Cursor) or Extensions → Add custom extension (Goose): command `uvx`, args `--from git+https://github.com/thenabbu/amazon-mcp amazon-mcp`.

Prefer a local checkout? Swap the command for `/path/to/amazon-mcp/.venv/bin/amazon-mcp`.

Windows: if your client doesn't put `uvx` on PATH, wrap it: `"command": "cmd", "args": ["/c", "uvx", "--from", "git+https://github.com/thenabbu/amazon-mcp", "amazon-mcp"]`.

Restart the client after editing — then look for the 9 `amazon` tools in its tool picker.

## Web connectors (ChatGPT, Claude.ai)

Web connectors need an **HTTP** endpoint instead of stdio. The server speaks Streamable HTTP on `/mcp`:

```bash
uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp --http        # 127.0.0.1:8787
uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp --http --port 9000
```

**Local (ChatGPT Developer mode):** ChatGPT → Settings → Connectors → enable Developer mode → add `http://localhost:8787/mcp`.

**Public URL (any plan, both Claude.ai and ChatGPT):** expose the port with a tunnel, then add the URL as a custom connector:

```bash
cloudflared tunnel --url http://localhost:8787     # or: ngrok http 8787
```

- **Claude.ai** → Settings → Connectors → Add custom connector → `https://<your-tunnel>/mcp`
- **ChatGPT** → Settings → Connectors → Add custom connector → `https://<your-tunnel>/mcp`
- **Claude Desktop** → Settings → Connectors → Add custom connector → same URL

> ⚠ The HTTP endpoint is **unauthenticated** — anyone who has the URL can query through it. Use a tunnel you keep to yourself, or put it behind Cloudflare Access / a tunnel with authentication if you expose it beyond localhost.

## Example

`get_product` on a product URL:

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

Plus polite pacing (2.5–4.5 s between requests) and a shared cooldown: if amazon rate-limits you, tools return a structured `RATE_LIMITED` error with a `retry_after` instead of hammering the wall.

## Limits (honest)

- **Deep review pagination needs login.** Amazon redirects `/product-reviews/` to sign-in for anonymous visitors (re-verified Oct 2026). Without cookies you get the ~13 top reviews from the product page + the full ratings histogram. `get_all_reviews` accepts the params and returns parsed reviews if you drop authenticated cookies into the session.
- **Search results cap at ~100 pages** (Amazon's own ceiling; deeper pages return empty).
- **Prime badge / Climate Pledge** are not rendered on amazon.in search pages — nothing to extract.
- A+ content is sometimes an empty header when Amazon lazy-loads the rest.
- Heavy bursts trigger rate limits (handled gracefully — see above — but don't hammer).

## Troubleshooting

- **Tools don't show up in the client** — restart the client after editing its MCP config; the server only connects on launch. Check `uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp` runs standalone first.
- **`RATE_LIMITED` errors** — amazon's robot-check wall; wait ~45–60 s and retry. It usually clears itself; the server already backs off for you.
- **Slow first request** — the first call builds a session and may solve a challenge (~3–8 s).
- **`--demo` fails** — the bundled offline self-check found a parser regression; an update of the repo likely broke an assertion.

## Project layout

```
amazon_mcp/
  server.py           # MCP tool layer (9 tools, input validation, thread-safe, stdio + http)
  amz_lib.py          # HTTP session: TLS fingerprinting + Akamai challenge solvers
  search_parse.py     # search-results parser (pure functions over HTML)
  product_extract.py  # product-page parser
  reviews_fetch.py    # reviews + ratings parser
  pincode_common.py   # delivery-pincode binding
  */ fixtures         # saved amazon.in pages used by the offline demo self-check
```

## License

MIT — see [LICENSE](LICENSE).
