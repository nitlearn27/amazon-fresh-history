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

A scrape typically takes 3–8 minutes. Poll `GET /api/products` until `status`
flips from `running` to results.

---

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate     # macOS / Linux
# or: .venv\Scripts\activate   # Windows

pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill in:

- `AMAZON_USERNAME` — your Amazon.in login email or phone
- `AMAZON_PASSWORD` — your Amazon.in password
- (optional) `SF_TOKEN_URL`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_API_ENDPOINT`

### First run — MUST be headed

Amazon nearly always presents a captcha or 2-step verification email the first
time it sees a new browser. Run the scraper in headed mode once so you can
clear the challenge by hand:

```bash
python scrape_amazon_orders.py --headed=true --orders=1
```

After this completes, `auth_state.json` is written. All subsequent runs reuse
that session and skip the login flow entirely.

### Salesforce Connected App (optional)

If you want sync, create a Connected App with:

- **OAuth flow:** Client Credentials
- **Scopes:** `api`, `refresh_token`
- **Run-as user** with read/update access to `Grocery_Product__c`

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

### Run the scraper directly (no Flask)

```bash
python scrape_amazon_orders.py                  # headed, 10 orders
python scrape_amazon_orders.py --orders=5
python scrape_amazon_orders.py --headed=false   # headless
```

### Re-sync the existing report to Salesforce (no re-scrape)

```bash
python salesforce_sync.py
```

---

## Deploying to Render

The repo is Docker-based and ready for Render's "New Web Service → connect repo"
flow. `render.yaml` declares every env var the service expects.

### One-time steps

1. **Run the scraper locally in headed mode** at least once to clear any
   captcha/2-step verification and capture `auth_state.json`.
2. **Push to GitHub.**
3. **Render → New → Web Service → connect repo.** Render auto-detects
   `Dockerfile` and `render.yaml`.
4. **Set environment variables** in the Render dashboard:

   | Variable             | Value                                                       |
   |----------------------|-------------------------------------------------------------|
   | `AMAZON_USERNAME`    | your Amazon.in login email/phone                            |
   | `AMAZON_PASSWORD`    | your Amazon.in password                                     |
   | `AMAZON_AUTH_STATE`  | **paste the entire contents of your local `auth_state.json`** |
   | `SF_TOKEN_URL`       | (optional) Salesforce OAuth token endpoint                  |
   | `SF_CLIENT_ID`       | (optional) Connected App consumer key                       |
   | `SF_CLIENT_SECRET`   | (optional) Connected App consumer secret                    |
   | `SF_API_ENDPOINT`    | (optional) `…/services/data/v57.0/sobjects/Grocery_Product__c/` |
   | `HEADLESS`           | `true` (already set in `render.yaml`)                       |

5. **Deploy.** Trigger one scrape via `POST /api/products`. After it finishes,
   Render logs print the fresh `auth_state.json` content — copy it back into
   the `AMAZON_AUTH_STATE` env var to extend the session lifespan.

### How session persistence works on Render

Render's filesystem is ephemeral — `auth_state.json` is wiped on every restart.
`app.py` rehydrates it from `AMAZON_AUTH_STATE` on container startup, so the
scraper finds it exactly where it expects.

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

`number_of_times_purchased` is the per-product aggregate across all scanned
orders, repeated on every row of the same title.

---

## Notes & constraints

- **Read-only.** No purchase, cancel, return, or any write action on Amazon.
- **No credential logging.** The username is masked; the password never reaches
  stdout.
- **No new Salesforce records.** Matches by `title__c` only; misses are logged
  and skipped.
- **Captchas are not bypassed.** If Amazon shows one, the scrape stops with a
  clear error and a screenshot.
- **Single tenant.** One Amazon account per deployment.
- Never commit `.env` or `auth_state.json`. Already in `.gitignore`.
