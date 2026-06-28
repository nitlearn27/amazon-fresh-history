import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from scrape_amazon_orders import open_logged_in_page

# Selectors specific to search results
SEARCH_SELECTORS = {
    "result_card": "div[data-component-type='s-search-result'][data-asin]",
}

async def search_amazon_now(query: str, headless: bool = True) -> list[dict]:
    """
    Search Amazon Fresh/Now for a query and return scraped products.
    """
    async with async_playwright() as pw:
        # Reuses the exact same login and delivery location setup
        browser, context, page = await open_logged_in_page(pw, headless)
        try:
            search_url = f"https://www.amazon.in/s?k={quote(query)}&i=nowstore"
            print(f"[search] Navigating to: {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass

            # Wait for search results to load
            try:
                await page.wait_for_selector(SEARCH_SELECTORS["result_card"], timeout=8000)
            except PlaywrightTimeoutError:
                print(f"[search] No search results found for: {query}")
                return []

            # Extract products using a robust JS evaluation
            scraped_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            products = await page.evaluate(
                r"""
                ({scrapedAt}) => {
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

                    const cards = Array.from(
                        document.querySelectorAll("div[data-component-type='s-search-result'][data-asin]")
                    ).filter(c => (c.getAttribute('data-asin') || '').trim()).slice(0, 3);

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
                            source: "Amazon now",
                            weight
                        };
                    });
                }
                """,
                {"scrapedAt": scraped_at}
            )
            return products or []
        finally:
            await context.close()
            await browser.close()
