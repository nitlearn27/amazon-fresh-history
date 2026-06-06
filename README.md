# Amazon Fresh Purchase History → Salesforce

A Flask web service that logs into **amazon.in** with an email/phone + password,
scrapes the last N **Amazon Fresh** orders, and syncs each unique product to
Salesforce `Grocery_Product__c` records.

- **Sibling project** to `purchase-history` (Flipkart). Both write to the same
  Salesforce object; the `source__c` field distinguishes the two
  (`"Flipkart"` vs `"Amazon"`).
- **Salesforce sync is update-only** — existing `Grocery_Product__c` records are
  matched by `title__c` and have product fields patched. New records are
  **never** created.
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

### OTP / 2-step verification

Every run does a fresh login (no session is cached). Amazon may ask for a
2-step verification code. When it does, the scraper polls
`Purchase_Info__c.my_amazon_otp__c` in Salesforce every 5 s for up to
3 minutes — paste the OTP Amazon sent into that field and save; the scraper
picks it up automatically, submits it, and immediately blanks the field.

This works the same way in headed local runs and headless Render runs.

### Delivery location (Fresh availability)

Amazon Fresh prices and stock are **per delivery location**. With no location
set, every Fresh product page reports *"currently unavailable"* and no price.
So right after login the scraper sets the "Deliver to" location:

1. It first tries to select the saved address whose text contains
   `DELIVERY_ADDRESS_PREFIX` (default `82, Flat No 6`).
2. If no matching saved address is found, it enters `DELIVERY_PINCODE`
   (default `560094`).

Both are optional env vars — the built-in defaults work without configuration.
Set `DELIVERY_ADDRESS_PREFIX` to a substring unique to the address you want.

### Salesforce Connected App

If you want sync, create a Connected App with:

- **OAuth flow:** Client Credentials
- **Scopes:** `api`, `refresh_token`
- **Run-as user** with read/update access to `Grocery_Product__c` (including the
  `last_purchased_price__c` field) and read/edit on `Purchase_Info__c.my_amazon_otp__c`
  for the OTP bridge.

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
   | `DELIVERY_ADDRESS_PREFIX` | _(optional)_ saved-address substring to deliver to; default `82, Flat No 6` |
   | `DELIVERY_PINCODE`        | _(optional)_ pincode fallback; `560094` in `render.yaml`         |

   Only `AMAZON_*` are strictly required to deploy. The `SF_*` block enables
   Salesforce sync + the OTP bridge; the rest have working defaults.

4. **Deploy.** Trigger a scrape via `POST /api/products`.

### OTP on Render

Each scrape does a full login. If Amazon asks for a 2-step verification code,
watch the Render logs for the `[auth] ACTION REQUIRED` line, then paste the
OTP into `Purchase_Info__c.my_amazon_otp__c` in Salesforce. The scraper picks
it up within 5 seconds, submits it, and continues automatically.

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
      "source": "Amazon",
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
