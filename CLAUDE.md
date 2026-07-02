# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

A Playwright-based automation service that logs into **amazon.in** with an
email/phone + password, scrapes the last N **Amazon Fresh** orders from the
order history, extracts each constituent item, and produces a per-product
report containing:

1. Purchase date — taken from each order's detail page.
2. Number of times that product appears across the scanned orders.
3. Current price, image URL, product URL, and availability — captured by
   visiting each unique product page once.

After each successful scrape, the report is also pushed to **Salesforce**: each
unique product title is upserted into `Grocery_Product__c` using `title__c` as the
external ID — existing titles are updated, new titles are created — setting
`number_of_times_purchased__c`, `last_ordered_date__c`, `current_price__c`,
`last_purchased_price__c`, `product_url__c`, `image_url__c`, `availability__c`,
`source__c`, and `scraped_at__c`. `source__c` is per-product: `"Amazon Now"` for
items from Amazon Now (the `/tez/` quick-commerce service) and `"Amazon Fresh"`
for classic Amazon Fresh items.

Note the price distinction: `current_price__c` is the live price read from the
product page (and also the sole determinant of `availability__c` — priced
means Available), while `last_purchased_price__c` is the price actually paid in
the most recent order containing that product (read from the order item list).

This is the Amazon sibling of the `purchase-history` project (Flipkart). Both
projects write to the same Salesforce object; the `source__c` field
distinguishes the two.

The project runs as a **Flask web service** — scraping is triggered via HTTP
endpoints, and an interactive **Swagger UI** is served at `/docs`. It is
designed for local development and cloud deployment on **Render** (Docker-based).

## Tech Stack

| Concern | Choice |
|---|---|
| Language | Python 3.11 |
| Browser automation | Playwright (async) + Chromium |
| Login | amazon.in email/phone + password; 2-step OTP pushed via `POST /api/otp` (no Gmail dependency) |
| Salesforce sync | REST API + OAuth 2.0 client_credentials (Connected App) |
| Web service | Flask 3 |
| API docs | Swagger UI (CDN) backed by OpenAPI 3.0 spec at `/openapi.json` |
| Deployment | Render (Docker) |
| Config | `.env` file locally; Render environment variables in production |

## File Layout

```
.
├── CLAUDE.md
├── README.md
├── Dockerfile               # Docker image for Render deployment
├── render.yaml              # Render service configuration
├── .env.example             # Template — no real values
├── .gitignore
├── .dockerignore
├── requirements.txt
├── app.py                   # Flask web service (entry point) + Swagger UI at /docs
├── scrape_amazon_orders.py  # Core scraping logic; calls salesforce_sync at end
├── amazon_cart.py           # Add Fresh products to cart by name (search + fuzzy match)
├── otp_store.py             # In-process, short-TTL store for the 2-step OTP push
└── salesforce_sync.py       # OAuth + PATCH Grocery_Product__c.title__c matches
```

## Environment Variables

### Required (local `.env` and Render dashboard)

| Variable | Description |
|---|---|
| `AMAZON_USERNAME` | Amazon.in login email or phone |
| `AMAZON_PASSWORD` | Amazon.in account password |

### Salesforce sync (all four required; sync is skipped if any are missing)

| Variable | Description |
|---|---|
| `SF_TOKEN_URL` | OAuth token endpoint, e.g. `https://<domain>.my.salesforce.com/services/oauth2/token` |
| `SF_CLIENT_ID` | Connected App consumer key |
| `SF_CLIENT_SECRET` | Connected App consumer secret |
| `SF_API_ENDPOINT` | `https://<domain>.my.salesforce.com/services/data/v57.0/sobjects/Grocery_Product__c/` |

### Optional overrides

| Variable | Default |
|---|---|
| `HEADLESS` | `false` locally, `true` in Docker |
| `PORT` | `10000` (Render sets this automatically) |
| `AMAZON_AUTH_STATE_PATH` | `auth_state.json` — Playwright `storage_state` file caching the logged-in session (cookies + localStorage) so later runs skip login/OTP. Only used when reuse is enabled. Gitignored; holds live session cookies — never commit it. |
| `AMAZON_SESSION_REUSE` | **`false`** (opt-in) — unset means full login every run. Set to `true`/`1`/`yes` to save and reuse the session. Left off by default so cloud deploys (ephemeral FS) keep the proven login behavior. |
| `ORDERS_TO_SCRAPE` | `10` — fallback for both `scrape_amazon_orders.py` (when `--orders` is omitted) and `POST /api/products` (when the request body omits `"orders"`). Explicit values still override. |
| `OTP_TTL_SECONDS` | `300` — how long an OTP pushed to `POST /api/otp` stays valid before it is treated as stale. Short by design; the code is also cleared the instant it is consumed. |
| `DELIVERY_ADDRESS_PREFIX` | _(empty)_ — after login the scraper first tries to pick the saved "Deliver to" address whose text contains this substring; only if no match is found does it fall back to `DELIVERY_PINCODE`. **Personal (PII)** — set it via env / `.env` / dashboard; no value is hard-coded in the repo. |
| `DELIVERY_PINCODE` | _(empty)_ — 6-digit pincode entered as Amazon's "Deliver to" location when no saved address matches `DELIVERY_ADDRESS_PREFIX`. Fresh availability/price is per-location; with neither var set, location selection is skipped and items show "currently unavailable". **Personal (PII)** — set via env, not in the repo. |

## Environment Setup (Local)

```bash
python -m venv .venv
source .venv/bin/activate    # macOS / Linux
# or: .venv\Scripts\activate  # Windows
pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill in `AMAZON_USERNAME`, `AMAZON_PASSWORD`.

## Running Locally

### Start the web service

```bash
PORT=3001 HEADLESS=false python app.py
```

### API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/docs` | Interactive Swagger UI |
| `GET` | `/openapi.json` | OpenAPI 3.0 spec |
| `GET` | `/api/products` | Latest scrape output, `{product_name, date, number_of_times_purchased, ...}` shape |
| `POST` | `/api/products` | Start a scrape (runs in background thread). Body: `{"orders": <int>}`, default 10 |
| `GET` | `/api/search?q=<query>` | Live product search on Amazon Now (`/tez/` storefront, via its `searchByKeyword` JSON captured in-page); falls back to the classic Fresh search only when Now returns nothing. Per-product `source` = `"Amazon Now"` or `"Amazon Fresh"` |
| `GET` | `/api/cart` | Result of the last add-to-cart run: `{requested, added[], not_found[], cart_count, now_cart_count}` |
| `POST` | `/api/cart` | Add products to the cart by name (background thread) — Amazon Now cart first, classic Fresh cart as per-product fallback. Body: `{"products": [<str>, ...]}` |
| `GET` | `/api/otp` | Is a run currently waiting for a 2-step OTP? `{waiting, waiting_since, ttl_seconds}` |
| `POST` | `/api/otp` | Hand the 2-step verification OTP to a waiting run. Body: `{"otp": "<4-8 digits>"}` |

Scrapes and cart runs share the single Amazon account and cannot overlap — the
second concurrent `POST` returns `409`.

Open `http://localhost:3001/docs` for the interactive playground (the `/` route
redirects there).

### Run the scraper directly (without Flask)

```bash
python scrape_amazon_orders.py                  # headed, 10 orders
python scrape_amazon_orders.py --orders=5
python scrape_amazon_orders.py --headed=false   # headless
```

## High-Level Scraping Flow

1. **Launch Chromium** (headed locally, headless in Docker).
2. **Login** (in `open_logged_in_page`) — full login by default:
   - Navigate to `/ap/signin`, fill `AMAZON_USERNAME` → Continue, then
     `AMAZON_PASSWORD` → Sign In.
   - **Optional session reuse (opt-in via `AMAZON_SESSION_REUSE=true`):** if a
     saved `auth_state.json` exists, the context is created from it and validated
     by loading the orders page (it redirects to `/ap/signin` when logged out). A
     valid session **skips login and OTP entirely**; an expired/corrupt one falls
     back to the full login above. After any successful login the session is
     saved back to `auth_state.json`. Disabled by default.
   - **Captcha**: if Amazon shows one, stop. Re-run locally headed to solve once.
   - **2-step verification OTP**: the run blocks on an in-process store
     (`otp_store`) for up to 3 minutes — works in both headed and headless mode.
     `POST /api/otp {"otp": "..."}` hands it the code; it submits the OTP within
     ~1 s. The code lives in memory only and expires after `OTP_TTL_SECONDS`
     (default 300 s) so a stale code is never reused. In a headed local browser
     you can alternatively just type the OTP into Amazon directly — the run
     detects the screen advancing and continues.
3. **Set the delivery location** (so Fresh items report correct availability/
   price): open the "Deliver to" popover and first try to pick the saved
   address containing `DELIVERY_ADDRESS_PREFIX`; only if that's not found,
   enter `DELIVERY_PINCODE`. The choice persists in a session cookie.
4. **Navigate** to `https://www.amazon.in/your-orders/orders?orderFilter=months-6`.
5. **Filter for Amazon Fresh orders** — first try the dropdown if it exposes a
   Fresh option; otherwise filter client-side by matching the order card text
   for `"Amazon Fresh"`, `"Sold by: Amazon Fresh"`, or `"Fulfilled by Amazon Fresh"`.
6. **Paginate** until `num_orders` Fresh orders are collected (max 5 pages).
7. **For each Fresh order**: visit its `/gp/your-account/order-details` page,
   extract every product row's title, product-page URL, the order date, and the
   per-item **purchased price** (price paid in that order).
8. **For each unique product**: visit its product page once to capture
   `current_price`, `product_url`, `image_url`, and `availability`
   (`availability` is derived solely from whether the product page shows a
   price), carry the most-recent order's `last_purchased_price`, then
   immediately PATCH the matching `Grocery_Product__c` record in Salesforce.
9. **Write** `orders_report.json` and print the summary table.

## Selector Strategy

Amazon's DOM is generally more semantic than Flipkart's (real IDs like
`#ap_email`, `#signInSubmit`), but it still changes occasionally and adds
A/B-tested variants. Use this priority order:

1. Element IDs (`input#ap_password`) — most stable.
2. `name=` attributes — also stable.
3. Role + accessible name: `page.get_by_role("button", name="Sign-In")`
4. Visible text: `page.get_by_text("Order details", exact=False)`
5. Structural selectors anchored on visible text as last resort.

All selectors are centralised in the `SELECTORS` dict at the top of
`scrape_amazon_orders.py`. Update there and nowhere else.

When a selector fails:
- Save a screenshot (`amazon_login_debug.png` or `amazon_orders_debug.png`).
- Log the current URL.
- Exit non-zero. Do not silently continue with empty data.

## Expected Output Shape

`orders_report.json`:
```json
{
  "scraped_at": "2026-05-26T10:15:00+05:30",
  "orders_scanned": 7,
  "products": [
    {
      "title": "Amazon Brand - Vedaka Organic Toor Dal, 500g",
      "last_ordered_date": "2026-05-12",
      "number_of_times_purchased": 2,
      "current_price": 89.0,
      "last_purchased_price": 85.0,
      "product_url": "https://www.amazon.in/dp/B0...",
      "image_url": "https://m.media-amazon.com/images/I/...",
      "category": "Grocery",
      "availability": "Available",
      "source": "Amazon Fresh",
      "scraped_at": "2026-05-26T10:15:00+05:30"
    }
  ]
}
```

## Render Deployment

### How it works

- Render builds the `Dockerfile` (Python 3.11-slim + Playwright Chromium).
- Every run does a full login (email + password). Session reuse is **off in the
  cloud by default** (`AMAZON_SESSION_REUSE=false` in `render.yaml`) because the
  container disk is ephemeral — `auth_state.json` wouldn't survive a
  deploy/restart anyway. It can be flipped to `true` in the dashboard to reuse
  the session within a single container's lifetime.
- Scraping runs headless inside the container.

### Deploy steps

1. Push code to GitHub.
2. Render → **New Web Service** → connect repo → auto-detects `Dockerfile`.
3. Add environment variables in Render dashboard (see table above).
4. Deploy. Trigger a scrape via `POST /api/products`.
5. If Amazon asks for OTP, watch the Render logs for the `[auth] ACTION REQUIRED`
   line (or poll `GET /api/otp` for `waiting: true`), then `POST /api/otp` with
   the code. The scraper picks it up within ~1 second.

## Amazon login pitfalls

- **Amazon is aggressive at bot detection.** The first login from a new
  IP/device usually triggers a captcha and/or OTP. Captchas require a headed
  local run to solve by hand. OTPs are handled via the `/api/otp` push (see below).
- **Login every run by default.** Session caching is **opt-in**
  (`AMAZON_SESSION_REUSE=true`) and off unless explicitly enabled, so the default
  path is a full login (and possible OTP) on every run. When enabled, the session
  is saved to `auth_state.json` and reused so most runs skip login/OTP — but
  Amazon still expires sessions and may challenge, so the full-login + OTP path
  must always keep working. On Render/Railway the file lives on an **ephemeral**
  disk: it persists only within a running container's lifetime, not across
  deploys/restarts — which is why reuse defaults off in the cloud.
- **Wrong password / locked account.** Amazon shows the same "Sign in" page
  after a bad submit. The scraper detects this and exits with a screenshot.
- **Fresh orders may be empty.** If your account has no recent Amazon Fresh
  orders, the report is written with `products: []` rather than erroring —
  the Salesforce sync then no-ops cleanly.

### OTP via `/api/otp` (2FA push — all modes)

When Amazon presents the 2-step verification (OTP) screen — in any mode, headed
or headless — the run blocks on an in-process store (`otp_store.py`) and waits
for the code to be pushed over HTTP:

1. The run marks the store as *waiting* and blocks for up to 3 minutes, checking
   the store every second. `GET /api/otp` reports `waiting: true`; the log prints
   an `[auth] ACTION REQUIRED` line.
2. You watch the email/SMS Amazon just sent, then `POST /api/otp` with
   `{"otp": "123456"}`.
3. The run consumes the code (clearing it from memory immediately), types it into
   Amazon, and continues. The code also self-expires after `OTP_TTL_SECONDS`
   (default 300 s), so a stale value can never reach a later login.
4. If 3 minutes elapse with no push, the run exits with a screenshot and a clear
   error so it can be retried.

This is wholly in-process: no Salesforce, no extra Connected App permissions,
and the OTP never leaves the running container's memory. `POST /api/otp` only
accepts a code while a run is actually waiting (otherwise `409`). In a headed
local browser you can skip the API entirely and type the OTP straight into
Amazon — the run detects the screen advancing and carries on.

> **Single-tenant / in-memory caveat:** the store holds one OTP for the one
> Amazon account, and only one scrape/cart run can be in flight at a time, so a
> single global slot is sufficient. Because the code lives only in the process's
> memory, the `POST /api/otp` must hit the **same** running instance that is
> waiting — fine for a single Render/Railway web service, but it would not work
> across multiple replicas.

## Salesforce sync notes

- Auth uses OAuth 2.0 `client_credentials` flow (Connected App with the "Run As"
  user set). Tokens are cached in-process and refreshed on a 401.
- Field mapping (hard-coded constants at the top of `salesforce_sync.py`):
  - Match field: `title__c`
  - Updated fields: `number_of_times_purchased__c`, `last_ordered_date__c`,
    `current_price__c`, `last_purchased_price__c`, `product_url__c`,
    `image_url__c`, `category__c`, `availability__c`, `source__c`
    (= `"Amazon Now"` or `"Amazon Fresh"`, per product), `scraped_at__c`
  - `current_price__c` = live product-page price; `last_purchased_price__c` =
    price paid in the most recent order for that product (from the order item
    list, NOT the product page).
- The Connected App must grant access to the `Grocery_Product__c` sObject and
  the `api` scope. `Name` is auto-number on this object and **must not** be
  sent in POST/PATCH bodies.
- Running `python salesforce_sync.py` re-syncs the current `orders_report.json`
  on demand, without re-running the scraper.
- Both this project and `purchase-history` (Flipkart) update the SAME
  `Grocery_Product__c` records. For products sold on both retailers, the
  `source__c` field reflects whichever scraper ran last — this is by design.

## Add-to-cart flow (`amazon_cart.py`)

The one write action the service performs. `POST /api/cart` with
`{"products": [<name>, ...]}` runs `add_products_to_cart()` in a background
thread:

1. **Reuse the session** — `open_logged_in_page()` (factored out of
   `scrape_amazon_orders.run()`) does launch + login + OTP/captcha handling +
   delivery-location setup, identical to a scrape. Add-to-cart availability
   is location-keyed, so this setup is required.
2. **For each name — Amazon Now first**: load the `/tez/` search page
   (`/tez/browse/search?qcbrand=<brand>&searchKeyword=<name>`) and capture the
   `searchByKeyword` JSON it fetches (the endpoint 204s when called directly —
   CSRF), fuzzy-match the best `IN_STOCK` title (`difflib.SequenceMatcher`,
   same `MATCH_THRESHOLD` `0.6`), then click that ASIN's Add button
   (`AsinFaceout-AddToCart-<ASIN>`); the add is confirmed by the SPA's cart
   XHR. Items go to the **Amazon Now cart, which is separate from the main
   Amazon cart**.
3. **Fresh fallback (per product)** — only when Now has no confident match or
   the add fails: search the classic node (`/s?k=<name>&i=nowstore`), scan up
   to `MAX_RESULTS_TO_SCAN` result cards, same fuzzy match, add one unit and
   confirm via the `#nav-cart-count` badge incrementing.
4. **Report** — return `{requested, added[], not_found[], cart_count,
   now_cart_count, added_at}`; each added item carries `source` ("Amazon Now"
   or "Amazon Fresh"). Unmatched names land in `not_found` with their best
   candidate + score.

Cart-specific selectors live in `CART_SELECTORS` at the top of `amazon_cart.py`
(same convention as `SELECTORS` in the scraper). The flow **stops at the cart**
— it never opens checkout. A scrape and a cart run cannot run concurrently
(shared Amazon account → `409`).

## Out of Scope

- No checkout, purchase, payment, cancel, or return. (Adding to cart via
  `POST /api/cart` is in scope; everything past the cart is not.)
- Scraping covers Amazon Fresh **and** Amazon Now orders; other Amazon orders
  (regular retail) are skipped.
- Salesforce sync **upserts** by `title__c` (external ID): existing titles are
  updated, new titles are created. (Historically documented as update-only; the
  code actually creates on first sight.)
- No captcha solving — captchas always require a one-time headed local run.
- No multi-user support — the service is single-tenant (one Amazon account).
