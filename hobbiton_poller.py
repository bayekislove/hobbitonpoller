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
    python hobbiton_poller.py --start-date 2026-12-03 --end-date 2026-12-07 --min-tickets 2

Or import check_availability() / check_availability_multi() into your own
polling loop / cron job.
"""

import argparse
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------- CONFIG ---------------------------------

TOUR_URL = "https://www.hobbitontours.com/experiences/evening-banquet-tour/"

# Bump this whenever you edit the file, and check it's printed at startup —
# an easy way to confirm you're actually running the version you think you are.
SCRIPT_VERSION = "v6-poll-wait-instead-of-fixed-sleeps"

# Toggle this to False the first time you run it, so you can watch what
# happens and fix selectors if the site's markup differs from my guesses.
HEADLESS = False

# Best-guess selectors — adjust after inspecting the live page (see notes above).
SELECTORS = {
    # Cookiebot cookie-consent banner — this sits on top of everything and
    # blocks all clicks until dismissed. Trying several common Cookiebot
    # button ids/text since exact wording/variant can differ by config.
    "cookie_consent_candidates": [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "#CybotCookiebotDialogBodyButtonAccept",
        "button:has-text('Allow all')",
        "button:has-text('Allow All')",
        "button:has-text('Accept')",
    ],
    # Modal that appears about "Essential Maintenance on the Movie Set"
    "maintenance_modal_dismiss": "text=I understand, continue",
    # The date input/trigger inside the booking widget
    "date_field": "input.c-booking-form__datepicker.js-datepicker",
    # Pikaday calendar's month navigation buttons (confirmed from real markup:
    # buttons carry data-pika-day/month/year attributes; the standard Pikaday
    # css classes for prev/next are pika-prev / pika-next).
    "calendar_next": "button.pika-next",
    "calendar_prev": "button.pika-prev",
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

SMTP_HOST = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER = os.environ.get("SMTP_USER") or None
SMTP_PASS = os.environ.get("SMTP_PASS") or None

EMAIL_SUBJECT = "TICKETS FOR EVENING TOUR ARE AVAILABLE"


def send_email(to_address: str, available_dates: list, min_tickets: int):
    """Send a plain-text notification email listing which date(s) have
    availability. Requires SMTP_USER / SMTP_PASS env vars (see EMAIL CONFIG
    notes above)."""
    if not SMTP_USER or not SMTP_PASS:
        print(
            "[email] SMTP_USER / SMTP_PASS not set — skipping email send. "
            "See EMAIL CONFIG comments at the top of the script.",
            file=sys.stderr,
        )
        return False

    dates_list = "\n".join(f"  - {d}" for d in available_dates)
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
        print(f"[email] Sent notification to {to_address}")
        return True
    except Exception as e:
        print(f"[email] Failed to send: {e}", file=sys.stderr)
        return False


def date_range(start_str: str, end_str: str) -> list:
    """Return a list of YYYY-MM-DD strings from start_str to end_str, inclusive."""
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    if end < start:
        raise ValueError(f"--end-date ({end_str}) is before --start-date ({start_str})")
    days = (end - start).days
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]


def _first_visible(page, selector: str, description: str = "", timeout_ms: int = 15000, poll_ms: int = 250):
    """Return a Locator for the first element matching `selector` that is
    actually visible on screen, polling for up to timeout_ms.

    Two things make this necessary rather than a one-shot check:
    1. This page embeds the same "Book Now" widget ~12 times: once for the
       in-page widget you actually see, and ~11 hidden duplicates from the
       site header's "Book Now" mega-menu (one per tour type). A plain
       `.first` grabs whichever comes first in the DOM, which is often a
       hidden nav copy.
    2. The widget (and its Pikaday calendar, which redraws on every
       month-navigation click) renders asynchronously with variable
       timing — a one-shot visibility check can catch it mid-render and
       find 0 matches even though it appears half a second later.
    """
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
    _first_visible(page, selector, description, timeout_ms=timeout).click(timeout=5000)


def _dismiss_cookie_banner(page):
    """Dismiss the Cybot/Cookiebot consent banner if present. It sits on
    top of the page and intercepts every click until dismissed, which is
    why earlier runs failed with '<div ... CybotCookiebotDialog> intercepts
    pointer events'. Tries several common button variants since exact
    wording/ids can differ by Cookiebot configuration."""
    for sel in SELECTORS["cookie_consent_candidates"]:
        try:
            page.click(sel, timeout=2000)
            page.wait_for_timeout(300)
            return
        except PWTimeout:
            continue


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
    plus_btn = _first_visible(page, SELECTORS["group_size_plus"], "group size '+' stepper")
    for _ in range(target_size - 1):
        plus_btn.click()
        page.wait_for_timeout(200)  # tiny pause so the widget's JS can update state


def _select_date(page, date_str: str):
    """Open the date picker and select date_str (format: YYYY-MM-DD).

    The site uses the Pikaday calendar library, which renders only the
    currently-displayed month and opens on today's month. So this:
      1. opens the picker
      2. clicks the next/prev month arrow the right number of times to
         reach the target month/year
      3. clicks the exact day button, identified unambiguously via its
         data-pika-day/data-pika-month(0-indexed)/data-pika-year attributes
         (confirmed from real page markup) rather than guessing by visible
         text, which was ambiguous across months.
    """
    target = datetime.strptime(date_str, "%Y-%m-%d")
    _click_visible(page, SELECTORS["date_field"], "date field label")

    today = datetime.now()
    month_diff = (target.year - today.year) * 12 + (target.month - today.month)
    nav_selector = SELECTORS["calendar_next"] if month_diff >= 0 else SELECTORS["calendar_prev"]
    for _ in range(abs(month_diff)):
        _click_visible(page, nav_selector, "calendar month navigation arrow")

    day_selector = (
        f"button.pika-button[data-pika-day='{target.day}']"
        f"[data-pika-month='{target.month - 1}'][data-pika-year='{target.year}']"
    )
    _click_visible(page, day_selector, f"calendar day cell for {date_str}")


def check_availability(date_str: str, min_tickets: int = 2, headless: bool = HEADLESS) -> bool:
    """Return True if at least `min_tickets` are free for the Evening
    Banquet Tour on `date_str` (YYYY-MM-DD)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            # networkidle times out on this page (ongoing background
            # video/analytics traffic never lets the network fully settle).
            # Wait for the DOM instead, then explicitly wait for the widget.
            page.goto(TOUR_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(SELECTORS["date_field"], timeout=30000, state="attached")

            _dismiss_cookie_banner(page)
            _dismiss_maintenance_modal(page)
            _select_date(page, date_str)
            _set_group_size(page, min_tickets)
            _click_visible(page, SELECTORS["check_availability_btn"], "CHECK AVAILABILITY button")
            page.wait_for_timeout(2000)  # let the AJAX/result render

            page_text = page.content()
            sold_out = any(marker in page_text for marker in SELECTORS["sold_out_markers"])
            has_slots = page.locator(SELECTORS["time_slot_results"]).count() > 0

            return has_slots and not sold_out
        finally:
            # Always close, even if something above raised — an unclosed
            # browser process can otherwise interfere with the *next*
            # browser launch in check_availability_multi's loop, especially
            # on Windows (manifests as "Target page, context or browser
            # has been closed" on the following date's check).
            browser.close()


def check_availability_multi(dates: list, min_tickets: int = 2, headless: bool = HEADLESS) -> list:
    """Check each date in `dates` and return the subset that has at least
    `min_tickets` free. Launches one browser per date (simpler and more
    robust than trying to reuse widget state across dates), so this scales
    linearly with the number of dates — fine for a handful of dates like a
    5-day window, less fine for dozens."""
    available = []
    for i, d in enumerate(dates):
        try:
            if check_availability(d, min_tickets, headless=headless):
                available.append(d)
        except Exception as e:
            print(f"[{datetime.now()}] Error checking {d}: {e}", file=sys.stderr)
        if i < len(dates) - 1:
            time.sleep(2)  # let the previous browser process fully release before the next launch
    return available


def poll(dates: list, min_tickets: int, interval_seconds: int = 300,
         max_checks: int = None, email_to: str = None):
    """Poll repeatedly (checking every date in `dates` each round) until at
    least one date has availability (or max_checks reached). If email_to is
    given, sends a single notification email listing every available date
    found in that round."""
    checks = 0
    while max_checks is None or checks < max_checks:
        checks += 1
        available = check_availability_multi(dates, min_tickets)

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if available:
            print(f"[{stamp}] AVAILABLE: >= {min_tickets} tickets free on: {', '.join(available)}")
            if email_to:
                send_email(email_to, available, min_tickets)
            return True
        else:
            print(f"[{stamp}] No availability yet across {len(dates)} date(s). "
                  f"Checking again in {interval_seconds}s.")
            time.sleep(interval_seconds)
    return False


if __name__ == "__main__":
    print(f"[hobbiton_poller] script version: {SCRIPT_VERSION}")
    parser = argparse.ArgumentParser(description="Poll Hobbiton Evening Banquet Tour availability.")
    parser.add_argument("--date", help="Single target date, YYYY-MM-DD")
    parser.add_argument("--start-date", help="Start of a date range, YYYY-MM-DD (use with --end-date)")
    parser.add_argument("--end-date", help="End of a date range, YYYY-MM-DD, inclusive (use with --start-date)")
    parser.add_argument("--min-tickets", type=int, default=2, help="Minimum free tickets required")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between polls")
    parser.add_argument("--once", action="store_true", help="Check once and exit instead of polling")
    parser.add_argument("--email", help="Send a notification email to this address when tickets are available")
    args = parser.parse_args()

    if args.start_date and args.end_date:
        target_dates = date_range(args.start_date, args.end_date)
    elif args.date:
        target_dates = [args.date]
    else:
        parser.error("Provide either --date, or both --start-date and --end-date.")

    if args.once:
        available = check_availability_multi(target_dates, args.min_tickets)
        print(available if available else False)
        if available and args.email:
            send_email(args.email, available, args.min_tickets)
        sys.exit(0 if available else 1)
    else:
        poll(target_dates, args.min_tickets, args.interval, email_to=args.email)
