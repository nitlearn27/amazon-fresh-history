import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from scrape_amazon_orders import open_logged_in_page, AMAZON_NOW_BRAND_ID
from agent_resolver import resolve_selector

# Selectors specific to search results
SEARCH_SELECTORS = {
    "result_card": "div[data-component-type='s-search-result'][data-asin]",
}

MAX_SEARCH_RESULTS = 3


async def search_amazon_now(query: str, headless: bool = True) -> list[dict]:
    """
    Search Amazon Now (/tez/) first; fall back to the classic Fresh search
    only when Now returns no products.
    """
    async with async_playwright() as pw:
        # Reuses the exact same login and delivery location setup
        browser, context, page = await open_logged_in_page(pw, headless)
        try:
            products = await _search_now_tez(page, query)
            if products:
                return products
            print(f"[search] Amazon Now returned nothing for {query!r}; falling back to Fresh search.")
            return await _search_fresh_classic(page, query)
        finally:
            await context.close()
            await browser.close()


async def _search_now_tez(page, query: str) -> list[dict]:
    """Search the Amazon Now quick-commerce SPA (/tez/browse/search).

    The SPA's product grid is obfuscated styled-components, but it hydrates from
    a single JSON XHR (/tez/browse/searchByKeyword) we can capture in-page.
    Calling that endpoint directly returns 204 — it needs the SPA's CSRF
    headers — so navigation + response capture is the reliable path."""
    search_url = (
        f"https://www.amazon.in/tez/browse/search"
        f"?qcbrand={AMAZON_NOW_BRAND_ID}&searchKeyword={quote(query)}"
    )
    print(f"[search] Navigating to Amazon Now: {search_url}")
    try:
        async with page.expect_response(
            lambda r: "tez/browse/searchByKeyword" in r.url, timeout=20_000
        ) as resp_info:
            await page.goto(search_url, wait_until="domcontentloaded")
        resp = await resp_info.value
        payload = await resp.json()
    except PlaywrightTimeoutError:
        print(f"[search] Amazon Now search XHR did not fire for {query!r}.")
        return []
    except Exception as exc:
        print(f"[search] Amazon Now search failed for {query!r}: {exc}")
        return []

    raw = (((payload or {}).get("data") or {}).get("searchResponse") or {}).get("products") or []
    scraped_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    products = []
    for p in raw[:MAX_SEARCH_RESULTS]:
        asin = p.get("asin")
        price = (p.get("buyingPrice") or {}).get("amount")
        availability_type = (p.get("availability") or {}).get("type")
        products.append({
            "availability": "Available" if availability_type == "IN_STOCK" else "Unavailable",
            "current_price": price,
            "image_url": (p.get("heroImage") or {}).get("highResImageUrl"),
            "product_name": p.get("title"),
            "product_url": f"https://www.amazon.in/dp/{asin}" if asin else None,
            "rating": (p.get("customerReviewSummary") or {}).get("rating"),
            "scraped_at": scraped_at,
            "source": "Amazon Now",
            "weight": p.get("packSize"),
        })
    print(f"[search] Amazon Now returned {len(products)} product(s) for {query!r}.")
    return products


async def _search_fresh_classic(page, query: str) -> list[dict]:
    """Fallback: scrape the classic Fresh search results (/s?k=...&i=nowstore)."""
    search_url = f"https://www.amazon.in/s?k={quote(query)}&i=nowstore"
    print(f"[search] Navigating to: {search_url}")
    await page.goto(search_url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass

    # Wait for search results to load
    card_selector = None
    try:
        await page.wait_for_selector(SEARCH_SELECTORS["result_card"], timeout=8000)
    except PlaywrightTimeoutError:
        # A genuinely empty search is not a failure — don't invoke the agent.
        if await page.get_by_text("No results for", exact=False).count() > 0:
            print(f"[search] No search results found for: {query}")
            return []
        # Self-heal: the card selector may have gone stale.
        card_selector = await resolve_selector(
            page, "search.result_card",
            "exactly one element per product search-result card, each card "
            "containing a product title and a ₹ price",
            expectation="₹", min_count=2,
        )
        if card_selector is None:
            print(f"[search] No search results found for: {query}")
            return []

    # Extract products using a robust JS evaluation
    scraped_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    products = await page.evaluate(
        r"""
        ({scrapedAt, cardSelector}) => {
            const priceFromCard = (card) => {
                const off = card.querySelector("span.a-price > span.a-offscreen, .a-price .a-offscreen");
                const text = (off ? off.textContent : '') || '';
                const m = text.match(/₹\s*([\d,]+(?:\.\d+)?)/);
                return m ? parseFloat(m[1].replace(/,/g, '')) : null;
            };

            const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

            const titleFromCard = (card) => {
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

            const imageFromCard = (card) => {
                const img = card.querySelector("img.s-image");
                return img ? img.src : null;
            };

            const ratingFromCard = (card) => {
                const starsEl = card.querySelector("[aria-label*='stars'], [aria-label*='out of 5']");
                if (starsEl) {
                    const label = starsEl.getAttribute('aria-label');
                    const m = label.match(/([0-9.]+)\s*(?:out of 5)?/);
                    if (m) return parseFloat(m[1]);
                }
                const altEl = card.querySelector(".a-icon-alt");
                if (altEl) {
                    const text = altEl.textContent;
                    const m = text.match(/([0-9.]+)\s*(?:out of 5)?/);
                    if (m) return parseFloat(m[1]);
                }
                return null;
            };

            const weightFromCard = (card, title) => {
                // Try to find standalone weight badge/labels
                const weightRegex = /^(\d+(?:\.\d+)?\s*(?:gm|g|kg|ml|l|ltr|litre|grams|kilograms|milliliter|liters))\b$/i;
                const els = card.querySelectorAll("span, div");
                for (const el of els) {
                    const text = el.textContent.trim();
                    if (weightRegex.test(text)) {
                        return text;
                    }
                }
                // Fallback: Parse from title
                if (title) {
                    const m = title.match(/(\d+(?:\.\d+)?\s*(?:gm|g|kg|ml|l|ltr|litre|grams|kilograms|milliliter|liters))\b/i);
                    if (m) return m[1];
                }
                return null;
            };

            const cards = Array.from(document.querySelectorAll(cardSelector)).slice(0, 3);

            return cards.map(card => {
                const productName = titleFromCard(card);
                const currentPrice = priceFromCard(card);
                const imageUrl = imageFromCard(card);
                const productUrl = hrefFromCard(card);
                const rating = ratingFromCard(card);
                const weight = weightFromCard(card, productName);
                
                // Availability logic: if we have a price, it is Available.
                // Or if it says out of stock/unavailable, it's Unavailable.
                const text = card.textContent.toLowerCase();
                let availability = "Available";
                if (text.includes("currently unavailable") || text.includes("out of stock") || currentPrice === null) {
                    availability = "Unavailable";
                }

                return {
                    availability,
                    current_price: currentPrice,
                    image_url: imageUrl,
                    product_name: productName,
                    product_url: productUrl,
                    rating,
                    scraped_at: scrapedAt,
                    source: "Amazon Fresh",
                    weight
                };
            });
        }
        """,
        {"scrapedAt": scraped_at, "cardSelector": card_selector or SEARCH_SELECTORS["result_card"]}
    )
    return products or []
