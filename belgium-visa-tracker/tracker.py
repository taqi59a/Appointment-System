#!/usr/bin/env python3
"""Belgium visa appointment tracker + auto-booker — visaonweb.diplomatie.be"""

import os
import sys
import time
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TARGET_URL = "https://visaonweb.diplomatie.be/en"
VOW_USERNAME = os.environ["VOW_USERNAME"]
VOW_PASSWORD = os.environ["VOW_PASSWORD"]
VOW_APP_ID = os.environ["VOW_APP_ID"]
ICLOUD_APP_PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD", "")

SMTP_SERVER = "smtp.mail.me.com"
SMTP_PORT = 587

PERSISTENT_CONTEXT_DIR = "./vow_user_data"
SCREENSHOT_PATH = "./booking_confirmation.png"

IS_CI = os.environ.get("CI", "false").lower() == "true"

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
# Constants
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

# Selectors for clickable available slots in calendar views
SLOT_SELECTORS = [
    'td.available',
    'td[class*="available"]',
    '.slot-available',
    '[class*="slot"]:not([class*="unavailable"]):not([class*="disabled"])',
    'button[class*="available"]',
    'a[class*="available"]',
    '.calendar-day:not(.disabled):not(.unavailable):not(.past)',
    '[data-available="true"]',
    '[aria-disabled="false"][class*="day"]',
    # Common date picker patterns
    'td:not(.disabled):not(.unavailable) > a',
    '.fc-day:not(.fc-past):not(.fc-other-month)',
]

CONFIRM_SELECTORS = [
    'button:has-text("Confirm")',
    'button:has-text("Book")',
    'button:has-text("Submit")',
    'button:has-text("Reserve")',
    'button:has-text("Confirmer")',
    'button:has-text("Réserver")',
    'input[type="submit"]',
    '[class*="confirm"]',
    '[class*="submit"]',
]


# ---------------------------------------------------------------------------
# Email (booking confirmation)
# ---------------------------------------------------------------------------

def send_booking_confirmation(booking_detail: str) -> None:
    if not ICLOUD_APP_PASSWORD:
        log.warning("ICLOUD_APP_PASSWORD not set — skipping confirmation email")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "✅ VISA APPOINTMENT BOOKED — KINSHASA"
    msg["From"] = VOW_USERNAME
    msg["To"] = VOW_USERNAME

    html_body = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f5f5f5;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td align="center" style="padding:40px 20px;">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#fff;border-radius:8px;overflow:hidden;
                      box-shadow:0 2px 12px rgba(0,0,0,.15);">
          <tr>
            <td style="background:#2e7d32;padding:24px 32px;">
              <h1 style="margin:0;color:#fff;font-size:22px;">
                ✅ APPOINTMENT BOOKED SUCCESSFULLY
              </h1>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <p style="font-size:16px;color:#333;margin-top:0;">
                Your visa appointment has been <strong>automatically booked</strong> for<br>
                application <code style="background:#f5f5f5;padding:2px 6px;
                border-radius:4px;">{VOW_APP_ID}</code>.
              </p>
              <p style="font-size:14px;color:#555;">Booking details:</p>
              <pre style="background:#f9f9f9;border:1px solid #ddd;border-radius:4px;
                          padding:12px;font-size:13px;overflow-x:auto;
                          white-space:pre-wrap;">{booking_detail}</pre>
              <p style="text-align:center;margin:32px 0 0;">
                <a href="{TARGET_URL}"
                   style="background:#2e7d32;color:#fff;text-decoration:none;
                          padding:14px 32px;border-radius:6px;font-size:16px;
                          font-weight:bold;display:inline-block;">
                  ➜ View your appointment
                </a>
              </p>
              <p style="font-size:12px;color:#999;margin-top:24px;text-align:center;">
                A screenshot of the confirmation page has been saved in the workflow artifacts.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(VOW_USERNAME, ICLOUD_APP_PASSWORD)
            server.sendmail(VOW_USERNAME, VOW_USERNAME, msg.as_string())
        log.info("Booking confirmation email sent to %s", VOW_USERNAME)
    except Exception as exc:
        log.error("Confirmation email failed: %s", exc)


# ---------------------------------------------------------------------------
# CAPTCHA handling
# ---------------------------------------------------------------------------

def _captcha_present(page) -> bool:
    return any(page.query_selector(sel) for sel in CAPTCHA_SELECTORS)


def handle_captcha(page) -> bool:
    if not _captcha_present(page):
        return True

    if IS_CI:
        log.error("CAPTCHA encountered in headless CI — cannot resolve automatically.")
        try:
            log.error("Page URL: %s", page.url)
            log.error("DOM snapshot (first 2000 chars):\n%s", page.inner_html("body")[:2000])
        except Exception:
            pass
        return False

    print(
        "\033[93m" + "=" * 60 + "\n"
        "ACTION REQUIRED: Please solve CAPTCHA on the visible browser frame\n"
        "Waiting up to 90 seconds for resolution...\n" +
        "=" * 60 + "\033[0m",
        flush=True,
    )
    for remaining in range(90, 0, -1):
        time.sleep(1)
        if not _captcha_present(page):
            log.info("CAPTCHA resolved — continuing.")
            return True
        if remaining % 15 == 0:
            log.info("Waiting for CAPTCHA... %ds remaining", remaining)

    log.error("CAPTCHA not resolved within 90 seconds.")
    return False


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def do_login(page) -> bool:
    email_field = page.query_selector(
        'input[type="email"], input[name*="email"], input[name*="username"], '
        'input[placeholder*="email" i], input[placeholder*="user" i]'
    )
    if not email_field:
        log.info("No login form visible — using cached session (URL: %s)", page.url)
        return True

    log.info("Login form detected — signing in as %s", VOW_USERNAME)
    email_field.fill(VOW_USERNAME)

    pwd_field = page.query_selector('input[type="password"]')
    if not pwd_field:
        log.error("Password field not found")
        return False
    pwd_field.fill(VOW_PASSWORD)

    submit = page.query_selector(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Log"), button:has-text("Sign in"), '
        'button:has-text("Connexion"), button:has-text("Login"), '
        'button:has-text("Se connecter")'
    )
    if not submit:
        log.error("Submit button not found on login page")
        return False

    submit.click()
    page.wait_for_timeout(2000)

    if not handle_captcha(page):
        return False

    page.wait_for_load_state("networkidle", timeout=20_000)
    log.info("Authentication complete — URL: %s", page.url)
    return True


# ---------------------------------------------------------------------------
# Auto-booking
# ---------------------------------------------------------------------------

def attempt_booking(page) -> str | None:
    """
    Tries to click the first available slot and confirm the booking.
    Returns a confirmation string on success, None on failure.
    """
    log.info("Slot detected — attempting to auto-book...")

    # Find the first available/clickable slot
    slot_el = None
    for sel in SLOT_SELECTORS:
        try:
            candidates = page.query_selector_all(sel)
            # Filter out hidden or disabled elements
            visible = [el for el in candidates if el.is_visible() and el.is_enabled()]
            if visible:
                slot_el = visible[0]
                log.info("Clicking available slot via selector: %s", sel)
                break
        except Exception:
            continue

    if not slot_el:
        # Last resort: look for any non-greyed date link/button in the calendar area
        log.warning("No slot found via standard selectors — trying fallback text scan")
        # Try clicking a date that looks selectable
        slot_el = page.query_selector(
            'table.calendar a:not([class*="disabled"]):not([class*="past"]):not([class*="grey"])'
        )

    if not slot_el:
        log.error("Could not identify a clickable slot element")
        return None

    slot_el.click()
    page.wait_for_load_state("networkidle", timeout=15_000)
    page.wait_for_timeout(1500)

    # Handle any follow-up CAPTCHA before confirming
    if not handle_captcha(page):
        return None

    # Look for a confirmation/submit button
    confirmed = False
    for sel in CONFIRM_SELECTORS:
        confirm_btn = page.query_selector(sel)
        if confirm_btn and confirm_btn.is_visible() and confirm_btn.is_enabled():
            log.info("Clicking confirm/submit button: %s", sel)
            confirm_btn.click()
            page.wait_for_load_state("networkidle", timeout=20_000)
            page.wait_for_timeout(2000)
            confirmed = True
            break

    if not confirmed:
        log.warning("No confirm button found — slot may have been selected without explicit confirmation step")

    # Capture confirmation screenshot
    try:
        page.screenshot(path=SCREENSHOT_PATH, full_page=True)
        log.info("Confirmation screenshot saved to %s", SCREENSHOT_PATH)
    except Exception as exc:
        log.warning("Could not save screenshot: %s", exc)

    # Extract confirmation text
    confirmation_text = page.inner_text("body")[:800].strip()
    log.info("Booking result page text:\n%s", confirmation_text)
    return confirmation_text


# ---------------------------------------------------------------------------
# Main tracker
# ---------------------------------------------------------------------------

def run_tracker() -> int:
    """
    Exit codes:
      0 — no slots available
      1 — slot found AND booked successfully
      2 — error / CAPTCHA block
      3 — slot found but booking failed
    """
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            PERSISTENT_CONTEXT_DIR,
            headless=IS_CI,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        page.set_default_timeout(30_000)

        try:
            log.info("Opening %s", TARGET_URL)
            page.goto(TARGET_URL, wait_until="networkidle")

            # Login
            if not do_login(page):
                return 2

            # Locate application row
            log.info("Searching for application %s...", VOW_APP_ID)
            app_locator = page.locator(f':text("{VOW_APP_ID}")').first
            try:
                app_locator.wait_for(state="visible", timeout=15_000)
            except PlaywrightTimeout:
                log.error("Application %s not found — URL: %s", VOW_APP_ID, page.url)
                log.error("Page text (first 3000 chars):\n%s", page.inner_text("body")[:3000])
                return 2

            app_locator.click()
            page.wait_for_load_state("networkidle")
            log.info("Opened application — URL: %s", page.url)

            # Navigate into appointment/scheduling view
            appt_link = page.query_selector(
                'a:has-text("appointment"), a:has-text("Appointment"), '
                'a:has-text("schedule"), a:has-text("Schedule"), '
                'a:has-text("rendez-vous"), a:has-text("Rendez-vous"), '
                'button:has-text("Book"), button:has-text("Prendre")'
            )
            if appt_link:
                log.info("Navigating into appointment calendar...")
                appt_link.click()
                page.wait_for_load_state("networkidle")

            # Wait briefly for any JS calendar to render
            page.wait_for_timeout(2000)

            # Check availability
            body_text = page.inner_text("body").lower()
            no_slots = any(phrase in body_text for phrase in NO_SLOTS_PHRASES)

            if no_slots:
                log.info("No slots available for %s.", VOW_APP_ID)
                return 0

            # --- Slot available: auto-book it ---
            log.info("Slot available for %s — proceeding with auto-booking!", VOW_APP_ID)

            confirmation = attempt_booking(page)

            if confirmation:
                print(f"BOOKED\n{confirmation}", flush=True)
                send_booking_confirmation(confirmation)
                return 1  # success — triggers GH Actions issue + job failure for notification
            else:
                log.error("Slot was found but booking could not be completed.")
                print("BOOKING_FAILED", flush=True)
                return 3

        except PlaywrightTimeout as exc:
            log.error("Timeout: %s", exc)
            try:
                log.error("URL at timeout: %s", page.url)
            except Exception:
                pass
            return 2

        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
            return 2

        finally:
            context.close()


if __name__ == "__main__":
    sys.exit(run_tracker())
