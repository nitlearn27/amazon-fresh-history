"""
Add amazon.in products to the cart by name — Amazon Now first, Fresh fallback.

Given a list of real product names, this module searches the Amazon Now
(/tez/ quick-commerce) storefront for each, fuzzy-matches the best in-stock
result above a confidence threshold, and adds one unit to the Now cart (which
is separate from the main Amazon cart). Only when Now has no confident match
does it fall back to the classic Amazon Fresh search and cart. Unmatched names
are skipped and reported. The flow NEVER proceeds to checkout/payment —
matched items are left in the cart for manual review.

Login, OTP/captcha handling, and delivery-location setup are reused wholesale
from scrape_amazon_orders.open_logged_in_page (Fresh availability and the
add-to-cart button are keyed on the delivery location, so that setup matters
here exactly as it does for scraping).
"""

import argparse
import asyncio
import difflib
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from scrape_amazon_orders import AMAZON_HOME, AMAZON_NOW_BRAND_ID, open_logged_in_page

# ---------------------------------------------------------------------------
# Selectors — update here when Amazon changes its markup. Mirrors the SELECTORS
# convention in scrape_amazon_orders.py. Prefer IDs / data-attributes, fall
# back to role/text.
# ---------------------------------------------------------------------------
CART_SELECTORS = {
    # Search results: one card per result, ASIN on the data-asin attribute.
    "result_card": "div[data-component-type='s-search-result'][data-asin]",
    # Per-card add-to-cart control (several A/B variants on Fresh/Now results).
    "add_button": (
        "button[name='submit.addToCart'], input[name='submit.addToCart'], "
        "button[aria-label*='Add to cart' i], "
        "[id*='addToCart'] button, [data-csa-c-type='widget'] button"
    ),
    # Global cart-count badge in the nav bar.
    "cart_count": "#nav-cart-count, #nav-cart-count-container #nav-cart-count",
    # Amazon Now (/tez/ SPA) search results: per-ASIN Add button ({asin} is
    # substituted), plus a generic variant for fallback scans.
    "now_add_button": "button[data-csa-c-content-id='AsinFaceout-AddToCart-{asin}']",
    "now_add_button_any": "button[data-csa-c-slot-id='AsinFaceout-AddToCart']",
    # When the item is already in the Now cart, the Add button is replaced by a
    # quantity stepper; its "+" button adds one more unit.
    "now_qty_increase": (
        "button[data-csa-c-content-id='AsinFaceout-AddToCartQtyStepper-{asin}']"
        "[aria-label*='Increase' i]"
    ),
    # Products with size variants render "N options | Add" instead of a plain
    # Add button; clicking it opens a bottom sheet with one Add per variant.
    "now_add_variation": "[data-csa-c-content-id='AsinFaceout-AddToCartWithVariation-{asin}']",
    "now_variant_add": "button[data-csa-c-content-id='AsinVariationsBottomBanner-AddToCart-{asin}']",
}

# Amazon Fresh / "Now" store node. Restricting the search to this node keeps
# results within the Fresh catalogue (which is what we can add to the cart and
# what the order scraper reports). Confirm during headed verification.
FRESH_STORE_NODE = "nowstore"

# Minimum difflib similarity ratio for a search result title to count as a
# match for the requested name. Tune during headed verification.
MATCH_THRESHOLD = 0.6

# How many top search results to consider per name.
MAX_RESULTS_TO_SCAN = 12


def _search_url(name: str) -> str:
    return f"{AMAZON_HOME}/s?k={quote(name)}&i={FRESH_STORE_NODE}"


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace for fuzzy comparison."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _best_match(query: str, candidate_titles: list[str]) -> tuple[int, float]:
    """Return (index, score) of the candidate title most similar to `query`.
    Score is in [0, 1]. Returns (-1, 0.0) for an empty list.

    The score is the max of two signals:
    - difflib character ratio — good for fullish product names;
    - word coverage — when EVERY word of the query appears in the title, the
      title scores 0.6+ (so short generic queries like "bhindi" still clear
      MATCH_THRESHOLD), with tighter titles (fewer extra words) ranking
      higher. A query word missing from the title gets no boost, so
      "bhindi masala powder" does not match plain bhindi."""
    q_tokens = set(_normalize(query).split())
    nq = _normalize(query)
    best_idx, best_score = -1, 0.0
    for i, title in enumerate(candidate_titles):
        nt = _normalize(title)
        score = difflib.SequenceMatcher(None, nq, nt).ratio()
        t_tokens = set(nt.split())
        if q_tokens and t_tokens and q_tokens <= t_tokens:
            score = max(score, 0.6 + 0.4 * (len(q_tokens) / len(t_tokens)))
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx, best_score


async def _read_now_cart_count(page) -> int | None:
    """Item count in the Amazon Now cart (separate from the main cart),
    read from the getcart XHR the /tez/ SPA fires on load. None if unknown.

    The body is parsed eagerly inside a response listener — the SPA's follow-up
    navigations dispose response bodies, so waiting via expect_response and
    reading afterwards races and loses."""
    payloads: list[dict] = []

    async def _capture(resp):
        if "tez/order/getcart" in resp.url and resp.status == 200:
            try:
                payloads.append(await resp.json())
            except Exception:
                pass

    page.on("response", _capture)
    try:
        # Any /tez/browse/ page fires getcart on load; the bare /tez home and
        # cart routes do not.
        await page.goto(_now_search_url("milk"), wait_until="domcontentloaded")
        for _ in range(20):
            if payloads:
                break
            await page.wait_for_timeout(500)
    except Exception as exc:
        print(f"[cart] Could not read the Amazon Now cart count ({exc}).")
        return None
    finally:
        page.remove_listener("response", _capture)

    if not payloads:
        print("[cart] Amazon Now getcart XHR not seen; cart count unknown.")
        return None
    payload = payloads[0]

    items = (((payload or {}).get("data") or {}).get("cartResponse") or {}).get("cartItems")
    if not isinstance(items, list):
        return None
    try:
        return sum(int(i.get("quantity") or 1) for i in items)
    except Exception:
        return len(items)


async def _read_cart_count(page) -> int:
    """Current cart item count from the nav badge; 0 if not readable."""
    try:
        loc = page.locator(CART_SELECTORS["cart_count"]).first
        if await loc.count() == 0:
            return 0
        text = (await loc.inner_text(timeout=2_000)) or ""
        m = re.search(r"\d+", text)
        return int(m.group(0)) if m else 0
    except Exception:
        return 0


async def _scan_results(page) -> list[dict]:
    """Collect up to MAX_RESULTS_TO_SCAN search-result cards as
    {asin, title, price} dicts, in page order.

    Title extraction is deliberately tolerant: Amazon cards often carry a short
    brand byline `<h2>` (e.g. "Aashirvaad") above the real product title, so we
    gather several candidate text nodes per card and keep the longest one rather
    than trusting the first `h2`."""
    raw = await page.evaluate(
        r"""
        (maxResults) => {
          const priceFromCard = (card) => {
            const off = card.querySelector("span.a-price > span.a-offscreen, .a-price .a-offscreen");
            const text = (off ? off.textContent : '') || '';
            const m = text.match(/₹\s*([\d,]+(?:\.\d+)?)/);
            return m ? parseFloat(m[1].replace(/,/g, '')) : null;
          };
          const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
          const titleFromCard = (card) => {
            // Candidate nodes most likely to hold the full product title.
            const sels = [
              "[data-cy='title-recipe'] a span",
              "[data-cy='title-recipe'] a",
              "a.a-link-normal[href*='/dp/'] span",
              "a.a-link-normal[href*='/gp/product/'] span",
              "h2 a span",
              "h2 a",
              "h2 span",
              "h2",
            ];
            let best = '';
            for (const sel of sels) {
              for (const el of card.querySelectorAll(sel)) {
                const t = clean(el.textContent);
                if (t.length > best.length) best = t;
              }
            }
            return best;
          };
          const hrefFromCard = (card) => {
            const a = card.querySelector("a.a-link-normal[href*='/dp/'], a.a-link-normal[href*='/gp/product/']");
            return a ? a.href : null;
          };
          const cards = Array.from(
            document.querySelectorAll("div[data-component-type='s-search-result'][data-asin]")
          ).filter(c => (c.getAttribute('data-asin') || '').trim());
          const out = [];
          for (const card of cards) {
            const title = titleFromCard(card);
            if (!title || title.length < 3) continue;
            out.push({
              asin: card.getAttribute('data-asin').trim(),
              title,
              price: priceFromCard(card),
              href: hrefFromCard(card),
            });
            if (out.length >= maxResults) break;
          }
          return out;
        }
        """,
        MAX_RESULTS_TO_SCAN,
    )
    return raw or []


async def _click_add_for_asin(page, asin: str) -> bool:
    """Click the add-to-cart control inside the result card for `asin`.
    Returns True if a click landed on some control."""
    card = page.locator(f"div[data-component-type='s-search-result'][data-asin='{asin}']").first
    if await card.count() == 0:
        return False
    try:
        await card.scroll_into_view_if_needed(timeout=3_000)
    except Exception:
        pass

    # Preferred: the centralised add-button selector.
    btn = card.locator(CART_SELECTORS["add_button"]).first
    try:
        if await btn.count() > 0 and await btn.is_visible():
            await btn.click(timeout=5_000)
            return True
    except Exception:
        pass

    # Fallback: any button/role within the card whose label says "add".
    add_pattern = re.compile(r"\badd\b", re.I)
    for loc in (
        card.get_by_role("button", name=add_pattern),
        card.get_by_role("link", name=add_pattern),
        card.get_by_text(add_pattern, exact=False),
    ):
        try:
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=5_000)
                return True
        except Exception:
            continue
    return False


def _now_search_url(name: str) -> str:
    return (
        f"{AMAZON_HOME}/tez/browse/search"
        f"?qcbrand={AMAZON_NOW_BRAND_ID}&searchKeyword={quote(name)}"
    )


async def _scan_now_results(page, name: str) -> list[dict]:
    """Search Amazon Now (/tez/) for `name` and return in-stock candidates as
    {asin, title, price} dicts.

    The SPA's grid is obfuscated styled-components, but it hydrates from one
    JSON XHR (tez/browse/searchByKeyword) we capture in-page — calling that
    endpoint directly returns 204 (needs the SPA's CSRF headers)."""
    try:
        async with page.expect_response(
            lambda r: "tez/browse/searchByKeyword" in r.url, timeout=20_000
        ) as resp_info:
            await page.goto(_now_search_url(name), wait_until="domcontentloaded")
        resp = await resp_info.value
        payload = await resp.json()
    except PlaywrightTimeoutError:
        print(f"[cart] Amazon Now search XHR did not fire for {name!r}.")
        return []
    except Exception as exc:
        print(f"[cart] Amazon Now search failed for {name!r}: {exc}")
        return []

    raw = (((payload or {}).get("data") or {}).get("searchResponse") or {}).get("products") or []
    candidates = []
    for p in raw:
        asin = (p.get("asin") or "").strip()
        title = (p.get("title") or "").strip()
        if not asin or len(title) < 3:
            continue
        if (p.get("availability") or {}).get("type") != "IN_STOCK":
            continue
        variations = {}
        for va, v in (p.get("variations") or {}).items():
            if (v.get("availability") or {}).get("type") != "IN_STOCK":
                continue
            variations[va] = {
                "pack_size": v.get("packSize") or "",
                "price": (v.get("buyingPrice") or {}).get("amount"),
            }
        candidates.append({
            "asin": asin,
            "title": title,
            "price": (p.get("buyingPrice") or {}).get("amount"),
            "variations": variations,
        })
        if len(candidates) >= MAX_RESULTS_TO_SCAN:
            break
    return candidates


def _pick_size_variant(query: str, variations: dict) -> str | None:
    """Variant ASIN whose pack size (e.g. "5 kg") is named in the query, if
    any. Comparison ignores whitespace and case ("Toor 5kg" matches "5 kg")."""
    nq = re.sub(r"\s+", "", query.lower())
    for vasin, v in variations.items():
        ps = re.sub(r"\s+", "", (v.get("pack_size") or "").lower())
        if ps and ps in nq:
            return vasin
    return None


async def _visible(locator) -> bool:
    try:
        return await locator.count() > 0 and await locator.is_visible()
    except Exception:
        return False


async def _click_now_add_for_asin(page, asin: str, variant_asin: str | None = None) -> bool:
    """Click the Amazon Now Add control for `asin` and return True if the SPA
    acknowledged the add (cart XHR, or the button turning into a stepper).

    Handles the three card variants:
    - plain Add button;
    - quantity stepper "+" when the item is already in the Now cart;
    - "N options | Add" for size-variant products — opens the bottom sheet and
      clicks the Add for `variant_asin` (a size named in the query) or for
      `asin` itself (the card's displayed default, e.g. 1 kg)."""
    add_btn = page.locator(CART_SELECTORS["now_add_button"].format(asin=asin)).first
    stepper = page.locator(CART_SELECTORS["now_qty_increase"].format(asin=asin)).first
    variation = page.locator(CART_SELECTORS["now_add_variation"].format(asin=asin)).first

    btn = None
    for _ in range(20):
        if await _visible(add_btn):
            btn = add_btn
            break
        if await _visible(stepper):
            print(f"[cart] {asin} is already in the Now cart — adding one more unit.")
            btn = stepper
            break
        if await _visible(variation):
            break
        await page.wait_for_timeout(500)

    if btn is None and await _visible(variation):
        print(f"[cart] {asin} has size options — opening the variant sheet.")
        try:
            await variation.scroll_into_view_if_needed(timeout=3_000)
            await variation.click(timeout=5_000)
        except Exception as exc:
            print(f"[cart] Could not open the variant sheet for {asin}: {exc}")
            return False
        for target in dict.fromkeys([variant_asin or asin, asin]):
            vbtn = page.locator(CART_SELECTORS["now_variant_add"].format(asin=target)).first
            try:
                await vbtn.wait_for(state="visible", timeout=8_000)
                btn = vbtn
                break
            except Exception:
                continue
        if btn is None:
            print(f"[cart] Variant sheet opened but no Add button found for {asin}.")
            return False

    if btn is None:
        print(f"[cart] No Amazon Now Add control rendered for {asin}.")
        return False
    try:
        await btn.scroll_into_view_if_needed(timeout=3_000)
    except Exception:
        pass

    # The SPA confirms an add with a /tez/ cart XHR; the Add button is also
    # replaced by a quantity stepper. Accept either signal.
    try:
        async with page.expect_response(
            lambda r: "/tez/" in r.url and "cart" in r.url.lower() and r.status < 400,
            timeout=10_000,
        ):
            await btn.click(timeout=5_000)
        return True
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1_000)
        stepper_gone = await btn.count() == 0 or not await btn.is_visible()
        if stepper_gone:
            print(f"[cart] No cart XHR seen for {asin}, but the Add button changed — treating as added.")
        return stepper_gone
    except Exception as exc:
        print(f"[cart] Amazon Now Add click failed for {asin}: {exc}")
        return False


async def add_one_now(page, name: str) -> dict:
    """Search Amazon Now for `name`, fuzzy-match the best in-stock result, and
    add one unit to the Now cart. Same record shape as add_one()."""
    print(f"\n[cart] Searching Amazon Now for {name!r}…")
    record = {
        "requested_name": name,
        "matched_title": None,
        "asin": None,
        "score": 0.0,
        "price": None,
        "product_url": None,
        "added": False,
        "reason": None,
        "source": "Amazon Now",
    }

    candidates = await _scan_now_results(page, name)
    if not candidates:
        record["reason"] = "no search results"
        print(f"[cart] No Amazon Now results for {name!r}.")
        return record

    print(f"[cart] Scanned {len(candidates)} Now result(s):")
    for c in candidates:
        print(f"         • [{c['asin']}] {c['title'][:90]}")

    idx, score = _best_match(name, [c["title"] for c in candidates])
    record["score"] = round(score, 3)
    if idx < 0 or score < MATCH_THRESHOLD:
        best = candidates[idx]["title"] if idx >= 0 else None
        record["reason"] = f"best score {score:.2f} below threshold {MATCH_THRESHOLD}"
        record["best_candidate"] = best
        print(f"[cart] No confident Now match for {name!r} (best={best!r} @ {score:.2f}).")
        return record

    match = candidates[idx]
    record["matched_title"] = match["title"]
    record["asin"] = match["asin"]
    record["price"] = match["price"]
    record["product_url"] = f"{AMAZON_HOME}/dp/{match['asin']}"
    print(f"[cart] Now match for {name!r}: {match['title'][:70]!r} @ {score:.2f}")

    # If the query names a pack size that exists as a variant (e.g. "Toor 5kg"),
    # prefer that variant; otherwise the card's default size is added.
    variant_asin = _pick_size_variant(name, match.get("variations") or {})
    if variant_asin:
        variant = match["variations"][variant_asin]
        record["asin"] = variant_asin
        record["price"] = variant["price"]
        record["product_url"] = f"{AMAZON_HOME}/dp/{variant_asin}"
        print(f"[cart] Query names pack size {variant['pack_size']!r} — using variant {variant_asin}.")

    if await _click_now_add_for_asin(page, match["asin"], variant_asin):
        record["added"] = True
        print(f"[cart] Added to the Amazon Now cart.")
    else:
        record["reason"] = "add-to-cart click not confirmed"
    return record


async def add_one(page, name: str) -> dict:
    """Search Fresh for `name`, fuzzy-match the best result, and add one unit
    to the cart. Returns a per-item result record."""
    print(f"\n[cart] Searching Fresh for {name!r}…")
    record = {
        "requested_name": name,
        "matched_title": None,
        "asin": None,
        "score": 0.0,
        "price": None,
        "product_url": None,
        "added": False,
        "reason": None,
        "source": "Amazon Fresh",
    }

    try:
        await page.goto(_search_url(name), wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
    except Exception as exc:
        record["reason"] = f"search navigation failed: {exc}"
        print(f"[cart] {record['reason']}")
        return record

    try:
        await page.wait_for_selector(CART_SELECTORS["result_card"], timeout=8_000)
    except PlaywrightTimeoutError:
        record["reason"] = "no search results"
        print(f"[cart] No Fresh results for {name!r}.")
        return record

    candidates = await _scan_results(page)
    if not candidates:
        record["reason"] = "no search results"
        print(f"[cart] No usable result cards for {name!r}.")
        return record

    print(f"[cart] Scanned {len(candidates)} result(s):")
    for c in candidates:
        print(f"         • [{c['asin']}] {c['title'][:90]}")

    idx, score = _best_match(name, [c["title"] for c in candidates])
    record["score"] = round(score, 3)
    if idx < 0 or score < MATCH_THRESHOLD:
        best = candidates[idx]["title"] if idx >= 0 else None
        record["matched_title"] = None
        record["reason"] = f"best score {score:.2f} below threshold {MATCH_THRESHOLD}"
        record["best_candidate"] = best
        print(f"[cart] No confident match for {name!r} (best={best!r} @ {score:.2f}).")
        return record

    match = candidates[idx]
    record["matched_title"] = match["title"]
    record["asin"] = match["asin"]
    record["price"] = match["price"]
    record["product_url"] = match.get("href") or f"{AMAZON_HOME}/dp/{match['asin']}"
    print(f"[cart] Match for {name!r}: {match['title'][:70]!r} @ {score:.2f}")

    cart_before = await _read_cart_count(page)
    clicked = await _click_add_for_asin(page, match["asin"])
    if not clicked:
        record["reason"] = "add-to-cart control not found"
        print(f"[cart] Could not find an Add control for {match['asin']}.")
        return record

    await page.wait_for_timeout(2_000)
    cart_after = await _read_cart_count(page)
    if cart_after > cart_before:
        record["added"] = True
        print(f"[cart] Added (cart {cart_before} → {cart_after}).")
    else:
        # The click landed but the badge didn't move — report it but don't claim success.
        record["reason"] = f"clicked Add but cart count did not increase ({cart_before} → {cart_after})"
        print(f"[cart] {record['reason']}")
    return record


async def add_products_to_cart(product_names: list[str], headless: bool) -> dict:
    """Log in, set the delivery location, and add one unit of each matched
    product to the cart — Amazon Now first, falling back to the classic Fresh
    flow per product when Now has no confident match. Returns a partitioned
    result dict; the Now cart is separate from the main Amazon cart."""
    # De-duplicate while preserving order, dropping blanks.
    names: list[str] = []
    seen: set[str] = set()
    for n in product_names:
        n = (n or "").strip()
        key = n.lower()
        if n and key not in seen:
            seen.add(key)
            names.append(n)

    added: list[dict] = []
    not_found: list[dict] = []
    cart_count = 0
    now_cart_count: int | None = None

    async with async_playwright() as pw:
        browser, context, page = await open_logged_in_page(pw, headless)
        try:
            for name in names:
                rec = await add_one_now(page, name)
                if not rec["added"]:
                    print(f"[cart] Amazon Now could not add {name!r} ({rec['reason']}); falling back to Fresh.")
                    rec = await add_one(page, name)
                if rec["added"]:
                    added.append({
                        "requested_name": rec["requested_name"],
                        "matched_title": rec["matched_title"],
                        "score": rec["score"],
                        "price": rec["price"],
                        "product_url": rec["product_url"],
                        "asin": rec["asin"],
                        "source": rec["source"],
                    })
                else:
                    not_found.append({
                        "requested_name": rec["requested_name"],
                        "best_candidate": rec.get("best_candidate") or rec.get("matched_title"),
                        "score": rec["score"],
                        "reason": rec["reason"],
                    })
            if any(a["source"] == "Amazon Now" for a in added):
                now_cart_count = await _read_now_cart_count(page)
            # The Fresh nav badge only exists on classic pages — leave any /tez/
            # page before reading it.
            await page.goto(AMAZON_HOME, wait_until="domcontentloaded")
            cart_count = await _read_cart_count(page)
        finally:
            await context.close()
            await browser.close()

    result = {
        "requested": len(names),
        "added": added,
        "not_found": not_found,
        "cart_count": cart_count,
        "now_cart_count": now_cart_count,
        "added_at": datetime.now(tz=timezone.utc).astimezone().isoformat(),
    }

    print(
        f"\n[cart] Done: {len(added)} added, {len(not_found)} not matched, "
        f"cart holds {cart_count} item(s), Now cart "
        f"{now_cart_count if now_cart_count is not None else 'n/a'}."
    )
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Add Amazon Fresh products to the cart by name."
    )
    ap.add_argument(
        "names",
        nargs="+",
        help="One or more product names to search for and add (one unit each).",
    )
    ap.add_argument(
        "--headed",
        type=lambda v: v.lower() not in ("false", "0", "no"),
        default=True,
        help="Run in headed mode (default: true)",
    )
    args = ap.parse_args()
    result = asyncio.run(
        add_products_to_cart(args.names, headless=not args.headed)
    )

    print(
        f"\n{'#':<4}  {'Requested':<40}  {'Matched':<40}  {'Score':<6}  {'Added'}"
    )
    print("-" * 100)
    rows = [(r, True) for r in result["added"]] + [(r, False) for r in result["not_found"]]
    for i, (r, ok) in enumerate(rows, 1):
        requested = r["requested_name"][:38]
        matched = (r.get("matched_title") or r.get("best_candidate") or "")[:38]
        print(f"{i:<4}  {requested:<40}  {matched:<40}  {r['score']:<6}  {'yes' if ok else 'no'}")


if __name__ == "__main__":
    main()
