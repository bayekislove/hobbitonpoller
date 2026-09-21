#!/usr/bin/env python3
"""
Hobbiton Evening Banquet Tour — ticket availability poller for Render.com.
"""

import argparse
import os
import smtplib
import sys
import time
import threading
import traceback
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from http.server import HTTPServer, BaseHTTPRequestHandler

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------- CONFIG ---------------------------------

TOUR_URL = "https://www.hobbitontours.com/experiences/evening-banquet-tour/"
SCRIPT_VERSION = "v10-verbose-logging-and-direct-date-input"
HEADLESS = True

SELECTORS = {
    "cookie_consent_candidates": [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "#CybotCookiebotDialogBodyButtonAccept",
        "button:has-text('Allow all')",
        "button:has-text('Allow All')",
        "button:has-text('Accept')",
    ],
    "maintenance_modal_dismiss": "text=I understand, continue",
    "date_field": "input.c-booking-form__datepicker.js-datepicker",
    "calendar_next": "button.pika-next",
    "calendar_prev": "button.pika-prev",
    "group_size_plus": "button:has-text('+')",
    "group_size_minus": "button:has-text('-')",
    "group_size_value": "[class*='group-size'] input, [class*='qty'] input",
    "check_availability_btn": "text=CHECK AVAILABILITY",
    "sold_out_markers": ["Fully Booked", "Not Available", "Sold Out", "No availability"],
    "time_slot_results": "[class*='timeslot'], [class*='time-slot'], button:has-text(':')",
}

# ----------------------------- EMAIL CONFIG -----------------------------
SMTP_HOST = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER = os.environ.get("SMTP_USER") or None
SMTP_PASS = os.environ.get("SMTP_PASS") or None

EMAIL_SUBJECT = "TICKETS FOR EVENING TOUR ARE AVAILABLE"


# ==========================================
# 1. HTTP SERVER FOR RENDER.COM (KEEP-ALIVE)
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Hobbiton Poller is active and running!")

    def log_message(self, format, *args):
        return


def start_http_server():
    """Starts a lightweight HTTP server in the background on the port supplied by Render."""
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [HTTP] Server listening on port {port}")
    server.serve_forever()


# ==========================================
# 2. HELPERS AND LOGIC
# ==========================================
def log_debug(msg: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{stamp}] [DEBUG] {msg}", flush=True)


def send_email(to_address: str, available_dates: list, min_tickets: int):
    if not SMTP_USER or not SMTP_PASS:
        print(
            "[email] SMTP_USER / SMTP_PASS not set — skipping email send.",
            file=sys.stderr,
            flush=True
        )
        return False

    dates_list = "\n".join(f"   - {d}" for d in available_dates)
    body = (
        f"At least {min_tickets} tickets are available for the "
        f"Hobbiton Evening Banquet Tour on the following date(s):\n\n"
        f"{dates_list}\n\n"
        f"Book here: {TOUR_URL}"
    )
    msg = MIMEText(body)
    msg["Subject"] = EMAIL_SUBJECT
    msg["From"] = SMTP_USER
    msg["To"] = to_address

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [to_address], msg.as_string())
        print(f"[email] Sent notification to {to_address}", flush=True)
        return True
    except Exception as e:
        print(f"[email] Failed to send: {e}", file=sys.stderr, flush=True)
        return False


def date_range(start_str: str, end_str: str) -> list:
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    if end < start:
        raise ValueError(f"--end-date ({end_str}) is before --start-date ({start_str})")
    days = (end - start).days
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]


def _first_visible(page, selector: str, description: str = "", timeout_ms: int = 15000, poll_ms: int = 250):
    deadline = time.monotonic() + timeout_ms / 1000
    last_count = 0
    while True:
        loc = page.locator(selector)
        last_count = loc.count()
        for i in range(last_count):
            item = loc.nth(i)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(poll_ms)
    raise RuntimeError(
        f"No visible match for selector '{selector}'"
        + (f" ({description})" if description else "")
        + f" after waiting {timeout_ms}ms. Last count seen: {last_count}."
    )


def _click_visible(page, selector: str, description: str = "", timeout: int = 15000):
    log_debug(f"Locating visible element: '{selector}' ({description})")
    elem = _first_visible(page, selector, description, timeout_ms=timeout)
    
    try:
        elem.scroll_into_view_if_needed(timeout=2000)
    except Exception as e:
        log_debug(f"scroll_into_view_if_needed failed/skipped: {e}")

    try:
        log_debug(f"Attempting standard click on '{description}'")
        elem.click(timeout=5000, force=True)
        log_debug(f"Standard click succeeded on '{description}'")
    except Exception as e:
        log_debug(f"Standard click failed ({e}). Attempting JS click fallback.")
        page.evaluate("(el) => el.click()", elem.element_handle())
        log_debug(f"JS click executed for '{description}'")


def _dismiss_cookie_banner(page):
    log_debug("Checking for Cookiebot banner...")
    for sel in SELECTORS["cookie_consent_candidates"]:
        try:
            if page.locator(sel).is_visible():
                log_debug(f"Found cookie button '{sel}', clicking...")
                page.click(sel, timeout=1500, force=True)
                page.wait_for_timeout(300)
                break
        except Exception:
            continue

    log_debug("Removing residual Cookiebot DOM nodes via JS...")
    try:
        page.evaluate("""() => {
            const ids = ['CybotCookiebotDialog', 'CybotCookiebotDialogBodyUnderlay'];
            ids.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.remove();
            });
        }""")
    except Exception as e:
        log_debug(f"DOM cleanup note: {e}")


def _dismiss_maintenance_modal(page):
    log_debug("Checking for maintenance modal...")
    try:
        page.click(SELECTORS["maintenance_modal_dismiss"], timeout=2000, force=True)
        log_debug("Dismissed maintenance modal.")
    except PWTimeout:
        log_debug("No maintenance modal encountered.")


def _set_group_size(page, target_size: int):
    log_debug(f"Setting group size to {target_size}...")
    plus_btn = _first_visible(page, SELECTORS["group_size_plus"], "group size '+' stepper")
    for step in range(target_size - 1):
        log_debug(f"Clicking group size '+' button (step {step + 1}/{target_size - 1})")
        plus_btn.click(force=True)
        page.wait_for_timeout(200)


def _select_date(page, date_str: str):
    log_debug(f"Selecting date: {date_str}")
    target = datetime.strptime(date_str, "%Y-%m-%d")
    
    # Format date as 'DD/MM/YYYY' or 'YYYY-MM-DD' as expected by the picker input
    formatted_date = target.strftime("%d/%m/%Y")

    log_debug(f"Attempting direct input fill for date field with value: {formatted_date}")
    try:
        date_input = page.locator(SELECTORS["date_field"]).first
        date_input.scroll_into_view_if_needed()
        
        # Fill input directly and trigger JS change event
        page.evaluate("""({selector, val}) => {
            const input = document.querySelector(selector);
            if (input) {
                input.value = val;
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""", {"selector": SELECTORS["date_field"], "val": formatted_date})
        
        log_debug("Direct JS value set successful.")
        page.wait_for_timeout(300)
    except Exception as e:
        log_debug(f"Direct date fill failed: {e}. Falling back to calendar picker UI interaction.")
        
        _click_visible(page, SELECTORS["date_field"], "date field input")
        page.wait_for_timeout(500)

        today = datetime.now()
        month_diff = (target.year - today.year) * 12 + (target.month - today.month)
        nav_selector = SELECTORS["calendar_next"] if month_diff >= 0 else SELECTORS["calendar_prev"]

        log_debug(f"Navigating calendar across {abs(month_diff)} month step(s)...")
        for i in range(abs(month_diff)):
            log_debug(f"Calendar navigation step {i + 1}/{abs(month_diff)}")
            _click_visible(page, nav_selector, "calendar month navigation arrow")
            page.wait_for_timeout(300)

        day_selector = (
            f"button.pika-button[data-pika-day='{target.day}']"
            f"[data-pika-month='{target.month - 1}'][data-pika-year='{target.year}']"
        )
        log_debug(f"Clicking day cell: '{day_selector}'")
        _click_visible(page, day_selector, f"calendar day cell for {date_str}")


def check_availability(date_str: str, min_tickets: int = 2, headless: bool = HEADLESS) -> bool:
    log_debug(f"=== Starting availability check for {date_str} ===")
    with sync_playwright() as p:
        log_debug("Launching Chromium browser...")
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
                "--disable-gpu"
            ]
        )
        try:
            page = browser.new_page()

            # Resource blocking for RAM optimization
            def block_heavy_resources(route):
                if route.request.resource_type in ["image", "media", "font"]:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", block_heavy_resources)

            log_debug(f"Navigating to {TOUR_URL}...")
            page.goto(TOUR_URL, wait_until="domcontentloaded", timeout=60000)
            
            log_debug("Waiting for date input element to be attached to DOM...")
            page.wait_for_selector(SELECTORS["date_field"], timeout=30000, state="attached")

            _dismiss_cookie_banner(page)
            _dismiss_maintenance_modal(page)
            _select_date(page, date_str)
            _set_group_size(page, min_tickets)

            log_debug("Clicking CHECK AVAILABILITY button...")
            _click_visible(page, SELECTORS["check_availability_btn"], "CHECK AVAILABILITY button")
            
            log_debug("Waiting 2000ms for availability response to render...")
            page.wait_for_timeout(2000)

            page_text = page.content()
            sold_out = any(marker in page_text for marker in SELECTORS["sold_out_markers"])
            slot_count = page.locator(SELECTORS["time_slot_results"]).count()
            
            log_debug(f"Check results -> sold_out_detected: {sold_out}, time_slots_found: {slot_count}")
            return slot_count > 0 and not sold_out
        except Exception as err:
            log_debug(f"EXCEPTIONAL ERROR in check_availability({date_str}): {err}")
            traceback.print_exc()
            raise err
        finally:
            log_debug("Closing browser instance...")
            browser.close()


def check_availability_multi(dates: list, min_tickets: int = 2, headless: bool = HEADLESS) -> list:
    available = []
    for i, d in enumerate(dates):
        try:
            if check_availability(d, min_tickets, headless=headless):
                available.append(d)
        except Exception as e:
            print(f"[{datetime.now()}] Error checking {d}: {e}", file=sys.stderr, flush=True)
        if i < len(dates) - 1:
            time.sleep(2)
    return available


def poll(dates: list, min_tickets: int, interval_seconds: int = 60,
         max_checks: int = None, email_to: str = None):
    checks = 0
    while max_checks is None or checks < max_checks:
        checks += 1
        log_debug(f"Starting cycle #{checks} for dates: {dates}")
        available = check_availability_multi(dates, min_tickets)

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if available:
            print(f"[{stamp}] AVAILABLE: >= {min_tickets} tickets free on: {', '.join(available)}", flush=True)
            if email_to:
                send_email(email_to, available, min_tickets)
        else:
            print(f"[{stamp}] No availability across {len(dates)} date(s). Checking again in {interval_seconds}s.", flush=True)

        time.sleep(interval_seconds)


# ==========================================
# 3. ENTRY POINT
# ==========================================
if __name__ == "__main__":
    print(f"[hobbiton_poller] script version: {SCRIPT_VERSION}", flush=True)

    # 1. Start HTTP health check server in background thread for Render.com
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()

    # 2. Parse CLI arguments or fallback to Environment Variables
    parser = argparse.ArgumentParser(description="Poll Hobbiton Evening Banquet Tour availability.")
    parser.add_argument("--date", help="Single target date, YYYY-MM-DD")
    parser.add_argument("--start-date", help="Start of a date range, YYYY-MM-DD")
    parser.add_argument("--end-date", help="End of a date range, YYYY-MM-DD")
    parser.add_argument("--min-tickets", type=int, default=int(os.environ.get("MIN_TICKETS", 2)))
    parser.add_argument("--interval", type=int, default=int(os.environ.get("INTERVAL_SECONDS", 60)))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--email", default=os.environ.get("EMAIL_TO"))
    args = parser.parse_args()

    # Read target dates from CLI or Environment Variables
    start_date = args.start_date or os.environ.get("START_DATE")
    end_date = args.end_date or os.environ.get("END_DATE")
    single_date = args.date or os.environ.get("TARGET_DATE")

    if start_date and end_date:
        target_dates = date_range(start_date, end_date)
    elif single_date:
        target_dates = [single_date]
    else:
        # Fallback default date if none provided
        target_dates = ["2026-11-20"]
        print(f"[WARN] No dates configured via ENV/CLI. Fallback to default: {target_dates}", flush=True)

    # Run main polling loop
    poll(target_dates, args.min_tickets, args.interval, email_to=args.email)
