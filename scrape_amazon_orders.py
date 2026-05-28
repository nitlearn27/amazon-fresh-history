"""
Amazon Fresh (amazon.in) order history scraper.
Login: email/phone + password. OTP (2-step verification) is polled from
Salesforce Purchase_Info__c.my_amazon_otp__c in headless mode.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Selectors — update here when Amazon changes its markup.
# Amazon uses semantic IDs/classes more reliably than Flipkart, so most of
# these are stable. Still prefer attribute/text matches over hashed classes.
# ---------------------------------------------------------------------------
SELECTORS = {
    # Login: email/phone step
    "email_input": "input[name='email'], input#ap_email",
    "continue_button": "input#continue, input[type='submit'][aria-labelledby*='continue']",
    # Login: password step
    "password_input": "input[name='password'], input#ap_password",
    "signin_button": "input#signInSubmit, input[type='submit'][aria-labelledby*='signInSubmit']",
    # Login challenges
    "captcha_image": "img[src*='opfcaptcha'], img[alt*='captcha' i], img[src*='Captcha']",
    "captcha_input": "input#auth-captcha-guess, input[name='guess']",
    "otp_input": (
        "input#auth-mfa-otpcode, input[name='otpCode'], "
        "input#input-box-otp, input[name='code']"
    ),
    "otp_submit": (
        "input#auth-signin-button, input[aria-labelledby*='auth-signin-button'], "
        "input#cvf-submit-otp-button"
    ),
    # Logged-in marker on amazon.in
    "logged_in_indicator": "#nav-link-accountList, a[href*='/your-account']",
    # Delivery-location ("Deliver to") picker — Fresh availability is keyed on this
    "location_trigger": "#nav-global-location-popover-link, #glow-ingress-block",
    "pincode_input": "#GLUXZipUpdateInput, input[name='GLUXZip'], input[autocomplete='postal-code']",
    "pincode_apply": "#GLUXZipUpdate input[type='submit'], #GLUXZipUpdate-announce, span#GLUXZipUpdate input",
    "pincode_done": "button[name='glowDoneButton'], #GLUXConfirmClose, .a-popover-footer .a-button-input",
    "location_header": "#glow-ingress-line2",
    "address_list": "#GLUXAddressList, #glow-toaster-content",
    # Orders page
    "order_card": "div.order-card, div.js-order-card, [class*='order-card']",
    "order_filter_dropdown": "select#orderFilter, select[name='orderFilter']",
    "view_order_details_link": "a:has-text('Order details'), a[href*='order-details']",
    # Product page
    "product_title": "span#productTitle",
    "product_price": (
        "span.a-price.priceToPay span.a-offscreen, "
        "span#corePriceDisplay_desktop_feature_div span.a-offscreen, "
        "span.a-price span.a-offscreen"
    ),
    "product_image": "img#landingImage, img[data-old-hires]",
    "product_availability": "div#availability span, span#availability",
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AMAZON_HOME = "https://www.amazon.in"
AMAZON_SIGNIN = (
    "https://www.amazon.in/ap/signin"
    "?openid.return_to=https%3A%2F%2Fwww.amazon.in%2Fyour-orders%2Forders"
    "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.assoc_handle=inflex"
    "&openid.mode=checkid_setup"
    "&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
)
AMAZON_ORDERS = "https://www.amazon.in/your-orders/orders?_encoding=UTF8&orderFilter=months-6"

# Amazon Fresh availability/price is per-delivery-location. With no location set
# for the session, Fresh product pages render "currently unavailable" and no
# price. After login we set a deliverable location so every product page reports
# real data: first try to pick the saved address starting with
# DEFAULT_ADDRESS_PREFIX, and only fall back to entering DEFAULT_PINCODE if that
# address can't be found. Override via DELIVERY_ADDRESS_PREFIX / DELIVERY_PINCODE.
DEFAULT_ADDRESS_PREFIX = "82, Flat No 6"
DEFAULT_PINCODE = "560094"

ORDERS_REPORT_FILE = Path("orders_report.json")

# Marker substrings that identify an Amazon Fresh order on the order list/details.
FRESH_MARKERS = (
    "amazon fresh",
    "sold by: amazon fresh",
    "fulfilled by amazon fresh",
    "be sure to chill any perishables",
)

load_dotenv()


def default_orders_to_scrape() -> int:
    """Default number of orders to scrape. Reads ORDERS_TO_SCRAPE from the
    environment (.env) and falls back to 10 when unset or invalid."""
    raw = (os.getenv("ORDERS_TO_SCRAPE") or "").strip()
    if not raw:
        return 10
    try:
        n = int(raw)
        return n if n > 0 else 10
    except ValueError:
        print(f"[config] ORDERS_TO_SCRAPE={raw!r} is not a valid integer; using 10.")
        return 10


def delivery_pincode() -> str:
    """Delivery pincode used to set Amazon's location so Fresh items report
    correct availability/price. Reads DELIVERY_PINCODE from the environment
    and falls back to DEFAULT_PINCODE when unset or not a 6-digit code."""
    raw = (os.getenv("DELIVERY_PINCODE") or "").strip()
    if re.fullmatch(r"\d{6}", raw):
        return raw
    if raw:
        print(f"[config] DELIVERY_PINCODE={raw!r} is not a 6-digit code; using {DEFAULT_PINCODE}.")
    return DEFAULT_PINCODE


def delivery_address_prefix() -> str:
    """Saved Amazon address to prefer when setting the delivery location.
    Matched as a substring against each saved address in the "Deliver to"
    popover. Reads DELIVERY_ADDRESS_PREFIX, falling back to
    DEFAULT_ADDRESS_PREFIX."""
    raw = (os.getenv("DELIVERY_ADDRESS_PREFIX") or "").strip()
    return raw or DEFAULT_ADDRESS_PREFIX


# ---------------------------------------------------------------------------
# Utilities (copied from the Flipkart scraper — same parsing semantics)
# ---------------------------------------------------------------------------

def mask(value: str) -> str:
    return f"***{value[-4:]}" if value and len(value) > 4 else "****"


def parse_date(raw: str) -> str:
    if not raw:
        return "unknown"
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
    cleaned = re.sub(r"'(\d{2})\b", lambda m: f"20{m.group(1)}", cleaned)
    has_year = bool(re.search(r"\b(19|20)\d{2}\b", cleaned))
    try:
        parsed = dateutil_parser.parse(cleaned, fuzzy=True).date()
        if not has_year and parsed > datetime.now(tz=timezone.utc).date():
            parsed = parsed.replace(year=parsed.year - 1)
        return parsed.isoformat()
    except Exception:
        m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        if m:
            return m.group(0)
        m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", raw)
        if m:
            d, mo, y = m.groups()
            y = f"20{y}" if len(y) == 2 else y
            return f"{y}-{int(mo):02d}-{int(d):02d}"
        return "unknown"


def _unavailable_fields() -> dict:
    return {
        "current_price": None,
        "product_url": None,
        "image_url": None,
        "availability": "Unavailable",
    }


def _extract_date_from_text(text: str) -> str:
    """Find an 'Order placed <date>' or 'Ordered on <date>' inside text → ISO YYYY-MM-DD.
    Amazon order cards show things like '15 April 2026' or 'April 15, 2026'."""
    m = re.search(
        r"(?:Order\s+placed|Ordered\s+on|Placed\s+on)\s*[:\n]?\s*"
        r"([A-Za-z]{3,9}\s+\d{1,2}(?:,?\s+\d{2,4})?|\d{1,2}\s+[A-Za-z]{3,9}(?:,?\s+\d{2,4})?)",
        text, re.I,
    )
    if m:
        return parse_date(m.group(1))
    return "unknown"


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

async def _log_page_state(page, label: str) -> None:
    """Diagnostic breadcrumb: URL + title + first heading at a transition point."""
    try:
        url = page.url
    except Exception:
        url = "<unknown>"
    try:
        title = (await page.title()) or ""
    except Exception:
        title = ""
    try:
        heading = (await page.locator("h1, h2").first.inner_text(timeout=500)) or ""
    except Exception:
        heading = ""
    print(f"[trace] {label}: url={url!r}")
    if title:
        print(f"[trace] {label}: title={title!r}")
    if heading:
        print(f"[trace] {label}: heading={heading[:120]!r}")


async def is_logged_in(page) -> bool:
    """We're logged in iff the orders page renders without redirecting to /ap/signin."""
    url = page.url.lower()
    if "/ap/signin" in url or "/ap/login" in url:
        print(f"[auth] is_logged_in=False — URL is on signin page ({page.url})")
        return False
    try:
        await page.wait_for_selector(SELECTORS["logged_in_indicator"], timeout=4_000)
        print(f"[auth] is_logged_in=True — account nav element present at {page.url}")
        return True
    except PlaywrightTimeoutError:
        print(f"[auth] is_logged_in=False — no account nav element at {page.url}")
        return False


async def _handle_captcha_block(page) -> None:
    """If Amazon shows a captcha, screenshot + exit. Never auto-solve."""
    captcha = page.locator(SELECTORS["captcha_image"]).first
    if await captcha.count() > 0 and await captcha.is_visible():
        screenshot_path = Path("amazon_login_debug.png")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(
            "\n[error] Amazon presented a captcha.\n"
            "  • Re-run this scraper locally in HEADED mode (--headed=true) so you can\n"
            "    solve the captcha once by hand.\n"
            f"  • Screenshot: {screenshot_path.resolve()}\n"
            f"  • URL       : {page.url}\n"
        )
        sys.exit(1)


async def _poll_salesforce_for_otp(
    poll_interval_seconds: int = 5,
    max_total_seconds: int = 180,
) -> tuple[str, str] | None:
    """Poll Salesforce for an OTP. Returns (record_id, otp) or None on timeout."""
    try:
        from salesforce_sync import (
            OTP_FIELD,
            OTP_OBJECT,
            SalesforceError,
            config_present,
            fetch_amazon_otp,
        )
    except Exception as exc:
        print(f"[auth] Could not import Salesforce OTP bridge: {exc}")
        return None

    if not config_present():
        print(
            "[auth] Salesforce env vars are not set (SF_TOKEN_URL / SF_CLIENT_ID /"
            " SF_CLIENT_SECRET / SF_API_ENDPOINT) — cannot poll for OTP."
        )
        return None

    print(
        f"[auth] Amazon prompted for OTP. Polling Salesforce "
        f"{OTP_OBJECT}.{OTP_FIELD} every {poll_interval_seconds}s "
        f"(max {max_total_seconds}s)…"
    )
    print(
        f"[auth] ACTION REQUIRED: open Salesforce → {OTP_OBJECT} → set "
        f"{OTP_FIELD} to the OTP that Amazon just emailed/SMS'd, then save."
    )
    max_attempts = max(1, max_total_seconds // poll_interval_seconds)
    for attempt in range(1, max_attempts + 1):
        elapsed = (attempt - 1) * poll_interval_seconds
        outcome = "empty"
        try:
            result = fetch_amazon_otp()
        except SalesforceError as exc:
            print(f"[auth] Salesforce OTP poll {attempt}/{max_attempts} (t={elapsed}s) failed: {exc}")
            result = None
            outcome = "error"
        except Exception as exc:
            print(f"[auth] Salesforce OTP poll {attempt}/{max_attempts} (t={elapsed}s) unexpected: {exc}")
            result = None
            outcome = "error"
        if result is not None:
            print(f"[auth] OTP received from Salesforce on attempt {attempt}/{max_attempts} (t={elapsed}s).")
            return result
        print(
            f"[auth] Salesforce OTP poll {attempt}/{max_attempts} "
            f"(t={elapsed}s) → {outcome}; sleeping {poll_interval_seconds}s…"
        )
        if attempt < max_attempts:
            await asyncio.sleep(poll_interval_seconds)
    return None


async def _handle_otp_challenge(page) -> bool:
    """If Amazon asks for an OTP (2-step verification), poll Salesforce
    Purchase_Info__c.my_amazon_otp__c regardless of headed/headless mode.
    Returns True iff we entered an OTP."""
    otp_input = page.locator(SELECTORS["otp_input"]).first
    try:
        await otp_input.wait_for(state="visible", timeout=4_000)
        print("[auth] OTP input detected — Amazon is asking for 2-step verification.")
        await _log_page_state(page, "OTP screen")
    except PlaywrightTimeoutError:
        return False

    polled = await _poll_salesforce_for_otp()
    if polled is None:
        screenshot_path = Path("amazon_login_debug.png")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(
            "\n[error] No OTP appeared in Salesforce within the 3-minute window.\n"
            "  • Set Purchase_Info__c.my_amazon_otp__c to the OTP that Amazon\n"
            "    just emailed/SMS'd, then re-trigger the scrape.\n"
            f"  • Screenshot: {screenshot_path.resolve()}\n"
        )
        sys.exit(1)
    otp_record_id, otp = polled

    if not re.fullmatch(r"\d{4,8}", otp):
        print(f"[error] {otp!r} does not look like a numeric OTP. Aborting.")
        sys.exit(1)

    await otp_input.fill(otp)
    print(f"[auth] OTP filled into Amazon ({len(otp)} digits). Submitting…")
    submit = page.locator(SELECTORS["otp_submit"]).first
    try:
        await submit.wait_for(state="visible", timeout=5_000)
        await submit.click()
        print("[auth] Clicked OTP submit button.")
    except PlaywrightTimeoutError:
        await otp_input.press("Enter")
        print("[auth] OTP submit button not visible — pressed Enter on OTP field.")

    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        print("[auth] Post-OTP: networkidle did not fire within 15s (continuing).")
    await _log_page_state(page, "after OTP submit")

    try:
        from salesforce_sync import clear_amazon_otp
        clear_amazon_otp(otp_record_id)
        print("[auth] Cleared OTP from Salesforce.")
    except Exception as exc:
        # Login itself already succeeded — surface a warning but don't abort.
        print(f"[auth] Warning: failed to clear OTP in Salesforce: {exc}")

    return True


async def login(page, amazon_username: str, amazon_password: str, headless: bool) -> None:
    """
    Amazon password login:
      1. Navigate directly to the signin page.
      2. Fill email → continue, then password → sign in.
      3. Handle 2-step verification (OTP) if Amazon prompts for it.
      4. If Amazon shows a captcha, exit with a clear message (we never solve them).
    """
    print(f"[auth] Logging in to Amazon as …{mask(amazon_username)}")
    print(f"[auth] headless={headless}")

    print(f"[auth] Navigating to signin page…")
    await page.goto(AMAZON_SIGNIN, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        print("[auth] Signin: networkidle did not fire within 10s (continuing).")
    await _log_page_state(page, "on signin page")

    await _handle_captcha_block(page)

    # Step 1: email/phone
    email_input = page.locator(SELECTORS["email_input"]).first
    try:
        await email_input.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        screenshot_path = Path("amazon_login_debug.png")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(
            f"[error] Could not locate Amazon email input.\n"
            f"  URL        : {page.url}\n"
            f"  Screenshot : {screenshot_path.resolve()}"
        )
        sys.exit(1)

    await email_input.fill(amazon_username)
    print("[auth] Email/phone entered.")

    continue_btn = page.locator(SELECTORS["continue_button"]).first
    try:
        await continue_btn.wait_for(state="visible", timeout=5_000)
        await continue_btn.click()
        print("[auth] Clicked Continue.")
    except PlaywrightTimeoutError:
        await email_input.press("Enter")
        print("[auth] Continue button not visible — pressed Enter on email field.")
    await page.wait_for_timeout(2_000)
    await _log_page_state(page, "after Continue")

    await _handle_captcha_block(page)

    # Step 2: password
    password_input = page.locator(SELECTORS["password_input"]).first
    try:
        await password_input.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        screenshot_path = Path("amazon_login_debug.png")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(
            f"[error] Could not locate Amazon password input.\n"
            f"  URL        : {page.url}\n"
            f"  Screenshot : {screenshot_path.resolve()}\n"
            f"  • This usually means Amazon does not recognize the account, or it\n"
            f"    redirected to an extra verification step. Re-run headed to inspect."
        )
        sys.exit(1)

    await password_input.fill(amazon_password)
    print("[auth] Password entered.")

    signin_btn = page.locator(SELECTORS["signin_button"]).first
    try:
        await signin_btn.wait_for(state="visible", timeout=5_000)
        await signin_btn.click()
        print("[auth] Clicked Sign-In.")
    except PlaywrightTimeoutError:
        await password_input.press("Enter")
        print("[auth] Sign-In button not visible — pressed Enter on password field.")
    await page.wait_for_timeout(3_000)
    await _log_page_state(page, "after Sign-In")

    # Step 3: OTP challenge (only on new device / suspicious login)
    print("[auth] Checking for OTP / 2-step verification screen…")
    otp_entered = await _handle_otp_challenge(page)
    if not otp_entered:
        print("[auth] No OTP screen detected — continuing.")

    # Step 4: post-login captcha (rare but possible)
    await _handle_captcha_block(page)

    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        print("[auth] Post-signin: networkidle did not fire within 15s (continuing).")
    await _log_page_state(page, "post-login final state")

    if not await is_logged_in(page):
        screenshot_path = Path("amazon_login_debug.png")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(
            f"\n[error] Login did not complete.\n"
            f"  URL        : {page.url}\n"
            f"  Screenshot : {screenshot_path.resolve()}\n"
            f"  • Wrong password, or Amazon presented an unhandled verification step.\n"
            f"  • Re-run headed to inspect."
        )
        sys.exit(1)

    print("[auth] Amazon login successful.")


# ---------------------------------------------------------------------------
# Delivery location
# ---------------------------------------------------------------------------

async def _read_location_header(page) -> str:
    try:
        return ((await page.locator(SELECTORS["location_header"]).first.inner_text(timeout=3_000)) or "").strip()
    except Exception:
        return ""


async def _location_widget_open(page) -> bool:
    """True if the "Deliver to" popover (saved-address list or pincode input)
    is currently visible."""
    for sel in (SELECTORS["pincode_input"], SELECTORS["address_list"]):
        try:
            if await page.locator(sel).first.is_visible():
                return True
        except Exception:
            continue
    return False


async def _open_location_popover(page) -> bool:
    """Open Amazon's "Deliver to" popover (idempotent — no-op if already open)."""
    if await _location_widget_open(page):
        return True
    try:
        trigger = page.locator(SELECTORS["location_trigger"]).first
        await trigger.wait_for(state="visible", timeout=8_000)
        await trigger.click()
        await page.wait_for_timeout(1_000)
    except Exception as exc:
        print(f"[location] Could not open location popover ({exc}).")
        return False
    return await _location_widget_open(page)


async def _select_saved_address(page, address_prefix: str) -> bool:
    """Within the open location popover, pick the saved address whose visible
    text contains `address_prefix`. Returns True only if a matching address
    was found and clicked."""
    print(f"[location] Looking for saved address containing {address_prefix!r}…")
    candidates = (
        page.locator(SELECTORS["address_list"]).get_by_text(address_prefix, exact=False),
        page.get_by_text(address_prefix, exact=False),
    )
    target = None
    for loc in candidates:
        try:
            if await loc.count() > 0:
                target = loc.first
                break
        except Exception:
            continue
    if target is None:
        print(f"[location] No saved address matching {address_prefix!r}.")
        return False

    try:
        await target.scroll_into_view_if_needed(timeout=3_000)
        await target.click(timeout=5_000)
    except Exception as exc:
        print(f"[location] Found address but click failed ({exc}).")
        return False

    # Selecting a saved address sometimes needs an explicit confirm.
    for done in (
        page.locator(SELECTORS["pincode_done"]).first,
        page.get_by_role("button", name=re.compile(r"\b(done|apply|continue|use this address)\b", re.I)).first,
    ):
        try:
            if await done.count() > 0 and await done.is_visible():
                await done.click(timeout=3_000)
                await page.wait_for_timeout(800)
                break
        except Exception:
            continue

    try:
        await page.wait_for_load_state("networkidle", timeout=6_000)
    except PlaywrightTimeoutError:
        pass
    print(f"[location] Selected saved address; header now {repr(await _read_location_header(page))}.")
    return True


async def _enter_pincode(page, pincode: str) -> bool:
    """Fallback path: type `pincode` into the "Deliver to" popover and apply."""
    print(f"[location] Setting delivery pincode to {pincode}…")
    if not await _open_location_popover(page):
        return False
    try:
        zip_input = page.locator(SELECTORS["pincode_input"]).first
        await zip_input.wait_for(state="visible", timeout=8_000)
        await zip_input.fill(pincode)
    except PlaywrightTimeoutError:
        print("[location] Pincode input did not appear; skipping.")
        return False

    apply_btn = page.locator(SELECTORS["pincode_apply"]).first
    try:
        if await apply_btn.count() > 0:
            await apply_btn.click(timeout=4_000)
        else:
            await zip_input.press("Enter")
    except Exception:
        try:
            await zip_input.press("Enter")
        except Exception:
            pass

    try:
        await page.wait_for_load_state("networkidle", timeout=6_000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(1_000)

    # A confirmation step ("Done" / "Continue") sometimes follows the apply.
    for done in (
        page.locator(SELECTORS["pincode_done"]).first,
        page.get_by_role("button", name=re.compile(r"\b(done|continue)\b", re.I)).first,
    ):
        try:
            if await done.count() > 0 and await done.is_visible():
                await done.click(timeout=3_000)
                await page.wait_for_timeout(800)
                break
        except Exception:
            continue

    try:
        await page.wait_for_load_state("networkidle", timeout=6_000)
    except PlaywrightTimeoutError:
        pass

    header = await _read_location_header(page)
    if pincode in header:
        print(f"[location] Delivery location confirmed: {repr(header)}.")
    else:
        print(f"[location] Pincode submitted (header shows {repr(header)}).")
    return True


async def _set_delivery_location(page, address_prefix: str, pincode: str) -> bool:
    """Set Amazon's "Deliver to" location so Fresh items report correct
    availability/price (the Fresh catalog is keyed on the delivery location).

    Prefers the saved address containing `address_prefix`; only falls back to
    entering `pincode` if that address can't be found. The choice persists in a
    session cookie, so every product page visited afterwards reports real data.

    Best-effort: logs a warning and returns False if the widget can't be
    driven, so the scrape still proceeds."""
    if not await _open_location_popover(page):
        print("[location] Location popover unavailable; continuing without setting it.")
        return False
    if await _select_saved_address(page, address_prefix):
        return True
    print(f"[location] Falling back to pincode {pincode}.")
    return await _enter_pincode(page, pincode)


async def _goto_with_retry(page, url: str, attempts: int = 3) -> None:
    """page.goto that retries on net::ERR_ABORTED.

    Applying a delivery location makes Amazon reload the current page; a goto
    fired immediately afterwards can be interrupted by that reload and raise
    net::ERR_ABORTED. Retrying after a short settle resolves it."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded")
            return
        except Exception as exc:
            last_exc = exc
            if "ERR_ABORTED" in str(exc) or "interrupted" in str(exc).lower():
                print(f"[nav] goto aborted (attempt {attempt}/{attempts}); retrying…")
                await page.wait_for_timeout(1_500)
                continue
            raise
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Order scraping
# ---------------------------------------------------------------------------

async def _select_fresh_filter(page) -> None:
    """Try to set Amazon's order-history filter to 'Amazon Fresh' if the
    dropdown exposes that option. No-op when the option is not present —
    in that case we fall back to client-side text filtering."""
    dropdown = page.locator(SELECTORS["order_filter_dropdown"]).first
    if await dropdown.count() == 0:
        return
    try:
        options = await dropdown.locator("option").all_inner_texts()
    except Exception:
        return
    fresh_option = next(
        (o for o in options if "fresh" in o.lower()), None
    )
    if not fresh_option:
        return
    try:
        await dropdown.select_option(label=fresh_option)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(1_000)
        print(f"[orders] Applied dropdown filter: {fresh_option!r}.")
    except Exception as exc:
        print(f"[orders] Could not select Fresh filter: {exc}")


async def _is_fresh_order(card) -> bool:
    """Inspect a single order card's visible text for Amazon Fresh markers."""
    try:
        text = (await card.inner_text() or "").lower()
    except Exception:
        return False
    return any(marker in text for marker in FRESH_MARKERS)


async def _next_page_link(page):
    """Return the locator for the 'Next' pagination link, or None if absent/disabled."""
    next_link = page.locator(
        "ul.a-pagination li.a-last a, a:has-text('Next')"
    ).first
    try:
        if await next_link.count() == 0:
            return None
        if not await next_link.is_visible():
            return None
        # Amazon greys out the Next item with class 'a-disabled' when there are no more pages.
        parent_li = page.locator("ul.a-pagination li.a-last").first
        if await parent_li.count() > 0:
            cls = (await parent_li.get_attribute("class")) or ""
            if "a-disabled" in cls:
                return None
        return next_link
    except Exception:
        return None


async def _collect_fresh_order_detail_urls(
    page, num_orders: int, max_pages: int = 1
) -> list[dict]:
    """Walk the orders pages, keep only Amazon Fresh orders, return up to
    num_orders {url, fallback_date} entries.

    Returns the order-details URL plus an order-card-text-derived fallback
    date — used later if the detail page's date is hard to parse."""
    collected: list[dict] = []
    seen_urls: set[str] = set()

    for page_index in range(max_pages):
        try:
            await page.wait_for_selector(SELECTORS["order_card"], timeout=10_000)
        except PlaywrightTimeoutError:
            print(f"[orders] No order cards on page {page_index + 1}; stopping.")
            break

        cards = await page.query_selector_all(SELECTORS["order_card"])
        print(f"[orders] Page {page_index + 1}: {len(cards)} order card(s) on screen.")

        for card in cards:
            if not await _is_fresh_order(card):
                continue
            card_text = (await card.inner_text()) or ""
            fallback_date = _extract_date_from_text(card_text)

            details_link = await card.query_selector("a[href*='order-details']")
            if details_link is None:
                continue
            href = await details_link.get_attribute("href") or ""
            if not href:
                continue
            if not href.startswith("http"):
                href = AMAZON_HOME.rstrip("/") + "/" + href.lstrip("/")
            if href in seen_urls:
                continue
            seen_urls.add(href)
            collected.append({"url": href, "fallback_date": fallback_date})
            if len(collected) >= num_orders:
                return collected

        # Paginate
        next_link = await _next_page_link(page)
        if next_link is None:
            print("[orders] No more pages.")
            break
        try:
            await next_link.click()
            await page.wait_for_load_state("networkidle", timeout=15_000)
            await page.wait_for_timeout(1_000)
        except Exception as exc:
            print(f"[orders] Pagination click failed: {exc}")
            break

    return collected


async def _extract_order_date_from_detail_page(page, fallback_date: str) -> str:
    """Pull the order date from an order-details page. Amazon shows it in a
    label like 'Order placed - 12 April 2026' near the top."""
    try:
        body_text = (await page.locator("body").inner_text() or "")[:4000]
    except Exception:
        body_text = ""
    date = _extract_date_from_text(body_text)
    return date if date != "unknown" else fallback_date


async def _expand_view_all_items(page) -> bool:
    """Amazon Fresh order-details pages collapse the line-items behind a
    'View all items' toggle (sits above the Delivery Address block, next
    to the 'N items in this order' label). Click it so the full ordered
    list is rendered in a dialog/expanded panel before we extract product
    anchors — without this, the DOM also contains recommendation/upsell
    widgets that aren't in the order.

    Returns True if a click succeeded."""
    pattern = re.compile(r"view\s+all\s+items?", re.IGNORECASE)
    candidates = [
        page.get_by_role("link", name=pattern),
        page.get_by_role("button", name=pattern),
        page.get_by_text(pattern, exact=False),
    ]
    for loc in candidates:
        try:
            if await loc.count() == 0:
                continue
            await loc.first.click(timeout=5_000)
            print("[order] Clicked 'View all items' — navigating to EWC items view.")
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(1_000)
            return True
        except Exception:
            continue
    print("[order] 'View all items' toggle not found — extracting from default page.")
    return False


async def _extract_products_from_detail_page(page) -> list[dict]:
    """Pull product-title + product-page URL + purchased-price triples from the
    order's item list.

    Amazon Fresh (UFPO) order-details pages render the actual ordered items
    inside `#ufpo-od-item-list-section`, with one `div[id$='-item-grid-row']`
    per item — the row's id prefix is the product ASIN. We iterate rows
    rather than anchors so we get one product per item even when the row
    contains multiple links (image + title + 'buy it again'). This section
    is hidden by `class='hide'` on the order-details page until 'View all
    items' navigates to the itemmod sub-page that unhides it; either way
    `textContent` reads through the visibility.

    The `price` is the price paid in THIS order (used for last_purchased_price);
    it is NOT the product's current price — that comes from the product page.

    Falls back to a generic anchor scan for classic non-Fresh orders."""
    raw = await page.evaluate(r"""
        () => {
          const priceFromEl = (el) => {
            if (!el) return null;
            const off = el.querySelector("span.a-price > span.a-offscreen, .a-price .a-offscreen");
            const text = (off ? off.textContent : el.textContent) || '';
            const m = text.match(/₹\s*([\d,]+(?:\.\d+)?)/);
            return m ? parseFloat(m[1].replace(/,/g, '')) : null;
          };

          // ----- Amazon Fresh (UFPO) order item list -----
          const section = document.querySelector('#ufpo-od-item-list-section');
          if (section) {
            const rows = section.querySelectorAll('div[id$="-item-grid-row"]');
            const out = [];
            const seenIds = new Set();
            for (const row of rows) {
              if (row.id && seenIds.has(row.id)) continue;
              if (row.id) seenIds.add(row.id);
              const titleLink = row.querySelector(
                "a.a-link-normal[href*='/dp/'], a.a-link-normal[href*='/gp/product/'], a.a-link-normal"
              );
              if (!titleLink) continue;
              const title = (titleLink.textContent || '').replace(/\s+/g, ' ').trim();
              if (!title || title.length < 4) continue;
              out.push({ title, href: titleLink.href, price: priceFromEl(row) });
            }
            if (out.length) return out;
          }

          // ----- Fallback: classic order-details page -----
          const fallback = document.querySelector(
            "[data-component='shipments'], #od-shipments, [id^='shipment'], " +
            "[class*='shipment'], #orderDetails, .order-details-content"
          ) || document.body;
          const anchors = Array.from(fallback.querySelectorAll(
            "a.a-link-normal[href*='/gp/product/'], a.a-link-normal[href*='/dp/']"
          ));
          const seen = new Set();
          const out = [];
          for (const a of anchors) {
            const title = (a.innerText || '').replace(/\s+/g, ' ').trim();
            if (!title || title.length < 4) continue;
            const lower = title.toLowerCase();
            if (
              lower === 'buy it again' || lower === 'view your item' ||
              lower === 'view product' || lower === 'write a product review' ||
              lower === 'get product support' || lower === 'leave seller feedback' ||
              lower.startsWith('return ') || lower.startsWith('archive ') ||
              lower.includes('track package')
            ) continue;
            if (seen.has(title)) continue;
            seen.add(title);
            const container = a.closest('.a-fixed-left-grid, .a-row, li') || a.parentElement;
            out.push({ title, href: a.href, price: priceFromEl(container) });
          }
          return out;
        }
    """)
    return raw or []


def _clean_amazon_product_title(raw: str) -> str:
    """Amazon titles are usually clean already, but they can include a trailing
    quantity badge or an ellipsis. Collapse whitespace and strip trailing junk."""
    title = re.sub(r"\s+", " ", raw or "").strip()
    title = title.rstrip("…. ")
    return title


# ---------------------------------------------------------------------------
# Product-page enrichment
# ---------------------------------------------------------------------------

async def _extract_current_price(page) -> float | None:
    """Pick the most prominent ₹ price from the product page."""
    try:
        loc = page.locator(SELECTORS["product_price"]).first
        if await loc.count() > 0:
            text = await loc.inner_text(timeout=2_000)
            m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", text)
            if m:
                return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    # Fallback: scan visible body text for the first ₹ price.
    try:
        body = await page.locator("body").inner_text()
        m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", body)
        if m:
            return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


async def _extract_main_image(page) -> str | None:
    """Return the main product image from the Amazon product page."""
    try:
        return await page.evaluate("""
            () => {
              const main = document.querySelector('img#landingImage');
              if (main) return main.getAttribute('data-old-hires') || main.src || null;
              const any = document.querySelector('img[data-old-hires]');
              if (any) return any.getAttribute('data-old-hires') || any.src || null;
              return null;
            }
        """)
    except Exception:
        return None


def _availability_from_price(price: float | None) -> str:
    """Availability is determined solely by whether the product page shows a
    price: a priced product page is in stock, a price-less one is not. (We
    deliberately avoid scanning page text — out-of-stock phrases bleed in from
    unrelated carousels / 'compare with similar items' / other-seller blocks.)"""
    return "Available" if price is not None else "Unavailable"


async def extract_product_details(page) -> dict:
    """Capture price / image / url / availability from the currently-open product page."""
    try:
        await page.wait_for_load_state("networkidle", timeout=6_000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(500)

    price = await _extract_current_price(page)
    return {
        "current_price": price,
        "product_url": page.url,
        "image_url": await _extract_main_image(page),
        "availability": _availability_from_price(price),
    }


async def visit_product_page(page, product_url: str) -> dict:
    """Navigate to a product page and extract per-product fields.
    Returns _unavailable_fields() on any error so callers can still upsert."""
    if not product_url:
        return _unavailable_fields()
    try:
        await page.goto(product_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(500)
        return await extract_product_details(page)
    except Exception as exc:
        print(f"  [product] navigation failed: {exc}")
        return _unavailable_fields()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(num_orders: int, headless: bool) -> None:
    load_dotenv()
    amazon_username = os.getenv("AMAZON_USERNAME", "")
    amazon_password = os.getenv("AMAZON_PASSWORD", "")

    if not amazon_username or not amazon_password:
        print("[error] AMAZON_USERNAME and AMAZON_PASSWORD must both be set in .env")
        sys.exit(1)

    async with async_playwright() as pw:
        # --no-sandbox / --disable-dev-shm-usage are required inside Docker (Render).
        # The extra flags reduce Chromium's memory footprint in headless mode.
        browser_args = []
        if headless:
            browser_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--no-first-run",
                "--disable-default-apps",
                "--mute-audio",
                "--hide-scrollbars",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--metrics-recording-only",
                "--safebrowsing-disable-auto-update",
            ]

        browser = await pw.chromium.launch(
            headless=headless,
            slow_mo=900 if not headless else 0,
            args=browser_args,
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        # Abort image/media/font requests — not needed for data extraction and
        # they are the biggest contributors to Chromium's memory usage.
        async def _block_heavy(route):
            if route.request.resource_type in {"image", "media", "font"}:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _block_heavy)

        # ---- Login ----
        await login(page, amazon_username, amazon_password, headless)

        # ---- Set delivery location so Fresh items report correct availability ----
        print("[nav] Navigating home to set delivery location…")
        await _goto_with_retry(page, AMAZON_HOME)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        await _set_delivery_location(page, delivery_address_prefix(), delivery_pincode())
        # Applying a location reloads the page; let it settle before navigating
        # away, otherwise the next goto can be interrupted (net::ERR_ABORTED).
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(1_000)

        # ---- Navigate to orders ----
        print("[nav] Navigating to orders page…")
        await _goto_with_retry(page, AMAZON_ORDERS)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

        # Try to set the dropdown to "Amazon Fresh" — usually not present, but
        # if it is, it saves us pagination.
        await _select_fresh_filter(page)

        # ---- Collect Amazon Fresh order-detail URLs ----
        fresh_orders = await _collect_fresh_order_detail_urls(page, num_orders)
        actual_count = len(fresh_orders)
        print(f"[orders] Collected {actual_count} Amazon Fresh order(s).")

        if actual_count == 0:
            screenshot_path = Path("amazon_orders_debug.png")
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(
                f"[warn] No Amazon Fresh orders found in the visible history.\n"
                f"  URL        : {page.url}\n"
                f"  Screenshot : {screenshot_path.resolve()}\n"
                f"  Writing empty report so the Salesforce sync no-ops cleanly."
            )

        # ---- For each Fresh order: visit detail page, extract products ----
        all_products: list[dict] = []
        for idx, order in enumerate(fresh_orders, 1):
            print(f"\n[order {idx}/{actual_count}] {order['url'][:90]}")
            try:
                await page.goto(order["url"], wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightTimeoutError:
                    pass
                await page.wait_for_timeout(800)

                order_date = await _extract_order_date_from_detail_page(
                    page, order["fallback_date"]
                )
                await _expand_view_all_items(page)
                items = await _extract_products_from_detail_page(page)

                if not items:
                    print(f"[order {idx}/{actual_count}] No items found on detail page.")
                    continue

                seen_titles: set[str] = set()
                for it in items:
                    title = _clean_amazon_product_title(it.get("title", ""))
                    if not title or title.lower() in seen_titles:
                        continue
                    seen_titles.add(title.lower())
                    all_products.append({
                        "item_id": f"amazon::{idx}::{title.lower()}",
                        "title": title,
                        "date": order_date,
                        "category": "Grocery",
                        "product_url_from_order": it.get("href"),
                        "purchased_price": it.get("price"),
                    })
                print(f"[order {idx}/{actual_count}] {len(seen_titles)} product(s)")
            except Exception as exc:
                print(f"[order {idx}/{actual_count}] Error: {exc}")

        # ---- Aggregate by title (Counter — same idea as Flipkart scraper) ----
        title_counts: Counter[str] = Counter(p["title"] for p in all_products)

        unique_by_title: dict[str, dict] = {}
        for p in all_products:
            t = p["title"]
            cur = unique_by_title.get(t)
            if cur is None:
                entry = dict(p)
                entry["last_purchased_price"] = p.get("purchased_price")
                unique_by_title[t] = entry
                continue
            # Newer date wins — and the last purchased price tracks that order.
            if p.get("date") and p["date"] != "unknown" and (
                not cur.get("date") or cur["date"] == "unknown" or p["date"] > cur["date"]
            ):
                cur["date"] = p["date"]
                cur["last_purchased_price"] = p.get("purchased_price")
            # Backfill a missing price even when the date isn't newer.
            elif cur.get("last_purchased_price") is None and p.get("purchased_price") is not None:
                cur["last_purchased_price"] = p.get("purchased_price")
            if not cur.get("product_url_from_order") and p.get("product_url_from_order"):
                cur["product_url_from_order"] = p["product_url_from_order"]

        # ---- Per-product page visit + immediate Salesforce sync ----
        # Check Salesforce availability once before the loop to avoid
        # printing "skipped" N times.
        try:
            from salesforce_sync import (
                sync_products as _sf_sync,
                config_present as _sf_config_present,
            )
            _sf_available = _sf_config_present()
            if not _sf_available:
                missing = [
                    k for k in ("SF_TOKEN_URL", "SF_CLIENT_ID", "SF_CLIENT_SECRET", "SF_API_ENDPOINT")
                    if not (os.getenv(k) or "").strip()
                ]
                print(f"[salesforce] Sync disabled — missing env vars: {', '.join(missing)}")
        except Exception as exc:
            print(f"[salesforce] Import failed — sync disabled: {exc}")
            _sf_available = False

        scraped_at = datetime.now(tz=timezone.utc).astimezone().isoformat()
        titles = list(unique_by_title.keys())
        report_products = []
        print(f"\n[products] Visiting {len(titles)} unique product page(s)…")
        for i, title in enumerate(titles, 1):
            entry = unique_by_title.pop(title)  # release as we go
            print(f"  [{i}/{len(titles)}] {title[:70]}")
            details = await visit_product_page(page, entry.get("product_url_from_order"))

            date = entry.get("date")
            product = {
                "title": title,
                "last_ordered_date": None if not date or date == "unknown" else date,
                "number_of_times_purchased": title_counts[title],
                "current_price": details["current_price"],
                "last_purchased_price": entry.get("last_purchased_price"),
                "product_url": details["product_url"],
                "image_url": details["image_url"],
                "category": entry.get("category", "Grocery"),
                "availability": details["availability"] or "Unavailable",
                "source": "Amazon",
                "scraped_at": scraped_at,
            }

            if _sf_available:
                try:
                    _sf_sync([product])
                except Exception as exc:
                    print(f"  [salesforce] {title[:50]}: {exc}")

            report_products.append(product)

        await context.close()
        await browser.close()

    # ---- Write report ----
    report = {
        "scraped_at": scraped_at,
        "orders_scanned": actual_count,
        "products": report_products,
    }
    ORDERS_REPORT_FILE.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n[done] Report written to {ORDERS_REPORT_FILE}")

    print(
        f"\n{'#':<4}  {'Product Title':<50}  {'Date':<12}  {'Cnt':<4}  "
        f"{'Price':<8}  {'LastPaid':<9}  {'Cat':<12}  {'Avail'}"
    )
    print("-" * 120)
    for i, p in enumerate(report_products, 1):
        title = p["title"][:48] + ".." if len(p["title"]) > 50 else p["title"]
        price = "" if p["current_price"] is None else f"₹{p['current_price']}"
        last_paid = "" if p.get("last_purchased_price") is None else f"₹{p['last_purchased_price']}"
        print(
            f"{i:<4}  {title:<50}  {str(p['last_ordered_date']):<12}  "
            f"{p['number_of_times_purchased']:<4}  {price:<8}  {last_paid:<9}  "
            f"{str(p['category']):<12}  {p['availability']}"
        )


def main() -> None:
    default_orders = default_orders_to_scrape()
    ap = argparse.ArgumentParser(description="Scrape Amazon Fresh order history.")
    ap.add_argument(
        "--orders",
        type=int,
        default=default_orders,
        help=f"Number of orders to scrape (default: {default_orders}, from ORDERS_TO_SCRAPE in .env)",
    )
    ap.add_argument(
        "--headed",
        type=lambda v: v.lower() not in ("false", "0", "no"),
        default=True,
        help="Run in headed mode (default: true)",
    )
    args = ap.parse_args()
    asyncio.run(run(num_orders=args.orders, headless=not args.headed))


if __name__ == "__main__":
    main()
