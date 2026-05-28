"""
Flask web service wrapping the Amazon Fresh scraper.
Render (and any cloud platform) needs an HTTP port — this provides it.

Endpoints:
  GET  /health         → liveness check
  GET  /docs           → Swagger UI playground
  GET  /openapi.json   → OpenAPI 3.0 spec
  GET  /api/products   → latest scrape output (full per-product field set:
                         product_name, date, number_of_times_purchased,
                         current_price, last_purchased_price, product_url,
                         image_url, category, availability, source, scraped_at)
  POST /api/products   → start a scrape in a background thread (body: {"orders": <int>})
"""

import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout/stderr so unicode characters print on Windows, AND enable
# line buffering so every print() lands in the log file/stream immediately —
# without this, redirected stdout (e.g. `python app.py > app.log`) is block-
# buffered and the scraper's progress lines never show up in real time.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Scrape state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state = {
    "running": False,
    "last_result": None,          # dict from orders_report.json
    "last_run_at": None,          # ISO timestamp
    "error": None,
}


def _run_scrape(num_orders: int) -> None:
    """Blocking function executed in a background thread."""
    headless = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")
    try:
        from scrape_amazon_orders import run
        asyncio.run(run(num_orders=num_orders, headless=headless))

        report_path = Path("orders_report.json")
        if report_path.exists():
            _state["last_result"] = json.loads(report_path.read_text())
            _state["error"] = None
        else:
            _state["error"] = "Scrape finished but orders_report.json was not created."

    except Exception as exc:
        _state["error"] = str(exc)
        print(f"[scrape] Error: {exc}")
    finally:
        _state["running"] = False
        _state["last_run_at"] = datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(tz=timezone.utc).isoformat()})


def _shape_products() -> list[dict]:
    """Return the latest scrape result with the full per-product field set."""
    result = _state.get("last_result") or {}
    out = []
    for p in result.get("products", []):
        # Tolerate legacy keys from older orders_report.json files.
        date = p.get("last_ordered_date") or p.get("purchase_date")
        count = p.get("number_of_times_purchased")
        if count is None:
            count = p.get("purchase_count_in_last_10_orders")
        out.append({
            "product_name": p.get("title"),
            "date": date,
            "number_of_times_purchased": count,
            "current_price": p.get("current_price"),
            "last_purchased_price": p.get("last_purchased_price"),
            "product_url": p.get("product_url"),
            "image_url": p.get("image_url"),
            "category": p.get("category"),
            "availability": p.get("availability"),
            "source": p.get("source"),
            "scraped_at": p.get("scraped_at"),
        })
    return out


@app.route("/api/products", methods=["GET"])
def api_get_products():
    """
    Returns the products from the most recent Amazon Fresh scrape in clean JSON.

    Response (200) when data is available:
      {
        "scraped_at": "...",
        "orders_scanned": 10,
        "products": [
          { "product_name": "...", "date": "YYYY-MM-DD", "number_of_times_purchased": 1, ... },
          ...
        ]
      }

    Response (202) if a scrape is currently running.
    Response (404) if no scrape has been run yet — call POST /api/products to start one.
    """
    if _state["running"]:
        return jsonify({
            "status": "running",
            "message": "A scrape is in progress. Try again in 2-5 minutes.",
        }), 202

    if _state["error"]:
        return jsonify({
            "status": "error",
            "error": _state["error"],
            "last_run_at": _state["last_run_at"],
        }), 500

    if _state["last_result"] is None:
        return jsonify({
            "status": "no_data",
            "message": "No scrape has been run yet. POST /api/products to start one.",
        }), 404

    result = _state["last_result"]
    return jsonify({
        "scraped_at": result.get("scraped_at"),
        "orders_scanned": result.get("orders_scanned", 0),
        "products": _shape_products(),
    }), 200


@app.route("/api/products", methods=["POST"])
def api_refresh_products():
    """
    Trigger a fresh scrape of the last N Amazon Fresh orders.

    Optional JSON body: { "orders": <int> }   (default: 10)

    Returns immediately with status 202.
    Poll GET /api/products until status switches from "running" to having data.
    """
    with _lock:
        if _state["running"]:
            return jsonify({
                "status": "running",
                "message": "A scrape is already in progress.",
            }), 409

        body = request.get_json(silent=True) or {}
        from scrape_amazon_orders import default_orders_to_scrape
        num_orders = int(body.get("orders", default_orders_to_scrape()))

        _state["running"] = True
        _state["error"] = None

    thread = threading.Thread(target=_run_scrape, args=(num_orders,), daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "orders_requested": num_orders,
        "message": "Scrape started. Poll GET /api/products until results appear.",
    }), 202


# ---------------------------------------------------------------------------
# OpenAPI 3.0 spec + Swagger UI served at /docs
# ---------------------------------------------------------------------------

_OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "Amazon Fresh Purchase History API",
        "version": "1.0.0",
        "description": (
            "Amazon Fresh order scraper + Salesforce `Grocery_Product__c` sync.\n\n"
            "After every successful scrape, the scraper drills into each unique product's "
            "Amazon page to capture current price, image, URL and availability (a product "
            "page that shows a price counts as available), and carries the last purchased "
            "price from the order history, then **updates** matching `Grocery_Product__c` "
            "records using `title__c` as the match field — existing records are updated; "
            "non-matching titles are skipped (no new records are ever created). Each row is "
            "stamped with `source__c=\"Amazon\"`.\n\n"
            "A scrape runs in a background thread and typically takes 3–8 minutes. "
            "Poll `GET /api/products` until the status flips from `running` to `ok`."
        ),
    },
    "tags": [
        {"name": "system",   "description": "Health and liveness."},
        {"name": "scrape",   "description": "Trigger Amazon Fresh scrapes."},
        {"name": "products", "description": "Read the latest scrape output."},
    ],
    "paths": {
        "/health": {
            "get": {
                "tags": ["system"],
                "summary": "Liveness probe",
                "description": "Returns `ok` and the current server timestamp.",
                "responses": {
                    "200": {
                        "description": "Server is up.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Health"},
                            "example": {"status": "ok", "timestamp": "2026-05-26T03:30:00+00:00"},
                        }},
                    }
                },
            }
        },
        "/api/products": {
            "get": {
                "tags": ["products"],
                "summary": "Get products from the last scrape (clean shape)",
                "description": (
                    "Returns the products in `{product_name, date, number_of_times_purchased, ...}` shape."
                ),
                "responses": {
                    "200": {
                        "description": "Products available.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ProductsOk"},
                        }},
                    },
                    "202": {
                        "description": "A scrape is currently running.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Running"},
                        }},
                    },
                    "404": {
                        "description": "No scrape has been run yet.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                            "example": {
                                "status": "no_data",
                                "message": "No scrape has been run yet. POST /api/products to start one.",
                            },
                        }},
                    },
                    "500": {
                        "description": "The last scrape errored.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ScrapeError"},
                        }},
                    },
                },
            },
            "post": {
                "tags": ["scrape"],
                "summary": "Refresh products (start a scrape)",
                "description": "Starts an Amazon Fresh scrape in a background thread. Returns `202 started` immediately.",
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/ScrapeRequest"},
                        "example": {"orders": 10},
                    }},
                },
                "responses": {
                    "202": {
                        "description": "Scrape started.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ScrapeStarted"},
                        }},
                    },
                    "409": {
                        "description": "A scrape is already running.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                        }},
                    },
                },
            },
        },
    },
    "components": {
        "schemas": {
            "Health": {
                "type": "object",
                "properties": {
                    "status":    {"type": "string", "example": "ok"},
                    "timestamp": {"type": "string", "format": "date-time"},
                },
            },
            "ScrapeRequest": {
                "type": "object",
                "properties": {
                    "orders": {
                        "type": "integer", "minimum": 1, "maximum": 50,
                        "description": (
                            "Number of recent Amazon Fresh orders to scrape. "
                            "If omitted, falls back to ORDERS_TO_SCRAPE from .env (default 10)."
                        ),
                    },
                },
            },
            "ScrapeStarted": {
                "type": "object",
                "properties": {
                    "status":           {"type": "string", "example": "started"},
                    "orders_requested": {"type": "integer", "example": 10},
                    "message":          {"type": "string"},
                },
            },
            "Running": {
                "type": "object",
                "properties": {
                    "status":  {"type": "string", "example": "running"},
                    "message": {"type": "string"},
                },
            },
            "Error": {
                "type": "object",
                "properties": {
                    "status":  {"type": "string"},
                    "error":   {"type": "string"},
                    "message": {"type": "string"},
                },
            },
            "ScrapeError": {
                "type": "object",
                "properties": {
                    "status":      {"type": "string", "example": "error"},
                    "error":       {"type": "string"},
                    "last_run_at": {"type": "string", "format": "date-time"},
                },
            },
            "ProductsOk": {
                "type": "object",
                "properties": {
                    "scraped_at":     {"type": "string", "format": "date-time"},
                    "orders_scanned": {"type": "integer"},
                    "products": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/CleanProduct"},
                    },
                },
            },
            "CleanProduct": {
                "type": "object",
                "properties": {
                    "product_name":              {"type": "string"},
                    "date":                      {"type": "string", "nullable": True, "example": "2026-04-12"},
                    "number_of_times_purchased": {"type": "integer"},
                    "current_price":             {"type": "number", "nullable": True, "example": 199.0,
                                                  "description": "Live price from the product page; also determines availability."},
                    "last_purchased_price":      {"type": "number", "nullable": True, "example": 185.0,
                                                  "description": "Price paid in the most recent order containing this product."},
                    "product_url":               {"type": "string", "nullable": True, "example": "https://www.amazon.in/dp/B0..."},
                    "image_url":                 {"type": "string", "nullable": True, "example": "https://m.media-amazon.com/images/..."},
                    "category":                  {"type": "string", "nullable": True, "example": "Grocery"},
                    "availability":              {"type": "string", "nullable": True, "example": "Available"},
                    "source":                    {"type": "string", "nullable": True, "example": "Amazon"},
                    "scraped_at":                {"type": "string", "format": "date-time", "nullable": True},
                },
            },
        }
    },
}


_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Amazon Fresh Purchase History API — Docs</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui.css">
  <link rel="icon" type="image/svg+xml"
        href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><text y='52' font-size='52'>🥬</text></svg>">
  <style>
    body { margin: 0; background: #fafafa; }
    .topbar { display: none; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script>
  <script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-standalone-preset.js"></script>
  <script>
    window.ui = SwaggerUIBundle({
      url: "/openapi.json",
      dom_id: "#swagger-ui",
      deepLinking: true,
      presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIStandalonePreset
      ],
      plugins: [SwaggerUIBundle.plugins.DownloadUrl],
      layout: "BaseLayout",
      tryItOutEnabled: true,
      persistAuthorization: true,
      defaultModelsExpandDepth: 0,
      docExpansion: "list"
    });
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return redirect("/docs", code=302)


@app.route("/docs", methods=["GET"])
def docs():
    return Response(_SWAGGER_HTML, mimetype="text/html; charset=utf-8")


@app.route("/openapi.json", methods=["GET"])
def openapi_spec():
    return jsonify(_OPENAPI_SPEC)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    print(f"[server] Starting on port {port}")
    app.run(host="0.0.0.0", port=port)
