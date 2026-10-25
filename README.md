# Amazon MCP

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio%20%7C%20http-green)](https://modelcontextprotocol.io)
[![CI](https://github.com/thenabbu/amazon-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/thenabbu/amazon-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

An MCP server for **amazon.in** — search with full filters, read product pages, reviews, and pincode-based delivery info. **Zero configuration**: no API key, no login, no account, no environment variables.

Works over **stdio** with any local agent (Claude, Codex, Cursor, Gemini, …) and over **HTTP** for web connectors (ChatGPT, Claude.ai).

## Tools

| Tool | What it does |
|---|---|
| `search_products` | Search with full filters — price, rating, brand, sort, category, delivery, COD — across pages |
| `discover_brands` | List brand filter tokens for a query (use before filtering by brand) |
| `get_product` | Everything on a product page: price, images, specs, variants, offers, seller, warranty. ASIN or URL |
| `get_reviews` | Ratings histogram + top reviews, no login needed |
| `get_all_reviews` | Paginated reviews with sort/star filters (login required — see Limits) |
| `set_pincode` | Set the 6-digit delivery PIN for the whole session |
| `get_delivery_info` | Delivery estimate for a product; optional per-call `pincode` |
| `get_suggestions` | Get related searches for a query |
| `search_best_value` | Search ranked by a transparent value score (formula included in the response) |
| `compare_products` | Compare up to 5 products side by side; flags missing fields instead of guessing |
| `get_price_history` | Months of price history + a buy/hold verdict from pricehistory.app (cached) |

## Install

One command, no clone (needs [`uv`](https://docs.astral.sh/uv/)):

```bash
uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp
```

**Pinned / stable** (recommended for configs you don't want moving under you — releases are tagged):

```bash
uvx --from git+https://github.com/thenabbu/amazon-mcp@v0.3.0 amazon-mcp
```

The untagged command always runs the latest `master`; the tagged one installs exactly that release.

Or install locally (needs Python 3.11+):

```bash
git clone https://github.com/thenabbu/amazon-mcp.git
cd amazon-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/amazon-mcp          # starts on stdio
```

Verify the install (prints the server handshake):

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}' | uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp
```

```json
{"jsonrpc":"2.0","id":1,"result":{"capabilities":{"prompts":{"listChanged":false},"resources":{"listChanged":false,"subscribe":false},"tools":{"listChanged":false}},"protocolVersion":"2025-06-18","serverInfo":{"name":"amazon-in","version":"0.3.0"}}}
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

Restart the client after editing — then look for the 11 `amazon` tools in its tool picker.

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

### Locking the endpoint (recommended whenever you tunnel)

Set a token before starting — every request must then carry `Authorization: Bearer <token>` or it gets **HTTP 401**:

```bash
export AMAZON_MCP_TOKEN="$(openssl rand -hex 24)"
export AMAZON_MCP_PUBLIC_URL="https://<your-tunnel>/mcp"   # optional, metadata only
uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp --http
```

- In a connector UI that supports **custom HTTP headers**, add `Authorization: Bearer <token>`.
- Token unset → the endpoint stays open (the server prints a startup warning). An obscure URL is not authentication: for anything beyond localhost, either set `AMAZON_MCP_TOKEN` or front the tunnel with Cloudflare Access / your tunnel provider's auth.

## Example

Real responses from the MCP (large arrays trimmed with `…`):

<details>
<summary><code>get_product on a product URL</code></summary>

```json
{
  "asin": "B0G2B2QVLL",
  "url": "https://www.amazon.in/dp/B0G2B2QVLL",
  "title": "Redmi 15C 5G Prime Edition Dusk Purple 6GB + 128GB | Massive 6000mAh Battery | Segment's Largest 17.53cm Display Up to 120Hz | MediaTek Dimensity 6300 | 33W Fast Charging | 50MP AI Dual Camera",
  "brand": "Redmi",
  "price": "₹17,999.00",
  "price_value": 17999.0,
  "list_price": "₹31,999",
  "savings_pct": "-44%",
  "images": [
    "https://m.media-amazon.com/images/I/711q7oij2qL._SL1500_.jpg",
    "https://m.media-amazon.com/images/I/61Uqw0zt8jL._SL1500_.jpg",
    "… (9 more)"
  ],
  "image_thumbs": [
    "https://m.media-amazon.com/images/I/41+KQ4UFUkL._SX38_SY50_CR,0,0,38,50_.jpg",
    "https://m.media-amazon.com/images/I/31LAVqojtpL._SX38_SY50_CR,0,0,38,50_.jpg",
    "… (9 more)"
  ],
  "bullets": [
    "Royale Design",
    "Massive 6000mAh Battery",
    "… (5 more)"
  ],
  "description": null,
  "aplus": "From the manufacturer Previous page Next page Previous page Next page Previous page Next page Previous page Next page",
  "details": {
    "Operating System": "Android 15, Xiaomi HyperOS",
    "RAM Memory Installed": "6 GB",
    "Processor Series": "Mediatek Dimensity 6300",
    "Memory Storage Capacity": "128 GB",
    "Colour": "Dusk Purple",
    "SIM Card Slot Count": "Dual SIM",
    "Connector Type": "USB Type C",
    "Form Factor": "Bar",
    "Sim Card Size": "Nano",
    "Water Resistance Level": "Water Resistant",
    "Headphones Jack": "[Unknown]",
    "Flash Memory Supported Size Maximum": "128 GB",
    "Rear Facing Camera Photo Sensor Resolution": "50 MP",
    "Number of Rear Facing Cameras": "2",
    "Battery Power": "6000",
    "Item Weight Unit of Measure": "212 Grams",
    "Item Dimensions": "17.2 x 7.9 x 0.8 Centimeters",
    "Frame Rate": "30fps, 60fps",
    "GPS Geotagging Functionality": "False",
    "Screen Size Unit of Measure": "6.9 Inches",
    "Resolution": "720 x 1560",
    "Refresh Rate": "120",
    "Display Type": "LCD",
    "Maximum Display Resolution": "2340 Pixels",
    "Wireless Provider": "Unlocked for All Carriers",
    "Cellular Technology": "5G",
    "Network Connectivity Technology": "5G",
    "Brand Name": "Redmi",
    "Country of Origin": "India",
    "Box Contents": "Charger, cable, SIM eject tool, Warranty card, User guide, Protective case",
    "Warranty Description": "1 year manufacturer warranty for device and 6 months manufacturer warranty for in-box accessories including batteries from the date of purchase",
    "Manufacturer": "BYD INDIA PRIVATE LIMITED, DEVELOPED PLOT NO.OZ-7,SIPCOT ORAGADAM, SRIPERUMBUDUR (TK) KANCHEEPURAM DISTRICT 602105 TAMIL NADU., Redmi",
    "Unit Count": "1.0 Pack",
    "Importer Contact Information": "Xiaomi Technology India Private Limited, Building Orchid. Block E, Embassy Tech Village, Marathahalli Outer Ring Road, Devarabisanahalli, Bengaluru 560103",
    "Item Type Name": "Smartphone",
    "Manufacturer Contact Information": "BYD INDIA PRIVATE LIMITED, DEVELOPED PLOT NO.OZ-7,SIPCOT ORAGADAM, SRIPERUMBUDUR (TK) KANCHEEPURAM DISTRICT 602105 TAMIL NADU.",
    "Packer Contact Information": "BYD INDIA PRIVATE LIMITED, DEVELOPED PLOT NO.OZ-7,SIPCOT ORAGADAM, SRIPERUMBUDUR (TK) KANCHEEPURAM DISTRICT 602105 TAMIL NADU.",
    "Best Sellers Rank": "#10 in Electronics ( See Top 100 in Electronics ) #4 in Smartphones",
    "ASIN": "B0G2B2QVLL",
    "Customer Reviews": "3.9 3.9 out of 5 stars (812) 3.9 out of 5 stars",
    "Brand": "Redmi",
    "RAM Memory Installed Size": "6 GB",
    "CPU Model": "Mediatek Dimensity 6300"
  },
  "variants": {
    "dimensions": [
      "color_name",
      "size_name"
    ],
    "items": [
      {
        "asin": "B0G2B4JX1F",
        "values": [
          "Midnight Black",
          "4GB+128GB"
        ],
        "current": false
      },
      {
        "asin": "B0G2BBWKCP",
        "values": [
          "Midnight Black",
          "6GB+128GB"
        ],
        "current": false
      },
      "… (4 more)"
    ]
  },
  "availability": "In stock",
  "rating": 3.9,
  "review_count": 812,
  "coupon": null,
  "promo_offers": null,
  "breadcrumbs": [
    "Electronics",
    "Mobiles & Accessories",
    "… (2 more)"
  ],
  "emi": "EMI starts at ₹633 per month. EMI starts at ₹633. No Cost EMI available EMI options",
  "offers": [
    "Cashback Upto ₹539.00 cashback as Amazon Pay Balance when you pay with Amazon Pay ICICI Bank Credit Cards 1 offer Cashback No Cost EMI Upto ₹469.22 EMI interest savings on Amazon Pay ICICI Bank Credit Cards 1 offer No Cost EMI Partner Offers Get GST invoice and save up to 18% on business purchases. Sign up for free"
  ],
  "exchange_offer": "With Exchange Up to 15,500.00 off You Pay Includes exchange service fee of Rs. Exchange your old product Which phone would you like to exchange? We are sorry. Amazon has encountered an error. Please try again. Your old phone will be rejected, if it has any dents, cracks on screen or body. View refer",
  "seller": "Dawntech Etel!",
  "warranty": "1 year manufacturer warranty for device and 6 months manufacturer warranty for in-box accessories including batteries from the date of purchase",
  "fbt": null,
  "size_chart": null,
  "customers_say": {
    "summary": "Customers find the phone's battery life reliable with good fast charging capabilities. However, the performance receives mixed feedback, with some saying it works perfectly while others report it doesn't work properly. The camera quality, display, and sound quality also get mixed reviews, with some finding them good while others report poor performance. Moreover, the phone receives negative feedback for its speed, network connectivity, and sound quality.",
    "themes": [
      {
        "theme": "battery life",
        "mentions": 16,
        "positive": 12,
        "negative": 4
      },
      {
        "theme": "value for money",
        "mentions": 43,
        "positive": 25,
        "negative": 18
      },
      "… (6 more)"
    ]
  },
  "video_count": 1,
  "video_thumb": "https://m.media-amazon.com/images/I/71-x6rPtf0L.SX38_SY50_CR,0,0,38,50_BG85,85,85_BR-120_PKdp-play-icon-overlay__.jpg",
  "aplus_images": [
    "https://m.media-amazon.com/images/S/aplus-media-library-service-media/27dc235e-4ac4-4fb0-bae8-2fcb726a1575.__CR0,0,1464,600_PT0_SX1464_V1___.jpg",
    "https://m.media-amazon.com/images/S/aplus-media-library-service-media/66814fb4-c930-47fa-9b93-cb7491e322fa.__CR0,0,1464,600_PT0_SX1464_V1___.jpg",
    "… (9 more)"
  ],
  "ships_from": "Amazon",
  "add_to_cart": true,
  "buying_options_count": null,
  "http_status": 200,
  "warnings": []
}
```

</details>

<details>
<summary><code>get_reviews on the same product</code></summary>

```json
{
  "asin": "B0G2B2QVLL",
  "ratings": {
    "average": 3.9,
    "total_ratings": 812,
    "histogram_percent": {
      "5": 52,
      "4": 21,
      "3": 9,
      "2": 3,
      "1": 15
    },
    "histogram_counts": {
      "5": 422,
      "4": 171,
      "3": 73,
      "2": 24,
      "1": 122
    }
  },
  "ratings_extras": {
    "reviews_with_images": 9,
    "mention_chips": [
      {
        "theme": "battery life",
        "mentions": 16,
        "positive": 12,
        "negative": 4
      },
      {
        "theme": "value for money",
        "mentions": 43,
        "positive": 25,
        "negative": 18
      },
      "… (6 more)"
    ]
  },
  "top_reviews": [
    {
      "review_id": "R3BBT5WIWPMAAY",
      "rating": 5.0,
      "title": "⭐⭐⭐⭐⭐ **Excellent Battery Life and Great Value for Money!**",
      "author": "Pradeep Kumar Jangid",
      "author_profile": "https://www.amazon.in/gp/profile/amzn1.account.AEG6ZLTRGR6CLBMBIPP7ZCID6F3A",
      "country": "India",
      "date": "11 October 2026",
      "verified_purchase": true,
      "variation": "Colour: Midnight Black",
      "body": "Really impressed with this phone! The large 6000mAh battery is a big plus for everyday use, making it a great choice for anyone who wants reliable battery life. The large 120Hz display offers a smooth experience for browsing, watching videos, and using apps. The MediaTek Dimensity 6300 processor is suitable for regular daily tasks, while the 6GB RAM and 128GB storage provide a practical combination for everyday use. The 50MP AI camera is another useful feature for capturing photos, and 33W fast charging adds convenience. The Midnight Black colour looks sleek and stylish, too. Overall, a feature-packed smartphone at an attractive price point. A good option for anyone looking for a large display, strong battery capacity, and 5G connectivity! 👍 Make the review sound more personalHighlight the phone’s main strengths",
      "helpful_count": 23,
      "images": [],
      "images_hi_res": [],
      "has_image": false
    },
    {
      "review_id": "R176YQDSDZ64PU",
      "rating": 4.0,
      "title": "Average",
      "author": "Dhruv Gupta",
      "author_profile": "https://www.amazon.in/gp/profile/amzn1.account.AHZECFUBQB2MHIAK2CBNYYYK3VEA",
      "country": "India",
      "date": "20 October 2026",
      "verified_purchase": true,
      "variation": "Colour: Midnight Black",
      "body": "Ok as expected.",
      "helpful_count": 0,
      "images": [],
      "images_hi_res": [],
      "has_image": false
    },
    "… (6 more)"
  ]
}
```

</details>

<details>
<summary><code>get_price_history over a 90-day window</code></summary>

```json
{
  "asin": "B0G2B2QVLL",
  "window_days": 90,
  "external": {
    "source": "pricehistory.app",
    "code": "G2N6dpw8",
    "asin": "B0G2B2QVLL",
    "product_name": "Redmi 15C 5G Prime Edition Dusk Purple 6GB",
    "points": 94,
    "granularity": "daily",
    "window": {
      "from": "2025-12-11",
      "to": "2026-10-24"
    },
    "lowest_ever": {
      "price": 13249,
      "on": "2026-02-21"
    },
    "highest_ever": {
      "price": 21578,
      "on": "2026-08-06"
    },
    "avg": 17585,
    "current": {
      "price": 17999,
      "mrp": 31999,
      "coupon": null,
      "deal": null
    },
    "lowest_offer_ever": 13249,
    "series": [
      {
        "t": "2026-08-01",
        "price": 20499
      },
      {
        "t": "2026-08-06",
        "price": 21578
      },
      "… (47 more)"
    ],
    "offer_series": [
      {
        "t": "2026-08-07",
        "price": 19498,
        "offers": [
          "1000 Coupon"
        ]
      },
      {
        "t": "2026-08-26",
        "price": 19499,
        "offers": [
          "1000 Coupon"
        ]
      }
    ],
    "sales": [],
    "verdict": {
      "price": 17999,
      "key": "wait",
      "label": "Better to wait",
      "formula": "price<=lowest*1.02 -> yes; <=avg(or (lowest+highest)/2) -> okay; <=avg+(highest-avg)*0.6 -> wait; else skip"
    },
    "updated_at": "2026-10-24 06:45:45",
    "cache": {
      "hit": true,
      "age_s": 2979
    }
  },
  "external_status": "ok",
  "note": "pricehistory.app archive: 94 daily pts, updated 2026-10-24 06:45:45, window 2025-12-11..2026-10-24 — cache hit 2979s old"
}
```

</details>

<details>
<summary><code>compare_products on two earbuds, with delivery to pincode 400001</code></summary>

```json
{
  "products": [
    {
      "asin": "B0GCMGNVJX",
      "title": "Kratos Pop Ear Buds Wireless with 60H Playtime, Noise Isolation & Clear Calls, Bluetooth Earbuds with Voice Assistant, Ear Buds with Bluetooth V 5.3 & with Type C Fast Charging, IPX4 TWS Earphones",
      "brand": "Kratos",
      "price_value": 529.0,
      "list_price": "₹2,999",
      "rating": 3.8,
      "review_count": 3845,
      "availability": "In stock",
      "add_to_cart": true,
      "delivery": {
        "bound_pin": "400001",
        "delivery_info": [
          "FREE delivery Tuesday, 27 October",
          "FREE delivery Tue, 27 Oct",
          "… (4 more)"
        ],
        "error": null
      }
    },
    {
      "asin": "B0D78XSMSM",
      "title": "pTron Bassbuds Astra in-Ear TWS Earbuds w/Stereo Sound, 50Hrs Playtime, Stereo Calls, Custom EQ, Mobile App, BTv5.3 Headphones, Touch Control, Voice Assistant, Type C Charging & IPX4 (Black)",
      "brand": "pTron",
      "price_value": 599.0,
      "list_price": "₹2,899",
      "rating": 4.0,
      "review_count": 3510,
      "availability": "In stock",
      "add_to_cart": true,
      "delivery": {
        "bound_pin": "400001",
        "delivery_info": [
          "FREE delivery Tuesday, 27 October",
          "FREE Delivery",
          "… (5 more)"
        ],
        "error": null
      }
    }
  ],
  "comparison": {
    "lowest_price_asin": "B0GCMGNVJX",
    "highest_rating_asin": "B0D78XSMSM",
    "missing_fields": {}
  },
  "warnings": []
}
```

</details>

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
- **External price history (`get_price_history`)** comes from [pricehistory.app](https://pricehistory.app) (daily granularity — their tracking is popularity-based, so point counts vary per product; `points` + `updated_at` always tell you what you got). Read-only: no account, no cookies shared with them; **one code lookup ever per ASIN + payload cached 6 h in sqlite (survives restarts — repeated calls are free)**; a 403/429 trips a 45 s local cooldown instead of hammering. We keep no parallel price archive of our own — theirs is strictly richer. Attribution: data © pricehistory.app, free for personal use — for scale/commercial use contact them about their official API; `AMAZON_MCP_DISABLE_PH=1` disables the source entirely.

## Troubleshooting

- **Tools don't show up in the client** — restart the client after editing its MCP config; the server only connects on launch. Check `uvx --from git+https://github.com/thenabbu/amazon-mcp amazon-mcp` runs standalone first.
- **`RATE_LIMITED` errors** — amazon's robot-check wall; wait ~45–60 s and retry. It usually clears itself; the server already backs off for you.
- **Slow first request** — the first call builds a session and may solve a challenge (~3–8 s).

## Project layout

```
amazon_mcp/
  server.py           # MCP tool layer (11 tools, input validation, thread-safe, stdio + http)
  amz_lib.py          # HTTP session: TLS fingerprinting + Akamai challenge solvers
  search_parse.py     # search-results parser (pure functions over HTML)
  product_extract.py  # product-page parser
  reviews_fetch.py    # reviews + ratings parser
  pincode_common.py   # delivery-pincode binding
```

## License

MIT — see [LICENSE](LICENSE).
