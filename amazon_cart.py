"""
Add Amazon Fresh (amazon.in) products to the cart by name.

Given a list of real product names, this module searches the Amazon Fresh
storefront for each, fuzzy-matches the best result above a confidence
threshold, and adds one unit of it to the cart. Unmatched names are skipped
and reported. The flow NEVER proceeds to checkout/payment — matched items are
left in the cart for manual review.

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

from scrape_amazon_orders import AMAZON_HOME, open_logged_in_page

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
    Score is a difflib ratio in [0, 1]. Returns (-1, 0.0) for an empty list."""
    nq = _normalize(query)
    best_idx, best_score = -1, 0.0
    for i, title in enumerate(candidate_titles):
        score = difflib.SequenceMatcher(None, nq, _normalize(title)).ratio()
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx, best_score


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
    """Log in, set the Fresh delivery location, and add one unit of each
    matched product to the cart. Returns a partitioned result dict."""
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

    async with async_playwright() as pw:
        browser, context, page = await open_logged_in_page(pw, headless)
        try:
            for name in names:
                rec = await add_one(page, name)
                if rec["added"]:
                    added.append({
                        "requested_name": rec["requested_name"],
                        "matched_title": rec["matched_title"],
                        "score": rec["score"],
                        "price": rec["price"],
                        "product_url": rec["product_url"],
                        "asin": rec["asin"],
                    })
                else:
                    not_found.append({
                        "requested_name": rec["requested_name"],
                        "best_candidate": rec.get("best_candidate") or rec.get("matched_title"),
                        "score": rec["score"],
                        "reason": rec["reason"],
                    })
            cart_count = await _read_cart_count(page)
        finally:
            await context.close()
            await browser.close()

    result = {
        "requested": len(names),
        "added": added,
        "not_found": not_found,
        "cart_count": cart_count,
        "added_at": datetime.now(tz=timezone.utc).astimezone().isoformat(),
    }

    print(
        f"\n[cart] Done: {len(added)} added, {len(not_found)} not matched, "
        f"cart now holds {cart_count} item(s)."
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
