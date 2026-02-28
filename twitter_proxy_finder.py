"""
Twitter Proxy Finder
====================

Step 1 of 2. Run this first.

What it does:
  1. Harvests thousands of free proxies from 20+ GitHub repos + APIs (parallel)
  2. Tests every proxy specifically against x.com (parallel, 100 workers at once)
  3. Saves only the working ones to:  twitter_working_proxies.json

Then run twitter_scraper.py to use those proxies.

Usage:
    pip install requests
    python twitter_proxy_finder.py
"""

import io
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests


# ── Logging — Windows UTF-8 fix ───────────────────────────────────────────────

def _make_stream_handler() -> logging.StreamHandler:
    if sys.platform == "win32":
        utf8_out = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
        h: logging.StreamHandler = logging.StreamHandler(utf8_out)
    else:
        h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    return h

logging.basicConfig(level=logging.INFO, handlers=[])
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_fh = logging.FileHandler("twitter_proxy_finder.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)
logger.addHandler(_make_stream_handler())


# =============================================================================
# Config — edit these if needed
# =============================================================================

OUTPUT_FILE       = "twitter_working_proxies.json"  # read by twitter_scraper.py
HARVEST_WORKERS   = 50    # parallel workers fetching proxy lists from GitHub/APIs
TEST_WORKERS      = 100   # parallel workers testing proxies against Twitter
BASIC_TIMEOUT     = 10    # seconds: basic connectivity test (httpbin.org)
TWITTER_TIMEOUT   = 15    # seconds: Twitter-specific test (x.com)


# =============================================================================
# Stage 1 — Proxy Harvester
# =============================================================================

class ProxyHarvester:
    """
    Fetches proxy lists from 20+ GitHub repos and free APIs simultaneously.
    Same sources as proxy_finder.py but all requests fire in parallel.
    """

    # Same GitHub sources as your proxy_finder.py
    GITHUB_SOURCES: Dict[str, str] = {
        "TheSpeedX":            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "clarketm":             "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
        "jetkai_http":          "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
        "jetkai_https":         "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt",
        "jetkai_socks4":        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt",
        "jetkai_socks5":        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
        "monosans_http":        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "monosans_socks4":      "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
        "monosans_socks5":      "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "ErcinDedeoglu_http":   "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt",
        "ErcinDedeoglu_https":  "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/https.txt",
        "ErcinDedeoglu_socks4": "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt",
        "ErcinDedeoglu_socks5": "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt",
        "vakhov_http":          "https://vakhov.github.io/fresh-proxy-list/http.txt",
        "vakhov_https":         "https://vakhov.github.io/fresh-proxy-list/https.txt",
        "vakhov_socks4":        "https://vakhov.github.io/fresh-proxy-list/socks4.txt",
        "vakhov_socks5":        "https://vakhov.github.io/fresh-proxy-list/socks5.txt",
        "KangProxy":            "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/xResults/Proxies.txt",
        "Proxifly_all":         "https://raw.githubusercontent.com/Proxifly/free-proxy-list/main/proxies/all/data.txt",
        "Proxifly_http":        "https://raw.githubusercontent.com/Proxifly/free-proxy-list/main/proxies/http/data.txt",
    }

    API_SOURCES: List[str] = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all",
        "https://www.proxy-list.download/api/v1/get?type=http",
        "https://www.proxy-list.download/api/v1/get?type=https",
        "https://www.proxy-list.download/api/v1/get?type=socks4",
        "https://www.proxy-list.download/api/v1/get?type=socks5",
        "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc",
    ]

    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set = set()
        self.proxies: List[Dict[str, str]] = []

    def _add(self, proxy: str, source: str) -> bool:
        with self._lock:
            if proxy not in self._seen:
                self._seen.add(proxy)
                self.proxies.append({"proxy": proxy, "source": source})
                return True
        return False

    def _fetch_text(self, name: str, url: str) -> int:
        """Fetch a plain-text proxy list (GitHub raw files)."""
        try:
            r = requests.get(url, timeout=15, headers=self.HEADERS)
            if r.status_code != 200:
                return 0
            count = 0
            for line in r.text.strip().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                raw = line.split()[0]  # take first token (ip:port)
                if ":" not in raw:
                    continue
                # Add protocol prefix if missing
                if not raw.startswith(("http://", "https://", "socks4://", "socks5://")):
                    if "socks5" in name.lower():
                        raw = f"socks5://{raw}"
                    elif "socks4" in name.lower():
                        raw = f"socks4://{raw}"
                    elif "https" in name.lower():
                        raw = f"https://{raw}"
                    else:
                        raw = f"http://{raw}"
                if self._add(raw, name):
                    count += 1
            logger.info(f"[Harvest] {name}: {count} proxies")
            return count
        except Exception as exc:
            logger.debug(f"[Harvest] {name} failed: {exc}")
            return 0

    def _fetch_api(self, url: str) -> int:
        """Fetch from a JSON or plain-text proxy API."""
        domain = urlparse(url).netloc
        try:
            r = requests.get(url, timeout=15, headers=self.HEADERS)
            if r.status_code != 200:
                return 0
            count = 0
            try:
                data = r.json()
                # Geonode JSON format: {"data": [{"ip": ..., "port": ..., "protocols": [...]}]}
                if isinstance(data, dict) and "data" in data:
                    for item in data["data"]:
                        ip, port = item.get("ip"), item.get("port")
                        if not ip or not port:
                            continue
                        for proto in item.get("protocols", ["http"]):
                            if self._add(f"{proto}://{ip}:{port}", domain):
                                count += 1
            except (json.JSONDecodeError, ValueError):
                # Plain text format
                for line in r.text.strip().splitlines():
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    if not line.startswith(("http", "socks")):
                        proto = "socks5" if "socks5" in url else "http"
                        line = f"{proto}://{line}"
                    if self._add(line, domain):
                        count += 1
            logger.info(f"[Harvest] API {domain}: {count} proxies")
            return count
        except Exception as exc:
            logger.debug(f"[Harvest] API {domain} failed: {exc}")
            return 0

    def harvest(self, workers: int = 50) -> List[Dict[str, str]]:
        """
        Fetch ALL sources in parallel.
        proxy_finder.py does this sequentially with time.sleep() between each.
        Here every request fires at the same time.
        """
        logger.info(
            f"[Stage 1] Harvesting from "
            f"{len(self.GITHUB_SOURCES)} GitHub sources + "
            f"{len(self.API_SOURCES)} APIs in parallel ..."
        )

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = []
            for name, url in self.GITHUB_SOURCES.items():
                futures.append(ex.submit(self._fetch_text, name, url))
            for url in self.API_SOURCES:
                futures.append(ex.submit(self._fetch_api, url))
            for f in as_completed(futures):
                f.result()  # exceptions are caught inside each method

        logger.info(f"[Stage 1] Done: {len(self.proxies)} unique proxies harvested")
        return self.proxies


# =============================================================================
# Stage 2 — Twitter Proxy Verifier
# =============================================================================

class TwitterProxyVerifier:
    """
    Tests every proxy specifically against x.com.

    Two-step test per proxy (mirrors proxy_finder.py test_single_proxy):
      Step 1 — Basic:   GET http://httpbin.org/ip       (proves proxy routes traffic)
      Step 2 — Twitter: GET https://x.com               (proves Twitter doesn't block it)

    All tests run in parallel — 100 workers fire at once.
    Only proxies that pass BOTH steps are kept.
    Results saved to twitter_working_proxies.json.
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def test_one(
        self,
        proxy_info: Dict[str, str],
        basic_timeout: int = 10,
        twitter_timeout: int = 15,
    ) -> Dict[str, Any]:
        """
        Test a single proxy.
        Returns the proxy dict enriched with working/speed/twitter_status fields.
        Same pattern as proxy_finder.py test_single_proxy() — target changed to x.com.
        """
        proxy_url = proxy_info["proxy"]

        result: Dict[str, Any] = {
            **proxy_info,
            "working":          False,
            "speed":            999.0,
            "works_on_twitter": False,
            "twitter_status":   "not_tested",
            "tested_at":        datetime.now().isoformat(),
        }

        # Skip SOCKS proxies — requests needs pysocks for those,
        # and Playwright works better with HTTP proxies anyway
        if proxy_url.startswith(("socks4://", "socks5://")):
            result["twitter_status"] = "skipped_socks"
            return result

        proxies = {"http": proxy_url, "https": proxy_url}

        try:
            # ── Step 1: Basic connectivity ───────────────────────────────────
            t0 = time.time()
            r = requests.get(
                "http://httpbin.org/ip",
                proxies=proxies,
                timeout=basic_timeout,
                headers=self.HEADERS,
            )
            if r.status_code != 200:
                result["twitter_status"] = f"basic_failed_{r.status_code}"
                return result

            result["working"] = True
            result["speed"]   = round(time.time() - t0, 3)

            # ── Step 2: Twitter/X test ───────────────────────────────────────
            t0 = time.time()
            tr = requests.get(
                "https://x.com",
                proxies=proxies,
                timeout=twitter_timeout,
                headers=self.HEADERS,
                allow_redirects=True,
            )
            twitter_time = round(time.time() - t0, 3)

            if tr.status_code == 200:
                body = tr.text.lower()
                if "x.com" in body or "twitter" in body or "login" in body:
                    result["works_on_twitter"] = True
                    result["twitter_status"]   = "success"
                    result["speed"]            = twitter_time
                else:
                    result["twitter_status"] = "blocked_page"

            elif tr.status_code in (301, 302, 303):
                # Redirect = proxy is passing traffic cleanly
                result["works_on_twitter"] = True
                result["twitter_status"]   = "redirect_ok"
                result["speed"]            = twitter_time

            elif tr.status_code == 403:
                result["twitter_status"] = "forbidden"
            elif tr.status_code == 429:
                result["twitter_status"] = "rate_limited"
            else:
                result["twitter_status"] = f"http_{tr.status_code}"

        except requests.exceptions.Timeout:
            result["twitter_status"] = "timeout"
        except requests.exceptions.ProxyError:
            result["twitter_status"] = "proxy_error"
        except requests.exceptions.ConnectionError:
            result["twitter_status"] = "connection_error"
        except Exception as exc:
            result["twitter_status"] = f"error_{type(exc).__name__}"

        return result

    def verify_all(
        self,
        proxies: List[Dict[str, str]],
        workers: int = 100,
        basic_timeout: int = 10,
        twitter_timeout: int = 15,
    ) -> List[Dict[str, Any]]:
        """
        Test ALL proxies in parallel — same pattern as proxy_finder.test_all_proxies()
        but with workers=100 and target=Twitter instead of Reddit.
        """
        total = len(proxies)
        logger.info(
            f"[Stage 2] Testing {total} proxies against Twitter "
            f"with {workers} parallel workers ..."
        )
        logger.info(f"[Stage 2] Estimated time: {total * twitter_timeout / workers / 60:.1f} min")

        working:   List[Dict[str, Any]] = []
        lock = threading.Lock()
        completed = 0

        with ThreadPoolExecutor(max_workers=workers) as ex:
            future_map = {
                ex.submit(self.test_one, p, basic_timeout, twitter_timeout): p
                for p in proxies
            }
            for fut in as_completed(future_map):
                result = fut.result()
                with lock:
                    completed += 1
                    if result["works_on_twitter"]:
                        working.append(result)
                        logger.info(
                            f"[Stage 2] [{completed}/{total}]  PASS  "
                            f"{result['proxy']:<45}  "
                            f"{result['speed']:.2f}s  [{result['twitter_status']}]"
                        )
                    elif completed % 250 == 0:
                        # Print progress every 250 tests to avoid flooding the console
                        logger.info(
                            f"[Stage 2] [{completed}/{total}] tested | "
                            f"{len(working)} working so far ..."
                        )

        working.sort(key=lambda x: x["speed"])

        logger.info(
            f"\n[Stage 2] Done: {len(working)} / {total} proxies work on Twitter "
            f"({len(working)/max(total,1)*100:.1f}%)"
        )
        return working


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    print(
        "\n" + "=" * 60 + "\n"
        "  TWITTER PROXY FINDER\n"
        "  Step 1 of 2\n"
        "\n"
        "  Harvests proxies from 20+ sources, tests all of them\n"
        "  against x.com in parallel, saves working ones to:\n"
        f"  {OUTPUT_FILE}\n"
        "=" * 60 + "\n"
    )

    # ── Stage 1: Harvest ─────────────────────────────────────────────────────
    harvester = ProxyHarvester()
    raw = harvester.harvest(workers=HARVEST_WORKERS)

    if not raw:
        logger.error("No proxies harvested from any source. Check your internet connection.")
        return

    print(f"\nHarvested {len(raw)} unique proxies from all sources.\n")

    # ── Stage 2: Verify against Twitter ──────────────────────────────────────
    verifier = TwitterProxyVerifier()
    working = verifier.verify_all(
        proxies=raw,
        workers=TEST_WORKERS,
        basic_timeout=BASIC_TIMEOUT,
        twitter_timeout=TWITTER_TIMEOUT,
    )

    if not working:
        logger.error("No proxies passed the Twitter test. Try running again — free proxies change frequently.")
        return

    # ── Save results ──────────────────────────────────────────────────────────
    output_path = Path(OUTPUT_FILE)
    with open(output_path, "w") as f:
        json.dump(working, f, indent=2)

    # Print summary (mirrors proxy_finder.py print_summary)
    print(
        "\n" + "=" * 60 + "\n"
        f"  RESULTS\n"
        "=" * 60 + "\n"
        f"  Total proxies tested:   {len(raw):,}\n"
        f"  Working on Twitter:     {len(working)}\n"
        f"  Fastest proxy:          {working[0]['proxy']}  ({working[0]['speed']:.2f}s)\n"
        f"  Slowest proxy:          {working[-1]['proxy']}  ({working[-1]['speed']:.2f}s)\n"
        "\n"
        f"  Saved to: {output_path.resolve()}\n"
        "=" * 60 + "\n"
        "\n"
        "  Next step:\n"
        "    python twitter_scraper.py\n"
        "\n"
    )

    # Top 10 fastest
    print("  Top 10 fastest proxies:")
    for i, p in enumerate(working[:10], 1):
        print(f"    {i:2d}. {p['proxy']:<45}  {p['speed']:.2f}s")
    print()


if __name__ == "__main__":
    main()