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
import re
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

# Add-to-cart state — same shape as _state. Kept separate from the scrape state
# so each operation reports its own last result / error independently.
_cart_state = {
    "running": False,
    "last_result": None,          # dict from add_products_to_cart()
    "last_run_at": None,
    "error": None,
}


def _account_busy() -> bool:
    """True if a scrape or a cart run is in flight. Both launch their own
    Chromium against the single Amazon account, so they must not overlap."""
    return _state["running"] or _cart_state["running"]


def _headless() -> bool:
    return os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")


def _run_scrape(num_orders: int) -> None:
    """Blocking function executed in a background thread."""
    headless = _headless()
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


def _run_cart(names: list) -> None:
    """Blocking add-to-cart run executed in a background thread.

    Catches BaseException (not just Exception) so that a login() sys.exit(1)
    raised inside this thread surfaces as an error rather than dying silently."""
    headless = _headless()
    try:
        from amazon_cart import add_products_to_cart
        _cart_state["last_result"] = asyncio.run(
            add_products_to_cart(names, headless=headless)
        )
        _cart_state["error"] = None
    except BaseException as exc:
        _cart_state["error"] = str(exc) or exc.__class__.__name__
        print(f"[cart] Error: {exc}")
    finally:
        _cart_state["running"] = False
        _cart_state["last_run_at"] = datetime.now(tz=timezone.utc).isoformat()


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
        if _account_busy():
            return jsonify({
                "status": "running",
                "message": "A scrape or cart run is already in progress.",
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


@app.route("/api/cart", methods=["GET"])
def api_get_cart():
    """
    Returns the result of the most recent add-to-cart run.

    Response (200) when a run has completed:
      { "requested": 3, "added": [...], "not_found": [...], "cart_count": 5, "added_at": "..." }

    Response (202) if a cart run is currently in progress.
    Response (404) if no cart run has been started yet.
    Response (500) if the last cart run errored.
    """
    if _cart_state["running"]:
        return jsonify({
            "status": "running",
            "message": "A cart run is in progress. Try again shortly.",
        }), 202

    if _cart_state["error"]:
        return jsonify({
            "status": "error",
            "error": _cart_state["error"],
            "last_run_at": _cart_state["last_run_at"],
        }), 500

    if _cart_state["last_result"] is None:
        return jsonify({
            "status": "no_data",
            "message": "No cart run has been started yet. POST /api/cart to start one.",
        }), 404

    return jsonify(_cart_state["last_result"]), 200


@app.route("/api/cart", methods=["POST"])
def api_add_to_cart():
    """
    Add one unit of each named Amazon Fresh product to the cart.

    JSON body: { "products": ["name1", "name2", ...] }

    Each name is searched on Amazon Fresh, fuzzy-matched to the best result, and
    added if the match clears the confidence threshold. Unmatched names are
    reported in the result's `not_found` list. The run NEVER proceeds to
    checkout — items are left in the cart for manual review.

    Returns 202 immediately. Poll GET /api/cart for the result.
    """
    with _lock:
        if _account_busy():
            return jsonify({
                "status": "running",
                "message": "A scrape or cart run is already in progress.",
            }), 409

        body = request.get_json(silent=True) or {}
        products = body.get("products")
        if (
            not isinstance(products, list)
            or not products
            or not all(isinstance(p, str) and p.strip() for p in products)
        ):
            return jsonify({
                "status": "invalid_request",
                "message": 'Body must be {"products": ["name1", "name2", ...]} with at least one non-empty name.',
            }), 400

        names = [p.strip() for p in products]
        _cart_state["running"] = True
        _cart_state["error"] = None

    thread = threading.Thread(target=_run_cart, args=(names,), daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "products_requested": len(names),
        "message": "Add-to-cart started. Poll GET /api/cart until results appear.",
    }), 202


@app.route("/api/otp", methods=["GET"])
def api_otp_status():
    """
    Report whether the scraper is currently blocked waiting for a 2-step
    verification OTP, and how long a pushed code stays valid.

    Response (200):
      { "waiting": true, "waiting_since": "...", "ttl_seconds": 300 }
    """
    from otp_store import store as otp_store
    return jsonify(otp_store.status()), 200


@app.route("/api/otp", methods=["POST"])
def api_submit_otp():
    """
    Hand a 2-step verification OTP to a scrape/cart run that is waiting for one.

    JSON body: { "otp": "123456" }   (4–8 digits)

    The code is held in memory only, consumed within ~1s by the waiting run, and
    expires after OTP_TTL_SECONDS (default 300s). Returns 409 if nothing is
    currently waiting for an OTP — check GET /api/otp first.
    """
    from otp_store import store as otp_store

    if not otp_store.is_waiting():
        return jsonify({
            "status": "no_otp_requested",
            "message": "No run is waiting for an OTP right now. Check GET /api/otp.",
        }), 409

    body = request.get_json(silent=True) or {}
    code = str(body.get("otp", "")).strip()
    if not re.fullmatch(r"\d{4,8}", code):
        print(f"[otp] POST /api/otp rejected: {len(code)}-char value is not 4-8 digits.")
        return jsonify({
            "status": "invalid_request",
            "message": 'Body must be {"otp": "<4-8 digits>"}.',
        }), 400

    otp_store.submit(code)
    print(f"[otp] POST /api/otp accepted: {len(code)}-digit code stored for the waiting run.")
    return jsonify({
        "status": "accepted",
        "message": "OTP received. The waiting run will pick it up within ~1 second.",
    }), 200


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
            "price from the order history, then **upserts** `Grocery_Product__c` "
            "records using `title__c` as the external ID — existing titles are updated and "
            "new titles are created. Each row is stamped with `source__c` = `\"Amazon Now\"` "
            "(items from Amazon Now, the /tez/ quick-commerce service) or `\"Amazon Fresh\"` "
            "(classic Amazon Fresh items).\n\n"
            "A scrape runs in a background thread and typically takes 3–8 minutes. "
            "Poll `GET /api/products` until the status flips from `running` to `ok`."
        ),
    },
    "tags": [
        {"name": "system",   "description": "Health and liveness."},
        {"name": "scrape",   "description": "Trigger Amazon Fresh scrapes."},
        {"name": "products", "description": "Read the latest scrape output."},
        {"name": "cart",     "description": "Add Amazon Fresh products to the cart by name."},
        {"name": "auth",     "description": "Hand the 2-step verification OTP to a waiting run."},
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
        "/api/cart": {
            "get": {
                "tags": ["cart"],
                "summary": "Get the result of the last add-to-cart run",
                "description": (
                    "Returns how each requested name resolved: `added` (matched + put in "
                    "the cart) vs `not_found` (no confident match)."
                ),
                "responses": {
                    "200": {
                        "description": "Cart run completed.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/CartResult"},
                        }},
                    },
                    "202": {
                        "description": "A cart run is currently in progress.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Running"},
                        }},
                    },
                    "404": {
                        "description": "No cart run has been started yet.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                        }},
                    },
                    "500": {
                        "description": "The last cart run errored.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ScrapeError"},
                        }},
                    },
                },
            },
            "post": {
                "tags": ["cart"],
                "summary": "Add Amazon Fresh products to the cart by name",
                "description": (
                    "Searches Amazon Fresh for each name, fuzzy-matches the best result, and "
                    "adds one unit of it to the cart. Unmatched names are skipped and reported. "
                    "Never proceeds to checkout. Returns `202 started` immediately; poll "
                    "`GET /api/cart` for the result."
                ),
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/CartRequest"},
                        "example": {"products": [
                            "Amazon Brand - Vedaka Organic Toor Dal, 500g",
                            "Amul Gold Full Cream Milk 500ml",
                        ]},
                    }},
                },
                "responses": {
                    "202": {
                        "description": "Add-to-cart started.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/CartStarted"},
                        }},
                    },
                    "400": {
                        "description": "Missing or malformed products array.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                        }},
                    },
                    "409": {
                        "description": "A scrape or cart run is already in progress.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                        }},
                    },
                },
            },
        },
        "/api/otp": {
            "get": {
                "tags": ["auth"],
                "summary": "Is a run waiting for an OTP?",
                "description": (
                    "Reports whether a scrape/cart run is currently blocked on Amazon's "
                    "2-step verification screen, and how long a pushed code stays valid."
                ),
                "responses": {
                    "200": {
                        "description": "OTP wait status.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/OtpStatus"},
                            "example": {
                                "waiting": True,
                                "waiting_since": "2026-06-21T10:15:00+00:00",
                                "ttl_seconds": 300,
                            },
                        }},
                    },
                },
            },
            "post": {
                "tags": ["auth"],
                "summary": "Submit the 2-step verification OTP",
                "description": (
                    "Hands the OTP Amazon just emailed/SMS'd to a run that is waiting for it. "
                    "The code is held in memory only, consumed within ~1s, and expires after "
                    "`OTP_TTL_SECONDS` (default 300). Returns `409` if nothing is waiting."
                ),
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/OtpRequest"},
                        "example": {"otp": "123456"},
                    }},
                },
                "responses": {
                    "200": {
                        "description": "OTP accepted.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/OtpAccepted"},
                        }},
                    },
                    "400": {
                        "description": "Missing or malformed OTP.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                        }},
                    },
                    "409": {
                        "description": "No run is waiting for an OTP.",
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
            "OtpStatus": {
                "type": "object",
                "properties": {
                    "waiting":       {"type": "boolean", "example": True},
                    "waiting_since": {"type": "string", "format": "date-time", "nullable": True},
                    "ttl_seconds":   {"type": "integer", "example": 300,
                                      "description": "How long a pushed OTP stays valid."},
                },
            },
            "OtpRequest": {
                "type": "object",
                "required": ["otp"],
                "properties": {
                    "otp": {"type": "string", "example": "123456",
                            "description": "The 4–8 digit code Amazon emailed/SMS'd."},
                },
            },
            "OtpAccepted": {
                "type": "object",
                "properties": {
                    "status":  {"type": "string", "example": "accepted"},
                    "message": {"type": "string"},
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
                    "source":                    {"type": "string", "nullable": True, "example": "Amazon Fresh",
                                                  "description": "\"Amazon Now\" or \"Amazon Fresh\", per product origin."},
                    "scraped_at":                {"type": "string", "format": "date-time", "nullable": True},
                },
            },
            "CartRequest": {
                "type": "object",
                "required": ["products"],
                "properties": {
                    "products": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                        "description": "Product names to search on Amazon Fresh and add (one unit each).",
                        "example": ["Amazon Brand - Vedaka Organic Toor Dal, 500g"],
                    },
                },
            },
            "CartStarted": {
                "type": "object",
                "properties": {
                    "status":              {"type": "string", "example": "started"},
                    "products_requested":  {"type": "integer", "example": 2},
                    "message":             {"type": "string"},
                },
            },
            "CartItem": {
                "type": "object",
                "properties": {
                    "requested_name": {"type": "string", "example": "Amul Gold Full Cream Milk 500ml"},
                    "matched_title":  {"type": "string", "example": "Amul Gold Homogenised Full Cream Milk, 500 ml Pouch"},
                    "score":          {"type": "number", "example": 0.82,
                                       "description": "difflib similarity ratio in [0, 1]."},
                    "price":          {"type": "number", "nullable": True, "example": 35.0},
                    "product_url":    {"type": "string", "nullable": True, "example": "https://www.amazon.in/dp/B0..."},
                    "asin":           {"type": "string", "nullable": True, "example": "B0..."},
                },
            },
            "NotFoundItem": {
                "type": "object",
                "properties": {
                    "requested_name": {"type": "string"},
                    "best_candidate": {"type": "string", "nullable": True,
                                       "description": "Closest title seen, even though it was below threshold."},
                    "score":          {"type": "number", "example": 0.41},
                    "reason":         {"type": "string", "nullable": True,
                                       "example": "best score 0.41 below threshold 0.6"},
                },
            },
            "CartResult": {
                "type": "object",
                "properties": {
                    "requested":  {"type": "integer", "example": 2},
                    "added":      {"type": "array", "items": {"$ref": "#/components/schemas/CartItem"}},
                    "not_found":  {"type": "array", "items": {"$ref": "#/components/schemas/NotFoundItem"}},
                    "cart_count": {"type": "integer", "example": 5,
                                   "description": "Total items in the cart after the run (nav badge)."},
                    "added_at":   {"type": "string", "format": "date-time"},
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
