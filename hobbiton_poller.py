#!/usr/bin/env python3
"""
Hobbiton Evening Banquet Tour — ticket availability poller.

WHAT THIS DOES
--------------
The Hobbiton booking widget never shows a raw seat count publicly — it only
shows one of: "Not Available", "Fully Booked", "Book Now - Limited Places"
(<=9 left), or "Book Now" (>10 left). So to answer "are there >= N free
tickets on date X", this script actually drives the real booking widget:
it sets the group size to N, picks the date, clicks "CHECK AVAILABILITY",
and checks whether the site returns a bookable time slot (=> at least N
tickets are free) or a "fully booked / no availability" response (=> fewer
than N are free).

IMPORTANT - PLEASE READ
------------------------
This site renders its booking widget with JavaScript, so this script uses
Playwright (a real headless browser) rather than a simple HTTP scraper.

I was NOT able to open live devtools / inspect the actual rendered DOM
myself while writing this (no live network access in my sandbox), so the
CSS selectors below are my best inference from the page's visible text and
typical widget structure. They will very likely need small adjustments.

HOW TO CALIBRATE THE SELECTORS (one-time, ~5 minutes):
  1. pip install playwright && playwright install chromium
  2. Run once with HEADLESS = False (near the bottom of this file) so you
     can watch the browser and see where it gets stuck.
  3. Right-click the element it's failing to find on
     https://www.hobbitontours.com/experiences/evening-banquet-tour/
     -> Inspect -> copy a stable selector (id, data-* attribute, or
     unique class) and paste it into the CONFIG section below.
  4. Common trouble spots: the date-picker popup, the "+" stepper button,
     and how a "sold out" result is worded (span/div text).

USAGE
-----
    pip install playwright
    playwright install chromium
    python hobbiton_poller.py --date 2026-08-15 --min-tickets 2

Or import check_availability() into your own polling loop / cron job.
"""

import argparse
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------- CONFIG ---------------------------------

TOUR_URL = "https://www.hobbitontours.com/experiences/evening-banquet-tour/"

# Toggle this to False the first time you run it, so you can watch what
# happens and fix selectors if the site's markup differs from my guesses.
HEADLESS = True

# Best-guess selectors — adjust after inspecting the live page (see notes above).
SELECTORS = {
    # Modal that appears about "Essential Maintenance on the Movie Set"
    "maintenance_modal_dismiss": "text=I understand, continue",
    # The date input/trigger inside the booking widget
    "date_field": "text=Please select date",
    # The "+" button used to increase group size
    "group_size_plus": "button:has-text('+')",
    # The "-" button, in case we need to reset group size down first
    "group_size_minus": "button:has-text('-')",
    # Element showing current group size value (adjust if it's an input instead)
    "group_size_value": "[class*='group-size'] input, [class*='qty'] input",
    # The submit/check button
    "check_availability_btn": "text=CHECK AVAILABILITY",
    # Text that indicates no availability
    "sold_out_markers": ["Fully Booked", "Not Available", "Sold Out", "No availability"],
    # Selector for bookable time-slot results once a search succeeds
    "time_slot_results": "[class*='timeslot'], [class*='time-slot'], button:has-text(':')",
}

# ------------------------------------------------------------------------

# ----------------------------- EMAIL CONFIG -----------------------------
# Reads SMTP credentials from environment variables so you never have to
# hardcode a password in this file. Set these before running, e.g.:
#
#   export SMTP_HOST=smtp.gmail.com
#   export SMTP_PORT=587
#   export SMTP_USER=you@gmail.com
#   export SMTP_PASS=your-app-password   # Gmail needs an "App Password", not your normal password
#
# (For Gmail: enable 2FA, then create an App Password at
#  https://myaccount.google.com/apppasswords — a regular password will be rejected.)
# Any other SMTP provider (Outlook, SendGrid, your own mail server, etc.)
# works the same way — just point SMTP_HOST/PORT at it.

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")

EMAIL_SUBJECT = "TICKETS FOR EVENING TOUR ARE AVAILABLE"


def send_email(to_address: str, date_str: str, min_tickets: int):
    """Send a plain-text notification email. Requires SMTP_USER / SMTP_PASS
    env vars to be set (see EMAIL CONFIG notes above)."""
    if not SMTP_USER or not SMTP_PASS:
        print(
            "[email] SMTP_USER / SMTP_PASS not set — skipping email send. "
            "See EMAIL CONFIG comments at the top of the script.",
            file=sys.stderr,
        )
        return False

    body = (
        f"At least {min_tickets} tickets are available for the "
        f"Hobbiton Evening Banquet Tour on {date_str}.\n\n"
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
        print(f"[email] Sent notification to {to_address}")
        return True
    except Exception as e:
        print(f"[email] Failed to send: {e}", file=sys.stderr)
        return False


def _dismiss_maintenance_modal(page):
    try:
        page.click(SELECTORS["maintenance_modal_dismiss"], timeout=3000)
    except PWTimeout:
        pass  # modal wasn't shown, that's fine


def _set_group_size(page, target_size: int):
    """Click the '+' stepper until group size reaches target_size.

    NOTE: this assumes the widget starts at 0 or 1 and has no built-in
    numeric input you can just type into. If it has a numeric <input>,
    it's more reliable to page.fill() that field directly instead —
    check SELECTORS['group_size_value'] and adjust this function.
    """
    plus_btn = page.locator(SELECTORS["group_size_plus"]).first
    for _ in range(target_size):
        plus_btn.click()
        page.wait_for_timeout(200)  # tiny pause so the widget's JS can update state


def _select_date(page, date_str: str):
    """Open the date picker and select date_str (format: YYYY-MM-DD).

    Date pickers vary a lot between widget vendors (day-cell buttons,
    aria-labels, etc.) — this is the piece most likely to need a rewrite
    once you inspect the real picker markup.
    """
    target = datetime.strptime(date_str, "%Y-%m-%d")
    page.click(SELECTORS["date_field"])
    # Common pattern: calendar shows day cells with an aria-label like
    # "15 August 2026" or a data-date="2026-08-15" attribute. Try a few:
    day_label = target.strftime("%-d %B %Y")  # e.g. "15 August 2026"
    candidates = [
        f"[aria-label='{day_label}']",
        f"[data-date='{date_str}']",
        f"text='{target.day}'",
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() > 0:
            loc.first.click()
            return
    raise RuntimeError(
        f"Could not find a clickable date cell for {date_str}. "
        "Inspect the live date picker and update _select_date()."
    )


def check_availability(date_str: str, min_tickets: int = 2, headless: bool = HEADLESS) -> bool:
    """Return True if at least `min_tickets` are free for the Evening
    Banquet Tour on `date_str` (YYYY-MM-DD)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(TOUR_URL, wait_until="networkidle")

        _dismiss_maintenance_modal(page)
        _select_date(page, date_str)
        _set_group_size(page, min_tickets)
        page.click(SELECTORS["check_availability_btn"])
        page.wait_for_timeout(2000)  # let the AJAX/result render

        page_text = page.content()
        sold_out = any(marker in page_text for marker in SELECTORS["sold_out_markers"])
        has_slots = page.locator(SELECTORS["time_slot_results"]).count() > 0

        browser.close()
        return has_slots and not sold_out


def poll(date_str: str, min_tickets: int, interval_seconds: int = 300,
         max_checks: int = None, email_to: str = None):
    """Poll repeatedly until availability is found (or max_checks reached).
    If email_to is given, sends a notification email as soon as tickets
    are found available."""
    checks = 0
    while max_checks is None or checks < max_checks:
        checks += 1
        try:
            available = check_availability(date_str, min_tickets)
        except Exception as e:
            print(f"[{datetime.now()}] Error during check: {e}", file=sys.stderr)
            available = False

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if available:
            print(f"[{stamp}] AVAILABLE: >= {min_tickets} tickets free on {date_str}!")
            if email_to:
                send_email(email_to, date_str, min_tickets)
            return True
        else:
            print(f"[{stamp}] Not yet available on {date_str}. Checking again in {interval_seconds}s.")
            time.sleep(interval_seconds)
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll Hobbiton Evening Banquet Tour availability.")
    parser.add_argument("--date", required=True, help="Target date, YYYY-MM-DD")
    parser.add_argument("--min-tickets", type=int, default=2, help="Minimum free tickets required")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between polls")
    parser.add_argument("--once", action="store_true", help="Check once and exit instead of polling")
    parser.add_argument("--email", help="Send a notification email to this address when tickets are available")
    args = parser.parse_args()

    if args.once:
        result = check_availability(args.date, args.min_tickets)
        print(result)
        if result and args.email:
            send_email(args.email, args.date, args.min_tickets)
        sys.exit(0 if result else 1)
    else:
        poll(args.date, args.min_tickets, args.interval, email_to=args.email)
