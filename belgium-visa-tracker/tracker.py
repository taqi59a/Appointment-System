#!/usr/bin/env python3
"""Belgium visa appointment tracker + auto-booker — visaonweb.diplomatie.be"""

import os
import sys
import time
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TARGET_URL  = "https://visaonweb.diplomatie.be/en"
VOW_USERNAME = os.environ["VOW_USERNAME"]
VOW_PASSWORD = os.environ["VOW_PASSWORD"]
VOW_APP_ID   = os.environ["VOW_APP_ID"]

PERSISTENT_CONTEXT_DIR = "./vow_user_data"
SCREENSHOT_PATH        = "./booking_confirmation.png"
IS_CI                  = os.environ.get("CI", "false").lower() == "true"
MAX_RETRIES            = 2  # retry entire flow on transient failures

# ---------------------------------------------------------------------------
# Exit codes  (workflow reads these to decide what action to take)
# ---------------------------------------------------------------------------
EXIT_NO_SLOTS  = 0   # normal — no slots, run silently
EXIT_BOOKED    = 1   # slot found AND booked  → open success issue
EXIT_ERROR     = 2   # transient error / CAPTCHA → silent pass, no issue
EXIT_BOOK_FAIL = 3   # slot found but booking incomplete → open urgent issue

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
    "aucun créneau disponible",
    "geen tijdsloten beschikbaar",
    "no available slots",
    "no time slots",
    "pas de créneaux",
    "keine termine",
]

CAPTCHA_SELECTORS = [
    'iframe[src*="captcha"]',
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    '[class*="captcha"]',
    '[id*="captcha"]',
    '.g-recaptcha',
    '.h-captcha',
]

SLOT_SELECTORS = [
    'td.available',
    'td[class*="available"]',
    '.slot-available',
    'button[class*="available"]',
    'a[class*="available"]',
    '.calendar-day:not(.disabled):not(.unavailable):not(.past)',
    '[data-available="true"]',
    '[data-status="available"]',
    '.fc-day:not(.fc-past):not(.fc-other-month):not(.fc-day-disabled)',
    'td:not([class*="disabled"]):not([class*="unavailable"]):not([class*="past"]) > a[href]',
    '[aria-label*="available" i]',
    '[title*="available" i]',
]

CONFIRM_SELECTORS = [
    'button:has-text("Confirm")',
    'button:has-text("Confirmer")',
    'button:has-text("Book appointment")',
    'button:has-text("Book")',
    'button:has-text("Submit")',
    'button:has-text("Reserve")',
    'button:has-text("Proceed")',
    'button:has-text("Réserver")',
    'input[type="submit"][value*="Confirm" i]',
    'input[type="submit"][value*="Book" i]',
    'input[type="submit"]',
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_screenshot(page, path: str) -> None:
    try:
        page.screenshot(path=path, full_page=True)
        log.info("Screenshot saved → %s", path)
    except Exception as exc:
        log.warning("Screenshot failed: %s", exc)


def _captcha_present(page) -> bool:
    return any(page.query_selector(sel) for sel in CAPTCHA_SELECTORS)


def handle_captcha(page) -> bool:
    """Return True if we can proceed (no CAPTCHA, or resolved locally)."""
    if not _captcha_present(page):
        return True

    if IS_CI:
        log.warning(
            "CAPTCHA detected in CI — session may be stale. "
            "URL: %s | DOM preview: %s",
            page.url,
            page.inner_html("body")[:800],
        )
        return False

    # Headed / local mode
    print(
        "\033[93m"
        + "=" * 60
        + "\nACTION REQUIRED: Solve the CAPTCHA in the browser window.\n"
        + "Waiting up to 90 seconds...\n"
        + "=" * 60
        + "\033[0m",
        flush=True,
    )
    for remaining in range(90, 0, -1):
        time.sleep(1)
        if not _captcha_present(page):
            log.info("CAPTCHA resolved.")
            return True
        if remaining % 15 == 0:
            log.info("Still waiting for CAPTCHA… %ds left", remaining)

    log.error("CAPTCHA not resolved within 90 s.")
    return False


def is_logged_in(page) -> bool:
    """Heuristic: if a logout link / dashboard element is present, we're in."""
    logged_in_signals = [
        'a[href*="logout"]',
        'a[href*="signout"]',
        'button:has-text("Logout")',
        'button:has-text("Sign out")',
        '[class*="dashboard"]',
        '[class*="my-applications"]',
        f':text("{VOW_APP_ID}")',
    ]
    return any(page.query_selector(sel) for sel in logged_in_signals)


def do_login(page) -> bool:
    """Fill login form if visible. Returns True when authenticated."""
    # Check if already logged in first
    if is_logged_in(page):
        log.info("Session active — no login required.")
        return True

    email_field = page.query_selector(
        'input[type="email"], input[name*="email" i], input[name*="username" i], '
        'input[placeholder*="email" i], input[placeholder*="user" i]'
    )
    if not email_field:
        log.warning("No login form and no active session — unexpected page: %s", page.url)
        log.warning("Page text: %s", page.inner_text("body")[:500])
        return False

    log.info("Logging in as %s", VOW_USERNAME)
    email_field.fill(VOW_USERNAME)

    pwd_field = page.query_selector('input[type="password"]')
    if not pwd_field:
        log.error("Password field missing.")
        return False
    pwd_field.fill(VOW_PASSWORD)

    submit = page.query_selector(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Log in"), button:has-text("Login"), '
        'button:has-text("Sign in"), button:has-text("Connexion"), '
        'button:has-text("Se connecter"), button:has-text("Log me in")'
    )
    if not submit:
        # Last resort: any visible button on the form
        submit = page.query_selector('form button, form input[type="button"]')
    if not submit:
        log.error("No submit button found on login page.")
        return False

    submit.click()
    page.wait_for_timeout(2000)

    if not handle_captcha(page):
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeout:
        pass  # page may still be usable

    log.info("Post-login URL: %s", page.url)

    if not is_logged_in(page):
        log.warning(
            "Still not logged in after submit — wrong credentials or unexpected redirect.\n"
            "Page text: %s",
            page.inner_text("body")[:600],
        )
        return False

    log.info("Login successful.")
    return True


def navigate_to_calendar(page) -> bool:
    """
    From the authenticated dashboard, find application VOW_APP_ID
    and navigate into its appointment calendar view.
    Returns True on success.
    """
    log.info("Looking for application %s…", VOW_APP_ID)

    app_locator = page.locator(f':text("{VOW_APP_ID}")').first
    try:
        app_locator.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeout:
        log.error(
            "Application %s not visible — URL: %s\nPage text: %s",
            VOW_APP_ID, page.url, page.inner_text("body")[:2000],
        )
        return False

    app_locator.click()
    page.wait_for_load_state("networkidle")
    log.info("Opened application page — URL: %s", page.url)

    # Find the appointment / schedule link within the application detail
    appt_link = page.query_selector(
        'a:has-text("appointment"), a:has-text("Appointment"), '
        'a:has-text("schedule"), a:has-text("Schedule"), '
        'a:has-text("rendez-vous"), a:has-text("Rendez-vous"), '
        'a:has-text("Book"), a:has-text("Prendre rendez"), '
        'button:has-text("Book"), button:has-text("Schedule")'
    )
    if appt_link:
        log.info("Clicking appointment link…")
        appt_link.click()
        page.wait_for_load_state("networkidle")
        log.info("Calendar URL: %s", page.url)

    # Give JavaScript calendar time to render
    page.wait_for_timeout(2500)
    return True


def attempt_booking(page) -> str | None:
    """
    Click the first visible available slot, confirm, and return
    confirmation text. Returns None if booking couldn't be completed.
    """
    log.info("Slot detected — attempting auto-booking…")
    _safe_screenshot(page, "./pre_booking.png")

    slot_el = None
    for sel in SLOT_SELECTORS:
        try:
            candidates = page.query_selector_all(sel)
            visible = [e for e in candidates if e.is_visible() and e.is_enabled()]
            if visible:
                slot_el = visible[0]
                log.info("Slot element matched selector: %s", sel)
                break
        except Exception:
            continue

    if not slot_el:
        log.error("No clickable slot element found — cannot auto-book.")
        return None

    slot_el.click()
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(1500)

    if not handle_captcha(page):
        return None

    # Some portals show a time-slot selection after clicking the date
    # Try to pick the first available time slot as well
    time_slot = page.query_selector(
        '[class*="timeslot"]:not([class*="disabled"]), '
        '[class*="time-slot"]:not([class*="disabled"]), '
        'input[type="radio"][name*="time"], '
        'button[class*="time"]:not([disabled])'
    )
    if time_slot:
        log.info("Time slot selection found — clicking first option.")
        time_slot.click()
        page.wait_for_timeout(1000)

    # Hit the confirm / submit button
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
        log.warning("No confirm button found — booking may still have gone through (single-step flow).")

    _safe_screenshot(page, SCREENSHOT_PATH)

    confirmation_text = page.inner_text("body")[:800].strip()
    log.info("Booking result:\n%s", confirmation_text)
    return confirmation_text


# ---------------------------------------------------------------------------
# Main
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
        log.info("Loading %s", TARGET_URL)
        page.goto(TARGET_URL, wait_until="networkidle")

        if not do_login(page):
            return EXIT_ERROR

        if not navigate_to_calendar(page):
            return EXIT_ERROR

        body_text = page.inner_text("body").lower()
        no_slots = any(phrase in body_text for phrase in NO_SLOTS_PHRASES)

        if no_slots:
            log.info("No slots available — done.")
            return EXIT_NO_SLOTS

        # Slot is open — book immediately
        log.info("*** SLOT AVAILABLE for %s — booking now! ***", VOW_APP_ID)
        confirmation = attempt_booking(page)

        if confirmation:
            print(f"BOOKED\n{confirmation}", flush=True)
            return EXIT_BOOKED
        else:
            print("BOOKING_FAILED", flush=True)
            return EXIT_BOOK_FAIL

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
            log.info("--- Attempt %d / %d ---", attempt, MAX_RETRIES)
            result = run_once(playwright)

            if result != EXIT_ERROR:
                return result  # definitive result — no retry needed

            if attempt < MAX_RETRIES:
                wait = attempt * 10
                log.info("Transient error — retrying in %ds…", wait)
                time.sleep(wait)

        log.warning("All %d attempts ended in error — exiting silently.", MAX_RETRIES)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(run_tracker())
