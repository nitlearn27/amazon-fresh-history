# Amazon Fresh Purchase History → Salesforce

A Flask web service that logs into **amazon.in** with an email/phone + password,
scrapes the last N **Amazon Fresh** orders, and syncs each unique product to
Salesforce `Grocery_Product__c` records.

- **Sibling project** to `purchase-history` (Flipkart). Both write to the same
  Salesforce object; the `source__c` field distinguishes the source
  (`"Flipkart"`, `"Amazon Fresh"`, or `"Amazon Now"`).
- **Salesforce sync upserts** by `title__c` (external ID): existing
  `Grocery_Product__c` records are updated, and titles not seen before are
  created.
- **Interactive Swagger UI** at `/docs`.
- **Deployable to Render** out of the box (Docker, headless Chromium).

---

## API

| Method | Path             | Description                                                                 |
|--------|------------------|-----------------------------------------------------------------------------|
| `GET`  | `/health`        | Liveness probe.                                                             |
| `GET`  | `/docs`          | Swagger UI playground (the root `/` redirects here).                        |
| `GET`  | `/openapi.json`  | OpenAPI 3.0 spec.                                                           |
| `GET`  | `/api/products`  | Latest scrape output.                                                       |
| `POST` | `/api/products`  | Start a scrape in a background thread. Body: `{"orders": <int>}` (default 10). |
| `GET`  | `/api/cart`      | Result of the last add-to-cart run (`added` vs `not_found`).               |
| `POST` | `/api/cart`      | Add Amazon Fresh products to the cart by name. Body: `{"products": ["name", …]}`. |
| `GET`  | `/api/otp`       | Is a run waiting for a 2-step OTP? `{waiting, waiting_since, ttl_seconds}`. |
| `POST` | `/api/otp`       | Hand the 2-step verification OTP to a waiting run. Body: `{"otp": "123456"}`. |

A scrape typically takes 3–8 minutes. Poll `GET /api/products` until `status`
flips from `running` to results. A scrape and a cart run cannot overlap (they
share the single Amazon account) — the second request gets `409`.

---

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate     # macOS / Linux
# or: .venv\Scripts\activate   # Windows

pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill in the required values.

### Captcha

If Amazon shows a captcha, the scrape stops with a screenshot and a clear
error. Re-run locally in headed mode (`--headed=true`) to solve it once by
hand; captchas do not recur once Amazon trusts the browser fingerprint.

### Session reuse (fewer logins / OTPs) — opt-in

By default every run does a full login. **Opt in** by setting
`AMAZON_SESSION_REUSE=true` (e.g. in your local `.env`). When enabled, the
browser session (cookies + localStorage) is saved to `auth_state.json` after
login; the next run loads the orders page to check it's still valid and, if so,
**skips login and OTP entirely**. An expired or corrupt session falls back to a
full login (and re-save). So you typically OTP once, then run freely until Amazon
logs the session out (usually days).

- It's **off by default** so cloud deploys keep the proven full-login behavior —
  Render/Railway use an **ephemeral** filesystem, so `auth_state.json` does not
  survive a deploy/restart and reuse only helps within one container's lifetime.
- `auth_state.json` holds live session cookies — it's gitignored; never commit it.
- To turn it off again (or recover from a bad state): set
  `AMAZON_SESSION_REUSE=false` (or just unset it) and/or delete `auth_state.json`.
- `AMAZON_AUTH_STATE_PATH` overrides the file location (default `auth_state.json`).

### OTP / 2-step verification

When a login *is* needed, Amazon may ask for a 2-step verification code. The run
blocks for up to 3 minutes waiting for you to push the code over HTTP:

```bash
# Check whether a run is waiting:
curl $BASE_URL/api/otp        # → {"waiting": true, ...}

# Hand it the OTP Amazon just sent:
curl -X POST $BASE_URL/api/otp -H "Content-Type: application/json" -d '{"otp":"123456"}'
```

The scraper picks it up within ~1 s, submits it, and continues. The code is held
in memory only and expires after `OTP_TTL_SECONDS` (default 300 s), so a stale
code is never reused. This works the same way in headed local runs and headless
Render runs. In a headed local browser you can also just type the OTP into Amazon
directly — the run detects the screen advancing and carries on.

### Delivery location (Fresh availability)

Amazon Fresh prices and stock are **per delivery location**. With no location
set, every Fresh product page reports *"currently unavailable"* and no price.
So right after login the scraper sets the "Deliver to" location:

1. It first tries to select the saved address whose text contains
   `DELIVERY_ADDRESS_PREFIX`.
2. If no matching saved address is found, it enters `DELIVERY_PINCODE`.

Both are **personal (PII), so there are no hard-coded defaults** — set them in
your `.env` locally and in the Render/Railway dashboard in production. Set
`DELIVERY_ADDRESS_PREFIX` to a substring unique to the address you want and
`DELIVERY_PINCODE` to your 6-digit pincode. With neither set, location selection
is skipped and Fresh items report as *"currently unavailable"*.

### Salesforce Connected App

If you want sync, create a Connected App with:

- **OAuth flow:** Client Credentials
- **Scopes:** `api`, `refresh_token`
- **Run-as user** with read/update access to `Grocery_Product__c` (including the
  `last_purchased_price__c` field).

Then fill the four `SF_*` env vars in `.env`. If any are missing, sync is
silently skipped and the scrape still completes.

---

## Running

### Start the web service

```bash
PORT=3001 HEADLESS=false python app.py
```

Open <http://localhost:3001/docs> for the interactive playground.

```bash
# Trigger a scrape
curl -X POST http://localhost:3001/api/products \
  -H "Content-Type: application/json" \
  -d '{"orders": 5}'

# Poll for results
curl http://localhost:3001/api/products
```

### Add Amazon Fresh products to the cart

```bash
# Via the API — names are searched, fuzzy-matched, and added one unit each
curl -X POST http://localhost:3001/api/cart \
  -H "Content-Type: application/json" \
  -d '{"products": ["Amul Gold Full Cream Milk 500ml", "Vedaka Toor Dal 1kg"]}'

# Poll for the result (added vs not_found)
curl http://localhost:3001/api/cart
```

Each name is searched on Amazon Fresh and matched against result titles with a
`difflib` similarity ratio; the best result is added only if it clears the
confidence threshold. Names that don't match confidently are reported under
`not_found`. The run **never proceeds to checkout** — matched items are left in
the cart for you to review and buy manually.

### Run the scraper directly (no Flask)

```bash
python scrape_amazon_orders.py                  # headed, 10 orders
python scrape_amazon_orders.py --orders=5
python scrape_amazon_orders.py --headed=false   # headless
```

### Add to cart directly (no Flask)

```bash
python amazon_cart.py "Amul Gold Full Cream Milk 500ml" "Vedaka Toor Dal 1kg"
python amazon_cart.py "Vedaka Toor Dal 1kg" --headed=false   # headless
```

### Re-sync the existing report to Salesforce (no re-scrape)

```bash
python salesforce_sync.py
```

---

## Deploying to Render

The repo is Docker-based and ready for Render's "New Web Service → connect repo"
flow. `render.yaml` declares every env var the service expects.

### Deploy steps

1. **Push to GitHub.**
2. **Render → New → Web Service → connect repo.** Render auto-detects
   `Dockerfile` and `render.yaml`.
3. **Set environment variables** in the Render dashboard:

   | Variable                  | Value                                                            |
   |---------------------------|------------------------------------------------------------------|
   | `AMAZON_USERNAME`         | your Amazon.in login email/phone                                 |
   | `AMAZON_PASSWORD`         | your Amazon.in password                                          |
   | `SF_TOKEN_URL`            | Salesforce OAuth token endpoint                                  |
   | `SF_CLIENT_ID`            | Connected App consumer key                                       |
   | `SF_CLIENT_SECRET`        | Connected App consumer secret                                    |
   | `SF_API_ENDPOINT`         | `…/services/data/v57.0/sobjects/Grocery_Product__c/`             |
   | `HEADLESS`                | `true` (already set in `render.yaml`)                            |
   | `ORDERS_TO_SCRAPE`        | _(optional)_ default order count; `10` in `render.yaml`          |
   | `DELIVERY_ADDRESS_PREFIX` | _(personal)_ saved-address substring to deliver to; set in dashboard, not in repo |
   | `DELIVERY_PINCODE`        | _(personal)_ 6-digit pincode fallback; set in dashboard, not in repo |
   | `AMAZON_AUTH_STATE_PATH`  | _(optional)_ session-cache file path; default `auth_state.json`  |
   | `AMAZON_SESSION_REUSE`    | _(optional)_ `true` enables session reuse; default `false` (off; `render.yaml` pins it off) |
   | `OTP_TTL_SECONDS`         | _(optional)_ how long a pushed OTP stays valid; default `300` (5 min) |

   Only `AMAZON_*` are strictly required to deploy. The `SF_*` block enables
   Salesforce sync; the rest have working defaults.

4. **Deploy.** Trigger a scrape via `POST /api/products`.

### OTP on Render

Each scrape does a full login. If Amazon asks for a 2-step verification code,
watch the Render logs for the `[auth] ACTION REQUIRED` line (or poll
`GET /api/otp` for `waiting: true`), then `POST /api/otp` with `{"otp":"123456"}`.
The scraper picks it up within ~1 second, submits it, and continues
automatically. The code lives only in the running container's memory and expires
after `OTP_TTL_SECONDS`.

---

## Output shape

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

- `number_of_times_purchased` is the per-product aggregate across all scanned
  orders, repeated on every row of the same title.
- `current_price` is the live price read from the product page; `availability`
  is `"Available"` whenever that page shows a price, else `"Unavailable"`.
- `last_purchased_price` is the price actually paid in the **most recent** order
  containing the product (read from the order item list, not the product page).

These map to `Grocery_Product__c` fields `current_price__c`,
`availability__c`, and `last_purchased_price__c` respectively.

---

## Notes & constraints

- **Cart is the only write.** Scraping is read-only; `POST /api/cart` adds items
  to the cart but stops there — no purchase, checkout, cancel, or return.
- **No credential logging.** The username is masked; the password never reaches
  stdout.
- **No new Salesforce records.** Matches by `title__c` only; misses are logged
  and skipped.
- **Captchas are not bypassed.** If Amazon shows one, the scrape stops with a
  clear error and a screenshot.
- **Single tenant.** One Amazon account per deployment.
- Never commit `.env`. Already in `.gitignore`.
