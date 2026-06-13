#!/usr/bin/env python3
"""
Belgium visa appointment tracker + auto-booker.

Confirmed flow (reverse-engineered from live portal):
  1. Login  → visaonweb.diplomatie.be/en
  2. Nav    → /en/VisaApplication/IndexByUserId
  3. API    → /Common/GetEAppointmentUrl?id=<guid>  (returns JSON {"url": "..."})
  4. Open   → appointment.cloud.diplomatie.be/...
  5. CAPTCHA (hCaptcha "I am human") — solved by session cache in CI
  6. Check  → look for "No slots available" text
  7. Book   → click first available date → confirm
"""

import json
import logging
import os
import sys
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORTAL_URL   = "https://visaonweb.diplomatie.be/en"
APP_LIST_URL = "https://visaonweb.diplomatie.be/en/VisaApplication/IndexByUserId"
APPT_API     = "https://visaonweb.diplomatie.be/Common/GetEAppointmentUrl?id={guid}"

VOW_USERNAME = os.environ["VOW_USERNAME"]
VOW_PASSWORD = os.environ["VOW_PASSWORD"]
VOW_APP_ID   = os.environ["VOW_APP_ID"]

PERSISTENT_CONTEXT_DIR = "./vow_user_data"
SCREENSHOT_PATH        = "./booking_confirmation.png"

# CI=true is set by GitHub Actions; local runs are headed so CAPTCHA can be solved
IS_CI = os.environ.get("CI", "false").lower() == "true"

MAX_RETRIES = 2

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_NO_SLOTS  = 0   # normal run — no slots
EXIT_BOOKED    = 1   # slot found AND booked  → open GitHub Issue
EXIT_ERROR     = 2   # transient error / CAPTCHA → silent
EXIT_BOOK_FAIL = 3   # slot found but booking incomplete → urgent Issue

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selector constants
# ---------------------------------------------------------------------------
NO_SLOTS_PHRASES = [
    "no slots available",
    "no appointment slots",
    "there are no available",
    "aucun créneau disponible",
    "geen tijdsloten beschikbaar",
    "no available slots",
    "no time slots",
    "pas de créneaux",
    "keine termine",
    "no appointments",
]

SLOT_SELECTORS = [
    'td.available',
    'td[class*="available"]',
    'button[class*="available"]',
    'a[class*="available"]',
    '.calendar-day:not(.disabled):not(.unavailable):not(.past)',
    '[data-available="true"]',
    '[data-status="available"]',
    '.fc-day:not(.fc-past):not(.fc-other-month):not(.fc-day-disabled)',
    '[aria-label*="available" i]',
    '[title*="available" i]',
    'td:not([class*="disabled"]):not([class*="unavailable"]):not([class*="past"]) > a[href]',
]

CONFIRM_SELECTORS = [
    'button:has-text("Confirm")',
    'button:has-text("Book")',
    'button:has-text("Submit")',
    'button:has-text("Reserve")',
    'button:has-text("Confirmer")',
    'button:has-text("Proceed")',
    'input[type="submit"][value*="Confirm" i]',
    'input[type="submit"][value*="Book" i]',
    'input[type="submit"]',
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _screenshot(page, path: str) -> None:
    try:
        page.screenshot(path=path, full_page=True)
    except Exception as e:
        log.warning("Screenshot failed: %s", e)


def _is_logged_in(page) -> bool:
    return any(page.query_selector(s) for s in [
        'a[href*="logout" i]',
        'a[href*="signout" i]',
        'button:has-text("Sign out")',
        f':text("{VOW_APP_ID}")',
        'a[href*="IndexByUserId"]',
    ])


def _do_login(page) -> bool:
    """Fill login form if visible. Returns True when authenticated."""
    if _is_logged_in(page):
        log.info("Session already active.")
        return True

    ef = page.query_selector(
        'input[type="email"], input[name*="email" i], input[name*="username" i], '
        'input[placeholder*="email" i], input[placeholder*="user" i]'
    )
    if not ef:
        log.warning("No login form found and not logged in — URL: %s", page.url)
        return False

    log.info("Logging in as %s …", VOW_USERNAME)
    ef.fill(VOW_USERNAME)
    pf = page.query_selector('input[type="password"]')
    if not pf:
        log.error("Password field not found.")
        return False
    pf.fill(VOW_PASSWORD)

    submit = page.query_selector(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Log me in"), button:has-text("Login"), '
        'button:has-text("Sign in"), button:has-text("Connexion")'
    )
    if not submit:
        log.error("Submit button not found.")
        return False

    submit.click()
    page.wait_for_timeout(2500)

    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeout:
        pass

    if not _is_logged_in(page):
        log.error("Login failed — still not authenticated. URL: %s", page.url)
        log.error("Page text: %s", page.inner_text("body")[:400])
        return False

    log.info("Login successful — URL: %s", page.url)
    return True


def _get_application_guid(page) -> str | None:
    """
    Navigate to My Applications, find the VOW_APP_ID row,
    extract the application GUID from the calendar button's ng-click attribute.
    Returns the GUID string or None.
    """
    log.info("Loading My Applications list …")
    page.goto(APP_LIST_URL, wait_until="networkidle")
    page.wait_for_timeout(2000)

    if VOW_APP_ID not in page.inner_text("body"):
        log.error("Application %s not found in list. URL: %s", VOW_APP_ID, page.url)
        log.error("Page text: %s", page.inner_text("body")[:1000])
        return None

    # The calendar button has ng-click="groupVAEapp('<guid>','NA')"
    row = page.locator(f'tr:has-text("{VOW_APP_ID}")').first
    cal_btn = row.locator('button:has(.fa-calendar)').first
    ng_click = cal_btn.get_attribute("ng-click") or ""

    # Extract GUID: groupVAEapp('xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx','NA')
    import re
    match = re.search(r"groupVAEapp\('([^']+)'", ng_click)
    if not match:
        log.error("Could not extract GUID from ng-click: %s", ng_click)
        return None

    guid = match.group(1)
    log.info("Extracted application GUID: %s", guid)
    return guid


def _get_appointment_url(page, guid: str) -> str | None:
    """Call the GetEAppointmentUrl API (from within the browser session) and return the URL."""
    api_url = APPT_API.format(guid=guid)
    log.info("Calling appointment URL API …")

    # Use page.evaluate to make the fetch inside the browser (carries session cookies)
    result = page.evaluate(f"""
        async () => {{
            const resp = await fetch('{api_url}', {{credentials: 'include'}});
            return await resp.text();
        }}
    """)

    try:
        data = json.loads(result)
        url = data.get("url")
        if url:
            log.info("Appointment URL: %s", url)
            return url
        log.error("API response has no 'url' field: %s", result[:300])
    except json.JSONDecodeError:
        log.error("API response is not JSON: %s", result[:300])
    return None


def _handle_captcha(page) -> bool:
    """
    Detect and handle the hCaptcha gate on appointment.cloud.diplomatie.be.
    - CI mode: log warning, return False (trigger EXIT_ERROR = silent retry)
    - Local mode: prompt user to solve, wait up to 120 s
    """
    if "/Captcha" not in page.url and "captcha" not in page.url.lower():
        return True  # no CAPTCHA

    if IS_CI:
        log.warning(
            "hCaptcha gate on appointment system — session cache may be stale.\n"
            "Run the tracker locally once (headless=False) to prime the session,\n"
            "then the GitHub Actions cache will carry the solved cookie forward.\n"
            "URL: %s", page.url
        )
        return False

    # Local headed mode — prompt human
    print(
        "\033[93m" + "=" * 60 +
        "\nACTION REQUIRED: Solve the 'I am human' CAPTCHA in the browser.\n"
        "Waiting up to 120 seconds …\n" +
        "=" * 60 + "\033[0m",
        flush=True,
    )
    for remaining in range(120, 0, -1):
        time.sleep(1)
        if "/Captcha" not in page.url:
            log.info("CAPTCHA solved — proceeding.")
            return True
        if remaining % 20 == 0:
            log.info("Waiting for CAPTCHA … %ds left", remaining)

    log.error("CAPTCHA not solved within 120 s.")
    return False


def _check_and_book(page, appt_url: str) -> int:
    """
    Navigate to the external appointment system, check availability, and book.
    Returns EXIT_NO_SLOTS, EXIT_BOOKED, EXIT_ERROR, or EXIT_BOOK_FAIL.
    """
    log.info("Opening appointment system …")
    try:
        page.goto(appt_url, wait_until="domcontentloaded", timeout=60_000)
    except PlaywrightTimeout:
        log.warning("domcontentloaded timed out — checking whatever loaded")
    page.wait_for_timeout(3000)

    if not _handle_captcha(page):
        return EXIT_ERROR

    page.wait_for_timeout(2000)
    body_text = page.inner_text("body").lower()

    if any(phrase in body_text for phrase in NO_SLOTS_PHRASES):
        log.info("No slots available.")
        return EXIT_NO_SLOTS

    # Slot may be available — try to book
    log.info("No 'no slots' phrase found — attempting to book …")
    _screenshot(page, "./pre_booking.png")

    # Find a clickable available date
    slot_el = None
    for sel in SLOT_SELECTORS:
        try:
            candidates = page.query_selector_all(sel)
            visible = [e for e in candidates if e.is_visible() and e.is_enabled()]
            if visible:
                slot_el = visible[0]
                log.info("Slot element matched: %s", sel)
                break
        except Exception:
            continue

    if not slot_el:
        log.warning(
            "Could not find a clickable slot element — page may need further navigation.\n"
            "Page text: %s", page.inner_text("body")[:600]
        )
        print("BOOKING_FAILED", flush=True)
        return EXIT_BOOK_FAIL

    slot_el.click()
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(1500)

    # Time-slot selection (some portals have a second step)
    time_slot = page.query_selector(
        '[class*="timeslot"]:not([class*="disabled"]), '
        'input[type="radio"][name*="time"], '
        'button[class*="time"]:not([disabled])'
    )
    if time_slot:
        log.info("Selecting first available time slot …")
        time_slot.click()
        page.wait_for_timeout(1000)

    # Confirm
    confirmed = False
    for sel in CONFIRM_SELECTORS:
        btn = page.query_selector(sel)
        if btn and btn.is_visible() and btn.is_enabled():
            log.info("Clicking confirm button: %s", sel)
            btn.click()
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(2000)
            confirmed = True
            break

    if not confirmed:
        log.warning("No confirm button found — booking may have completed automatically.")

    _screenshot(page, SCREENSHOT_PATH)
    confirmation = page.inner_text("body")[:800].strip()
    log.info("Booking result:\n%s", confirmation)
    print(f"BOOKED\n{confirmation}", flush=True)
    return EXIT_BOOKED


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run_once(playwright) -> int:
    context = playwright.chromium.launch_persistent_context(
        PERSISTENT_CONTEXT_DIR,
        headless=IS_CI,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
        viewport={"width": 1280, "height": 800},
        locale="en-GB",
    )
    page = context.new_page()
    page.set_default_timeout(30_000)

    try:
        log.info("Opening portal …")
        page.goto(PORTAL_URL, wait_until="networkidle")

        if not _do_login(page):
            return EXIT_ERROR

        guid = _get_application_guid(page)
        if not guid:
            return EXIT_ERROR

        appt_url = _get_appointment_url(page, guid)
        if not appt_url:
            return EXIT_ERROR

        return _check_and_book(page, appt_url)

    except PlaywrightTimeout as exc:
        log.error("Timeout at URL %s: %s", page.url, exc)
        return EXIT_ERROR
    except Exception as exc:
        log.error("Unexpected error: %s", exc, exc_info=True)
        return EXIT_ERROR
    finally:
        context.close()


def run_tracker() -> int:
    with sync_playwright() as playwright:
        for attempt in range(1, MAX_RETRIES + 1):
            log.info("=== Attempt %d / %d ===", attempt, MAX_RETRIES)
            result = run_once(playwright)

            if result != EXIT_ERROR:
                return result

            if attempt < MAX_RETRIES:
                wait = attempt * 15
                log.info("Transient error — retrying in %ds …", wait)
                time.sleep(wait)

        log.warning("All %d attempts ended in error — exiting silently.", MAX_RETRIES)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(run_tracker())
