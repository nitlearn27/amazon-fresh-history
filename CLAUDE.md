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
unique product title is matched against `Grocery_Product__c.title__c`, and
matching records get `number_of_times_purchased__c`, `last_ordered_date__c`,
`current_price__c`, `product_url__c`, `image_url__c`, `availability__c`,
`source__c` (= `"Amazon"`), and `scraped_at__c` updated. **No new records are
ever created** — non-matching titles are skipped.

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
| Login | amazon.in email/phone + password (no OTP/Gmail dependency) |
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

### Cloud-only (Render dashboard — populated after first local run)

| Variable | Description |
|---|---|
| `AMAZON_AUTH_STATE` | Full contents of `auth_state.json` after first successful local scrape |

### Optional overrides

| Variable | Default |
|---|---|
| `HEADLESS` | `false` locally, `true` in Docker |
| `PORT` | `10000` (Render sets this automatically) |
| `ORDERS_TO_SCRAPE` | `10` — fallback for both `scrape_amazon_orders.py` (when `--orders` is omitted) and `POST /api/products` (when the request body omits `"orders"`). Explicit values still override. |

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

Open `http://localhost:3001/docs` for the interactive playground (the `/` route
redirects there).

### Run the scraper directly (without Flask)

```bash
python scrape_amazon_orders.py                  # headed, 10 orders
python scrape_amazon_orders.py --orders=5
python scrape_amazon_orders.py --headed=false   # headless
```

## High-Level Scraping Flow

1. **Launch Chromium** (headed locally, headless in Docker) with a persistent
   browser context.
2. **Restore session** from `auth_state.json` if present.
3. **Login** via email/password:
   - Probe `https://www.amazon.in/your-orders` — if it serves the page, we're
     already logged in.
   - Otherwise go to `/ap/signin`, fill `AMAZON_USERNAME` → Continue, then
     fill `AMAZON_PASSWORD` → Sign In.
   - **Captcha**: if Amazon shows one, stop. Re-run locally headed to solve once.
   - **2-step verification OTP**: if prompted in headed mode, the scraper
     reads the OTP from stdin. In headless mode it exits with a clear error.
4. **Save session** to `auth_state.json` (skips login on subsequent runs).
5. **Navigate** to `https://www.amazon.in/your-orders/orders?orderFilter=months-6`.
6. **Filter for Amazon Fresh orders** — first try the dropdown if it exposes a
   Fresh option; otherwise filter client-side by matching the order card text
   for `"Amazon Fresh"`, `"Sold by: Amazon Fresh"`, or `"Fulfilled by Amazon Fresh"`.
7. **Paginate** until `num_orders` Fresh orders are collected (max 5 pages).
8. **For each Fresh order**: visit its `/gp/your-account/order-details` page,
   extract every product row's title + product-page URL, and the order date.
9. **For each unique product**: visit its product page once to capture
   `current_price`, `product_url`, `image_url`, and `availability`.
10. **Aggregate** by product title; write `orders_report.json`; print table.
11. **Sync to Salesforce** (best-effort):
    - Authenticate via `client_credentials` against `SF_TOKEN_URL`.
    - For each unique title, PATCH `Grocery_Product__c/title__c/<title>` with
      `source__c="Amazon"`.
    - Existing records are updated; non-matching titles are skipped.
    - Any Salesforce error is logged but does not fail the scrape.

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
      "product_url": "https://www.amazon.in/dp/B0...",
      "image_url": "https://m.media-amazon.com/images/I/...",
      "category": "Grocery",
      "availability": "Available",
      "source": "Amazon",
      "scraped_at": "2026-05-26T10:15:00+05:30"
    }
  ]
}
```

## Render Deployment

### How it works

- Render builds the `Dockerfile` (Python 3.11-slim + Playwright Chromium).
- On container start, `app.py` reads `AMAZON_AUTH_STATE` and writes it to
  `auth_state.json` (Render's filesystem is ephemeral — it resets on every
  restart).
- Scraping runs headless inside the container.

### Deploy steps

1. Push code to GitHub.
2. **First, run locally in headed mode** (`python scrape_amazon_orders.py
   --headed=true --orders=1`) to capture `auth_state.json`. Solve any captcha
   or 2-step verification challenge during this run.
3. Render → **New Web Service** → connect repo → auto-detects `Dockerfile`.
4. Add environment variables in Render dashboard (see table above).
5. Set `AMAZON_AUTH_STATE` to the full contents of the local `auth_state.json`.
6. After the first successful scrape on Render, the logs print a fresh
   `auth_state.json` content — copy that value back into `AMAZON_AUTH_STATE`
   to extend the session lifespan.

## Amazon login pitfalls

- **Amazon is more aggressive than Flipkart at bot detection.** The first
  login from any new IP/device usually triggers a captcha and/or 2-step
  verification email. Both require a one-time headed local run to clear.
- **Sessions persist.** After a successful login, the cookies in
  `auth_state.json` typically remain valid for weeks. Refresh the Render env
  var whenever the scraper starts erroring with "Login did not complete".
- **Wrong password / locked account.** Amazon shows the same "Sign in" page
  after a bad submit. The scraper detects this and exits with a screenshot.
- **`AMAZON_AUTH_STATE` env-var quoting on Render.** Paste the raw JSON
  contents into the env var — do NOT wrap them in extra quotes or escape
  characters.
- **Fresh orders may be empty.** If your account has no recent Amazon Fresh
  orders, the report is written with `products: []` rather than erroring —
  the Salesforce sync then no-ops cleanly.

### OTP via Salesforce (headless 2FA bridge)

When Amazon presents the 2-step verification (OTP) screen in headless mode
— typical the first time a brand-new IP/device combination tries to log in
— the scraper cannot read stdin. Instead it uses Salesforce as a manual
side-channel:

1. The scraper polls `Purchase_Info__c.my_amazon_otp__c` every 5 seconds
   for up to 3 minutes.
2. You watch the email/SMS Amazon just sent, then paste the OTP into that
   field on the single `Purchase_Info__c` record and save.
3. The scraper picks up the value, types it into Amazon, and immediately
   nulls the field so the next run does not see a stale OTP.
4. If 3 minutes elapse with the field empty, the scraper exits with a
   screenshot and a clear error so the run can be retried.

Headed local runs are unchanged — they still prompt for the OTP on stdin.

Connected App requirements: the "Run As" user must have **Read** and
**Edit** permission on `Purchase_Info__c` and the `my_amazon_otp__c`
field, in addition to the existing `Grocery_Product__c` permissions. No
new env vars — the bridge reuses `SF_TOKEN_URL`, `SF_CLIENT_ID`,
`SF_CLIENT_SECRET`, `SF_API_ENDPOINT`.

## Salesforce sync notes

- Auth uses OAuth 2.0 `client_credentials` flow (Connected App with the "Run As"
  user set). Tokens are cached in-process and refreshed on a 401.
- Field mapping (hard-coded constants at the top of `salesforce_sync.py`):
  - Match field: `title__c`
  - Updated fields: `number_of_times_purchased__c`, `last_ordered_date__c`,
    `current_price__c`, `product_url__c`, `image_url__c`, `category__c`,
    `availability__c`, `source__c` (= `"Amazon"`), `scraped_at__c`
- The Connected App must grant access to the `Grocery_Product__c` sObject and
  the `api` scope. `Name` is auto-number on this object and **must not** be
  sent in POST/PATCH bodies.
- Running `python salesforce_sync.py` re-syncs the current `orders_report.json`
  on demand, without re-running the scraper.
- Both this project and `purchase-history` (Flipkart) update the SAME
  `Grocery_Product__c` records. For products sold on both retailers, the
  `source__c` field reflects whichever scraper ran last — this is by design.

## Out of Scope

- No purchase, cancel, return, or any write action on the Amazon account.
- No scraping beyond Amazon Fresh orders (other Amazon orders are skipped).
- No creation of new Salesforce records — sync only updates existing titles.
- No captcha solving — captchas always require a one-time headed local run.
- No multi-user support — the service is single-tenant (one Amazon account).
