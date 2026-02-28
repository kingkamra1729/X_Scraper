"""
Twitter One-Time Login (Semi-Manual Flow)
=========================================

Opens your real Chrome, you log in manually, script saves the cookies.

Usage:
    pip install playwright playwright-stealth
    playwright install chromium
    python twitter_login.py
"""

import io
import json
import logging
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False


# -- Logging (Windows UTF-8 fix) -----------------------------------------------

def _make_stream_handler() -> logging.StreamHandler:
    if sys.platform == "win32":
        utf8 = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
        h: logging.StreamHandler = logging.StreamHandler(utf8)
    else:
        h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    return h

logging.basicConfig(level=logging.INFO, handlers=[])
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Log to BOTH terminal and a file so nothing is lost
_fh = logging.FileHandler("twitter_login.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)
logger.addHandler(_make_stream_handler())


# =============================================================================
# Settings
# =============================================================================

COOKIES_FILE = "twitter_cookies.json"

# Every URL pattern that means you are logged in.
# Checked with 'in' so partial matches work.
SUCCESS_PATTERNS = ["/home", "/explore", "/notifications", "/messages"]

# Patterns that mean we are STILL on the login flow (not done yet)
LOGIN_FLOW_PATTERNS = ["/i/flow/", "/login"]


# =============================================================================
# Helpers
# =============================================================================

def _logged_in(url: str) -> bool:
    if any(p in url for p in LOGIN_FLOW_PATTERNS):
        return False
    if "x.com" not in url and "twitter.com" not in url:
        return False
    return any(p in url for p in SUCCESS_PATTERNS)


def _save(ctx, cookies_path: Path) -> bool:
    """Save cookies to disk. Returns True on success."""
    try:
        cookies = ctx.cookies()
        if not cookies:
            logger.error("ctx.cookies() returned an empty list -- nothing to save.")
            return False
        with open(cookies_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)
        logger.info(f"Saved {len(cookies)} cookies -> {cookies_path.resolve()}")
        return True
    except Exception as exc:
        logger.error(f"Cookie save failed: {exc}")
        return False


# =============================================================================
# Login
# =============================================================================

def login_and_save_cookies() -> bool:
    cookies_path = Path(COOKIES_FILE)

    if cookies_path.exists():
        print(f"\nCookies file already exists: {cookies_path.resolve()}")
        print("Delete it and re-run if you need a fresh login.\n")
        return True

    print(
        "\n" + "=" * 55 + "\n"
        "  TWITTER MANUAL AUTHENTICATION\n"
        "=" * 55 + "\n"
        "  A Chrome window will open.\n"
        "  Log in to X yourself -- the script will watch\n"
        "  the URL and save cookies the moment you reach\n"
        "  your home feed.\n"
        "  (5 minute timeout)\n"
        "=" * 55 + "\n"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        page = ctx.new_page()

        if STEALTH_AVAILABLE:
            stealth_sync(page)
            logger.info("Stealth active")
        else:
            logger.warning("playwright-stealth not installed. Run: pip install playwright-stealth")

        logger.info("Opening x.com login page...")
        page.goto("https://x.com/i/flow/login")

        # ── Poll loop: 5 minutes, print URL every 10 seconds ──────────────────
        # Extended to 300 seconds (5 min) so slow logins don't time out.
        # Prints current URL every 10 seconds -- this is how you diagnose
        # whether the script is seeing the right page.
        logged_in = False
        logger.info("Waiting for you to log in... (checking URL every 10s)")

        for i in range(300):
            time.sleep(1)
            current_url = page.url

            # Print URL every 10 seconds so you can see what's happening
            if i % 10 == 0:
                logger.info(f"[{i:03d}s] URL: {current_url}")

            if _logged_in(current_url):
                logger.info(f"Login detected! URL: {current_url}")
                logged_in = True
                break

        # ── If loop ended without detecting login ──────────────────────────────
        if not logged_in:
            final_url = page.url
            logger.warning(
                f"Auto-detection timed out or failed.\n"
                f"Final URL: {final_url}\n"
                "Attempting emergency cookie save anyway..."
            )
            # Try saving whatever cookies exist at this point.
            # Even if URL detection failed, if you ARE logged in the cookies
            # will be valid and the save will work.
            time.sleep(2)
            saved = _save(ctx, cookies_path)
            browser.close()
            if saved:
                logger.info(
                    "Emergency save succeeded. Check twitter_cookies.json exists.\n"
                    "If the file is there, you can proceed to the next step."
                )
                return True
            else:
                logger.error(
                    "No cookies saved.\n"
                    "Most likely cause: login was not completed before timeout.\n"
                    "Try again and make sure you reach the home feed."
                )
                return False

        # ── Normal path: login detected ────────────────────────────────────────
        # Wait 3 seconds for post-login cookies to fully settle
        logger.info("Pausing 3s for cookies to settle...")
        time.sleep(3)

        saved = _save(ctx, cookies_path)
        browser.close()

        if not saved:
            return False

    print(
        "\n" + "=" * 55 + "\n"
        "  COOKIES SAVED SUCCESSFULLY\n"
        "=" * 55 + "\n"
        f"  File: {Path(COOKIES_FILE).resolve()}\n"
        "\n"
        "  Next steps:\n"
        "    1. python twitter_proxy_finder.py\n"
        "    2. python twitter_scraper.py\n"
        "\n"
        "  Re-run this script only if cookies expire (~30 days).\n"
        "=" * 55 + "\n"
    )
    return True


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    if not PLAYWRIGHT_AVAILABLE:
        print(
            "\nPlaywright not installed.\n"
            "Run:  pip install playwright && playwright install chromium\n"
        )
        return

    logger.info("=" * 55)
    logger.info("twitter_login.py starting")
    logger.info("=" * 55)

    if not login_and_save_cookies():
        sys.exit(1)


if __name__ == "__main__":
    main()
