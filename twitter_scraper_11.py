"""
Twitter/X Scraper -- Sticky Session Architecture
=================================================

Run order:
    1. python twitter_login.py          (once, saves twitter_cookies.json)
    2. python twitter_proxy_finder.py   (saves twitter_working_proxies.json)
    3. python twitter_scraper.py        (this file)

FOUR BUGS FIXED IN THIS VERSION
---------------------------------

Bug 1 -- RAM Meltdown
  Old:  max_parallel_sessions = 0 -> opens ALL 56 Chrome instances at once
  Fix:  MAX_PARALLEL_SESSIONS = 5 (configurable), sessions run in waves.

Bug 2 -- Synchronized Login Ban
  Fix:  Scraper NEVER logs in. Reads twitter_cookies.json only.

Bug 3 -- Cookie File Race Condition
  Fix:  Global COOKIE_WRITE_LOCK, one thread writes at a time.

Bug 4 -- Staggered Start
  Fix:  Each session sleeps 0-30s before navigating.

Setup:
    pip install playwright playwright-stealth
    playwright install chromium
    python twitter_login.py   # do this first!

Usage:
    python twitter_scraper.py
"""

import io
import json
import logging
import queue
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from urllib.parse import urlparse, quote

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page

# -- Playwright ----------------------------------------------------------------
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


# -- Global locks / flags ------------------------------------------------------
COOKIE_WRITE_LOCK = threading.Lock()
RATE_LIMITED      = threading.Event()


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
_fh = logging.FileHandler("twitter_scraper.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)
logger.addHandler(_make_stream_handler())


# =============================================================================
# Config
# =============================================================================

@dataclass
class Config:
    proxies_file: str = "twitter_working_proxies.json"
    cookies_file: str = "twitter_cookies.json"

    headless: bool = True
    slow_mo:  int  = 0

    max_parallel_sessions:  int   = 5
    tweets_per_session:     int   = 50
    max_scrolls_per_session:int   = 5
    scroll_pause_min:       float = 2.0
    scroll_pause_max:       float = 5.0
    page_load_pause_min:    float = 2.0
    page_load_pause_max:    float = 4.0
    stagger_min_seconds:    float = 0.0
    stagger_max_seconds:    float = 30.0

    output_dir: str = "twitter_data"

    user_agents: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.user_agents:
            self.user_agents = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
            ]


# =============================================================================
# Proxy Queue
# =============================================================================

class ProxyQueue:
    def __init__(self, proxies_file: str) -> None:
        self._q:    queue.Queue     = queue.Queue()
        self._lock                  = threading.Lock()
        self._stats: Dict[str, Dict]= {}
        self._total_loaded: int     = 0
        self._load(proxies_file)

    def _load(self, path: str) -> None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"Proxy file not found: '{path}'\n"
                "Run twitter_proxy_finder.py first to generate it."
            )
        with open(p) as f:
            data: List[Dict[str, Any]] = json.load(f)
        for item in data:
            url = item.get("proxy", "")
            if url:
                self._q.put(url)
                self._stats[url] = {
                    "speed":   item.get("speed", 9.9),
                    "success": False,
                    "tweets":  0,
                    "elapsed": 0.0,
                }
        self._total_loaded = self._q.qsize()
        logger.info(f"[ProxyQueue] {self._total_loaded} proxies loaded")

    def checkout(self) -> Optional[str]:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    def record(self, url: str, success: bool, tweets: int, elapsed: float) -> None:
        with self._lock:
            if url in self._stats:
                self._stats[url].update(
                    {"success": success, "tweets": tweets, "elapsed": elapsed}
                )

    def remaining(self) -> int:
        return self._q.qsize()

    def total(self) -> int:
        return self._total_loaded

    def summary(self) -> str:
        used    = [s for s in self._stats.values() if s["elapsed"] > 0]
        success = [s for s in used if s["success"]]
        tweets  = sum(s["tweets"] for s in used)
        return (
            f"{self._total_loaded} proxies | "
            f"{len(success)}/{len(used)} sessions ok | "
            f"{tweets} raw tweets"
        )

    def to_playwright_fmt(self, proxy_url: str) -> Dict[str, str]:
        parsed = urlparse(proxy_url)
        result: Dict[str, str] = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        }
        if parsed.username:
            result["username"] = parsed.username
        if parsed.password:
            result["password"] = parsed.password
        return result


# =============================================================================
# Health Monitor
# =============================================================================

class HealthMonitor:
    def __init__(self) -> None:
        self._lock          = threading.Lock()
        self.sessions_run   = 0
        self.sessions_ok    = 0
        self.total_tweets   = 0
        self.total_elapsed  = 0.0
        self.start          = datetime.now()

    def record(self, success: bool, tweets: int, elapsed: float) -> None:
        with self._lock:
            self.sessions_run  += 1
            self.total_elapsed += elapsed
            if success:
                self.sessions_ok  += 1
                self.total_tweets += tweets

    def report(self, proxy_summary: str = "") -> str:
        if self.sessions_run == 0:
            return "No sessions completed yet.\n"
        sr   = self.sessions_ok / self.sessions_run * 100
        avg  = self.total_elapsed / self.sessions_run
        wall = (datetime.now() - self.start).total_seconds()
        tph  = self.total_tweets / max(wall / 3600, 0.01)
        return (
            f"\n{'='*60}\n"
            f"  SCRAPER REPORT\n"
            f"{'='*60}\n"
            f"  Sessions:   {self.sessions_run} run | {self.sessions_ok} ok ({sr:.0f}%)\n"
            f"  Avg/session:{avg:.1f}s\n"
            f"  Tweets raw: {self.total_tweets:,} ({tph:.0f}/hr)\n"
            f"  Wall time:  {wall:.0f}s\n"
            + (f"  Proxies:    {proxy_summary}\n" if proxy_summary else "")
            + f"{'='*60}\n"
        )


# =============================================================================
# Sticky Session
# =============================================================================

class StickySession:
    INTERCEPT_PATTERNS = [
        "SearchTimeline",
        "UserTweets",
        "UserTweetsAndReplies",
        "TweetDetail",
        "HomeTimeline",
        "HomeLatestTimeline",
        "Likes",
        "Bookmarks",
        "ListLatestTweetsTimeline",
        "graphql",
    ]

    USER_PROFILE_PATTERNS = [
        "UserByScreenName",
        "UserByRestId",
    ]

    def __init__(
        self,
        session_id: int,
        proxy_url:  str,
        pq:         ProxyQueue,
        cfg:        Config,
    ) -> None:
        self.sid        = session_id
        self.proxy_url  = proxy_url
        self.pq         = pq
        self.cfg        = cfg
        self._bucket:   List[Dict]          = []
        self._profile:  Optional[Dict]      = None
        self._browser:  Optional["Browser"] = None
        self._ctx:      Optional["BrowserContext"] = None
        self._page:     Optional["Page"]    = None
        self._pw:       Optional[Any]       = None

    def tag(self) -> str:
        return f"[S{self.sid:03d}]"

    def _launch(self) -> None:
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.cfg.headless,
            channel="chrome",
            slow_mo=self.cfg.slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ],
        )
        opts: Dict[str, Any] = {
            "user_agent":  random.choice(self.cfg.user_agents),
            "viewport":    {"width": 1280, "height": 900},
            "locale":      "en-US",
            "timezone_id": "America/New_York",
        }
        opts["proxy"]  = self.pq.to_playwright_fmt(self.proxy_url)
        self._ctx      = self._browser.new_context(**opts)
        self._page     = self._ctx.new_page()

        if STEALTH_AVAILABLE:
            assert self._page is not None
            stealth_sync(self._page)
        else:
            assert self._ctx is not None
            self._ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver',  {get: () => undefined});
                Object.defineProperty(navigator, 'plugins',    {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages',  {get: () => ['en-US','en']});
                window.chrome = {runtime: {}};
            """)

    def _shutdown(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def _load_cookies(self) -> bool:
        p = Path(self.cfg.cookies_file)
        if not p.exists():
            return False
        try:
            assert self._ctx is not None
            with open(p) as f:
                self._ctx.add_cookies(json.load(f))
            return True
        except AssertionError:
            raise
        except Exception as exc:
            logger.warning(f"{self.tag()} Could not load cookies: {exc}")
            return False

    def _save_cookies(self) -> None:
        try:
            assert self._ctx is not None
            cookies = self._ctx.cookies()
            with COOKIE_WRITE_LOCK:
                with open(self.cfg.cookies_file, "w") as f:
                    json.dump(cookies, f, indent=2)
        except Exception as exc:
            logger.warning(f"{self.tag()} Cookie save failed: {exc}")

    def _attach_interceptor(self) -> None:
        def on_response(resp: Any) -> None:
            # 429 detection
            if resp.status == 429 and ("twitter.com" in resp.url or "x.com" in resp.url):
                if not RATE_LIMITED.is_set():
                    RATE_LIMITED.set()
                    logger.warning(f"{self.tag()} 429 on {resp.url[:80]}")
                return

            # User profile interception
            if any(pat in resp.url for pat in self.USER_PROFILE_PATTERNS):
                try:
                    data = resp.json()
                    profile = _extract_user_profile(data)
                    if profile and self._profile is None:
                        self._profile = profile
                        logger.info(
                            f"{self.tag()} Profile: @{profile.get('username')} "
                            f"({profile.get('followers', 0):,} followers)"
                        )
                except Exception:
                    pass
                return

            # Tweet interception
            if not any(pat in resp.url for pat in self.INTERCEPT_PATTERNS):
                return
            try:
                data = resp.json()
                if isinstance(data, dict):
                    for err in data.get("errors", []):
                        if err.get("code", 0) == 88:
                            if not RATE_LIMITED.is_set():
                                RATE_LIMITED.set()
                                logger.warning(f"{self.tag()} Rate limit code 88")
                            return
                for tweet in _extract_tweets(data):
                    self._bucket.append(tweet)
            except Exception:
                pass

        assert self._page is not None
        self._page.on("response", on_response)

    def open(self) -> bool:
        try:
            self._launch()
            self._attach_interceptor()
            if not self._load_cookies():
                logger.error(f"{self.tag()} No cookies. Run twitter_login.py first.")
                self._shutdown()
                return False
            logger.info(f"{self.tag()} Ready | proxy: {self.proxy_url}")
            return True
        except Exception as exc:
            logger.warning(f"{self.tag()} open() failed: {exc}")
            self._shutdown()
            return False

    def scrape(self, target_url: str) -> tuple:
        try:
            if RATE_LIMITED.is_set():
                logger.warning(f"{self.tag()} Skipping -- rate limit signalled")
                return list(self._bucket), self._profile

            stagger = random.uniform(self.cfg.stagger_min_seconds, self.cfg.stagger_max_seconds)
            if stagger > 0:
                logger.info(f"{self.tag()} Stagger: {stagger:.1f}s")
                time.sleep(stagger)

            if RATE_LIMITED.is_set():
                return list(self._bucket), self._profile

            assert self._page is not None
            logger.info(f"{self.tag()} Navigating: {target_url}")
            self._page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

            time.sleep(random.uniform(self.cfg.page_load_pause_min, self.cfg.page_load_pause_max))

            target = self.cfg.tweets_per_session
            cap    = self.cfg.max_scrolls_per_session

            for scroll_n in range(1, cap + 1):
                if RATE_LIMITED.is_set():
                    logger.warning(f"{self.tag()} Rate limit -- stopping at scroll {scroll_n-1}")
                    break
                self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                pause = random.uniform(self.cfg.scroll_pause_min, self.cfg.scroll_pause_max)
                time.sleep(pause)
                n = len(self._bucket)
                logger.info(f"{self.tag()} scroll {scroll_n}/{cap} | {n} tweets | {pause:.1f}s pause")
                if n >= target:
                    logger.info(f"{self.tag()} Target {target} reached")
                    break

        except Exception as exc:
            logger.warning(f"{self.tag()} scrape() error: {exc}")

        return list(self._bucket), self._profile

    def close(self) -> None:
        self._save_cookies()
        self._shutdown()
        logger.info(f"{self.tag()} Closed | {len(self._bucket)} tweets | proxy: {self.proxy_url}")


# =============================================================================
# Tweet Extractor  --  FIX: author fields now populated
# =============================================================================

def _is_tweet_id(value: str) -> bool:
    """Only accept pure numeric IDs. Rejects t.co URLs and empty strings."""
    if not value:
        return False
    if value.startswith("http"):
        return False
    return value.isdigit()


def _find_user(obj: Dict) -> Dict:
    """
    Try every known path Twitter uses to nest author data inside a tweet object.

    Twitter's SearchTimeline wraps tweets in an extra 'result' layer
    compared to UserTweets, which is why author was previously empty.
    We now walk ALL known schema variants.
    """
    candidates = [
        # Path 1: Standard SearchTimeline / HomeTimeline
        obj.get("core", {})
           .get("user_results", {})
           .get("result", {})
           .get("legacy", {}),

        # Path 2: tweet is itself nested under a 'tweet' key (some endpoints)
        obj.get("tweet", {})
           .get("core", {})
           .get("user_results", {})
           .get("result", {})
           .get("legacy", {}),

        # Path 3: HomeTimeline variant
        obj.get("user_results", {})
           .get("result", {})
           .get("legacy", {}),

        # Path 4: SearchTimeline sometimes has an extra 'result' wrapper
        #         around the whole tweet object -- we check the parent's user
        obj.get("core", {})
           .get("user_results", {})
           .get("result", {})
           .get("core", {})  # shouldn't be here but seen occasionally
           .get("legacy", {}),

        # Path 5: author_data sometimes stored directly under 'user' key
        obj.get("user", {}).get("legacy", {}),
    ]

    for c in candidates:
        if isinstance(c, dict) and c.get("screen_name"):
            return c

    # Path 6 (last resort): recursively search the entire object for any
    # sub-dict that looks like a user legacy dict (has screen_name + name).
    # Capped at depth 6 to avoid excessive recursion.
    def _deep_find(node: Any, depth: int = 0) -> Optional[Dict]:
        if depth > 6:
            return None
        if isinstance(node, dict):
            if node.get("screen_name") and node.get("name") and not node.get("full_text"):
                return node
            for v in node.values():
                result = _deep_find(v, depth + 1)
                if result:
                    return result
        elif isinstance(node, list):
            for item in node:
                result = _deep_find(item, depth + 1)
                if result:
                    return result
        return None

    deep = _deep_find(obj)
    return deep if deep else {}


def _extract_tweets(data: Any) -> List[Dict]:
    """
    Recursively walk Twitter's GraphQL JSON and collect real tweet objects.

    Real tweet: has rest_id (numeric) + legacy.full_text (non-empty)
    Ghost records (user stubs, URL objects): filtered by _is_tweet_id + text check
    """
    tweets:   List[Dict] = []
    seen_ids: set        = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if "legacy" in obj and "rest_id" in obj:
                rest_id = obj.get("rest_id", "")
                leg     = obj.get("legacy", {})
                text    = leg.get("full_text", leg.get("text", "")).strip()

                if not _is_tweet_id(rest_id) or not text:
                    for v in obj.values():
                        walk(v)
                    return

                if rest_id in seen_ids:
                    for v in obj.values():
                        walk(v)
                    return
                seen_ids.add(rest_id)

                user = _find_user(obj)

                is_retweet = leg.get("retweeted_status_result") is not None
                is_quote   = obj.get("quoted_status_result")    is not None
                is_reply   = bool(leg.get("in_reply_to_status_id_str", ""))
                if is_retweet:
                    tweet_type = "retweet"
                elif is_quote:
                    tweet_type = "quote"
                elif is_reply:
                    tweet_type = "reply"
                else:
                    tweet_type = "original"

                # Clean HTML entities from text
                clean_text = (
                    text.replace("&amp;", "&")
                        .replace("&lt;",  "<")
                        .replace("&gt;",  ">")
                        .replace("&quot;", '"')
                )

                screen_name = user.get("screen_name", "")

                tweet: Dict[str, Any] = {
                    "id":               rest_id,
                    "text":             clean_text,
                    "created_at":       leg.get("created_at", ""),
                    "author":           screen_name,
                    "author_name":      user.get("name", ""),
                    "author_id":        user.get("id_str", ""),
                    "author_followers": user.get("followers_count", 0),
                    "author_following": user.get("friends_count", 0),
                    "author_verified":  (
                        user.get("verified", False)
                        or user.get("is_blue_verified", False)
                    ),
                    "likes":            leg.get("favorite_count", 0),
                    "retweets":         leg.get("retweet_count", 0),
                    "replies":          leg.get("reply_count", 0),
                    "quotes":           leg.get("quote_count", 0),
                    "bookmarks":        leg.get("bookmark_count", 0),
                    "views":            obj.get("views", {}).get("count", 0),
                    "tweet_type":       tweet_type,
                    "lang":             leg.get("lang", ""),
                    "reply_to_id":      leg.get("in_reply_to_status_id_str", ""),
                    "reply_to_user":    leg.get("in_reply_to_screen_name", ""),
                    "hashtags": [
                        h["text"]
                        for h in leg.get("entities", {}).get("hashtags", [])
                    ],
                    "mentions": [
                        m.get("screen_name", "")
                        for m in leg.get("entities", {}).get("user_mentions", [])
                    ],
                    "urls": [
                        u.get("expanded_url", "")
                        for u in leg.get("entities", {}).get("urls", [])
                        if not u.get("expanded_url", "").startswith("https://twitter.com/i/web")
                    ],
                    "media": [
                        {
                            "type": m.get("type", ""),
                            "url":  m.get("media_url_https", ""),
                        }
                        for m in leg.get("entities", {}).get("media", [])
                    ],
                    # Real author URL -- falls back to /i/ only if truly no author
                    "url": (
                        f"https://x.com/{screen_name}/status/{rest_id}"
                        if screen_name
                        else f"https://x.com/i/status/{rest_id}"
                    ),
                    "scraped_at": datetime.now().isoformat(),
                }
                tweets.append(tweet)

            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return tweets


# =============================================================================
# User Profile Extractor
# =============================================================================

def _extract_user_profile(data: Any) -> Optional[Dict]:
    def walk(obj: Any) -> Optional[Dict]:
        if isinstance(obj, dict):
            leg = obj.get("legacy", {})
            if leg.get("screen_name") and not leg.get("full_text"):
                return {
                    "username":        leg.get("screen_name", ""),
                    "name":            leg.get("name", ""),
                    "id":              obj.get("rest_id", ""),
                    "bio":             leg.get("description", ""),
                    "location":        leg.get("location", ""),
                    "url":             leg.get("url", ""),
                    "followers":       leg.get("followers_count", 0),
                    "following":       leg.get("friends_count", 0),
                    "tweet_count":     leg.get("statuses_count", 0),
                    "listed_count":    leg.get("listed_count", 0),
                    "verified":        (
                        leg.get("verified", False)
                        or obj.get("is_blue_verified", False)
                    ),
                    "created_at":      leg.get("created_at", ""),
                    "profile_image":   leg.get("profile_image_url_https", ""),
                    "profile_banner":  leg.get("profile_banner_url", ""),
                    "pinned_tweet_id": (
                        leg.get("pinned_tweet_ids_str", [""])[0]
                        if leg.get("pinned_tweet_ids_str") else ""
                    ),
                    "scraped_at":      datetime.now().isoformat(),
                }
            for v in obj.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = walk(item)
                if r:
                    return r
        return None
    return walk(data)


# =============================================================================
# Twitter Scraper
# =============================================================================

class TwitterScraper:
    def __init__(self, pq: ProxyQueue, cfg: Config) -> None:
        self.pq     = pq
        self.cfg    = cfg
        self.health = HealthMonitor()
        self.output = Path(cfg.output_dir)
        self.output.mkdir(exist_ok=True)
        self._sid_counter = 0
        self._sid_lock    = threading.Lock()

    def _next_sid(self) -> int:
        with self._sid_lock:
            self._sid_counter += 1
            return self._sid_counter

    def _run_session(self, target_url: str) -> tuple:
        proxy_url = self.pq.checkout()
        if proxy_url is None:
            return [], None

        sid     = self._next_sid()
        session = StickySession(sid, proxy_url, self.pq, self.cfg)
        t0      = time.time()

        try:
            if not session.open():
                elapsed = time.time() - t0
                self.pq.record(proxy_url, False, 0, elapsed)
                self.health.record(False, 0, elapsed)
                return [], None

            tweets, profile = session.scrape(target_url)
            elapsed = time.time() - t0
            session.close()

            self.pq.record(proxy_url, True, len(tweets), elapsed)
            self.health.record(True, len(tweets), elapsed)
            return tweets, profile

        except Exception as exc:
            elapsed = time.time() - t0
            logger.error(f"[S{sid:03d}] Error: {exc}")
            try:
                session.close()
            except Exception:
                pass
            self.pq.record(proxy_url, False, 0, elapsed)
            self.health.record(False, 0, elapsed)
            return [], None

    def _parallel_scrape(self, target_url: str, label: str, limit: int) -> Dict[str, Any]:
        n_proxies = self.pq.remaining()
        n_workers = self.cfg.max_parallel_sessions

        if n_proxies == 0:
            logger.error("No proxies left. Run twitter_proxy_finder.py.")
            return {"tweets": [], "profile": None}

        RATE_LIMITED.clear()

        logger.info(
            f"\n[Scraper] Target:   {label}\n"
            f"[Scraper] Proxies:  {n_proxies} | Parallel: {n_workers}\n"
            f"[Scraper] Expected: up to {min(n_proxies * self.cfg.tweets_per_session, limit)} tweets\n"
        )

        all_batches:  List[List[Dict]] = []
        all_profiles: List[Dict]       = []

        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futures = [ex.submit(self._run_session, target_url) for _ in range(n_proxies)]
            done = 0
            for fut in as_completed(futures):
                tweets, profile = fut.result()
                all_batches.append(tweets)
                if profile:
                    all_profiles.append(profile)
                done += 1
                running_total = sum(len(b) for b in all_batches)
                logger.info(f"[Scraper] {done}/{n_proxies} done | {running_total} raw tweets")

        seen:   set        = set()
        merged: List[Dict] = []
        for batch in all_batches:
            for tweet in batch:
                tid = tweet.get("id", "")
                if tid and tid not in seen:
                    seen.add(tid)
                    merged.append(tweet)

        merged.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        raw_total = sum(len(b) for b in all_batches)

        if RATE_LIMITED.is_set():
            logger.warning(
                f"[Scraper] '{label}' ended early (429). "
                f"{len(merged)} tweets saved. Wait before re-running."
            )
        else:
            logger.info(f"[Scraper] '{label}' done: {len(merged)} unique from {raw_total} raw")

        best_profile = (
            max(all_profiles, key=lambda p: p.get("followers", 0))
            if all_profiles else None
        )
        return {"tweets": merged[:limit], "profile": best_profile}

    # -- Public API ------------------------------------------------------------

    def scrape_search(self, query: str, limit: int = 500, sort: str = "Latest") -> List[Dict]:
        f   = "live" if sort == "Latest" else "top"
        url = f"https://x.com/search?q={quote(query)}&src=typed_query&f={f}"
        return self._parallel_scrape(url, label=query, limit=limit)["tweets"]

    def scrape_user(self, username: str, limit: int = 200, include_replies: bool = False) -> Dict[str, Any]:
        tab = "/with_replies" if include_replies else ""
        url = f"https://x.com/{username}{tab}"
        return self._parallel_scrape(url, label=f"@{username}", limit=limit)

    def scrape_user_tweets(self, username: str, limit: int = 200) -> List[Dict]:
        return self.scrape_user(username, limit)["tweets"]

    def scrape_user_replies(self, username: str, limit: int = 200) -> List[Dict]:
        return self.scrape_user(username, limit, include_replies=True)["tweets"]

    def scrape_user_likes(self, username: str, limit: int = 200) -> List[Dict]:
        url = f"https://x.com/{username}/likes"
        return self._parallel_scrape(url, label=f"@{username}/likes", limit=limit)["tweets"]

    def scrape_tweet_thread(self, tweet_url: str, limit: int = 100) -> List[Dict]:
        if tweet_url.isdigit():
            tweet_url = f"https://x.com/i/web/status/{tweet_url}"
        return self._parallel_scrape(tweet_url, label=f"thread:{tweet_url[-20:]}", limit=limit)["tweets"]

    def scrape_hashtag(self, tag: str, limit: int = 500, sort: str = "Latest") -> List[Dict]:
        return self.scrape_search(f"#{tag}", limit=limit, sort=sort)

    def scrape_multiple_users(self, usernames: List[str], tweets_per_user: int = 100) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        for username in usernames:
            logger.info(f"\n[Scraper] Scraping @{username}")
            data = self.scrape_user(username, tweets_per_user)
            results[username] = data
            logger.info(
                f"[Scraper] @{username}: {len(data['tweets'])} tweets | "
                f"profile: {'found' if data['profile'] else 'not captured'}"
            )
        return results

    def scrape_multiple_queries(self, queries: List[str], tweets_per_query: int = 200, sort: str = "Latest") -> Dict[str, List[Dict]]:
        results: Dict[str, List[Dict]] = {}
        for q in queries:
            logger.info(f"\n[Scraper] Query: '{q}'")
            results[q] = self.scrape_search(q, tweets_per_query, sort)
            logger.info(f"[Scraper] '{q}': {len(results[q])} tweets")
        return results

    def save(self, data: Any, filename: str) -> Path:
        fp = self.output / filename
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"[Scraper] Saved: {fp}")
        return fp

    def report(self) -> str:
        return self.health.report(proxy_summary=self.pq.summary())


# =============================================================================
# Interactive CLI
# =============================================================================

def _ask(prompt: str, default: str = "") -> str:
    """Ask a question, return stripped answer or default."""
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return ans if ans else default


def _ask_int(prompt: str, default: int) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            print(f"  Please enter a whole number.")


def _ask_sort() -> str:
    raw = _ask("Sort order (L=Latest, T=Top)", "L").upper()
    return "Top" if raw.startswith("T") else "Latest"


def _pick_mode() -> str:
    print("""
  ╔══════════════════════════════════════════════╗
  ║         TWITTER / X  SCRAPER                ║
  ╠══════════════════════════════════════════════╣
  ║  1. Search  (keyword / hashtag / operator)  ║
  ║  2. User    (profile + tweets)              ║
  ║  3. User tweets + replies                   ║
  ║  4. User likes                              ║
  ║  5. Tweet thread                            ║
  ║  6. Multiple users                          ║
  ║  7. Multiple queries                        ║
  ║  8. Hashtag                                 ║
  ║  q. Quit                                    ║
  ╚══════════════════════════════════════════════╝""")
    return _ask("Choose a mode").lower()


def _run_cli(scraper: TwitterScraper) -> None:
    """
    Interactive loop. Runs until user types 'q'.
    Each iteration asks what to scrape, runs it, saves the result,
    and prints a summary before looping back to the menu.
    """
    while True:
        mode = _pick_mode()

        if mode in ("q", "quit", "exit"):
            print("\nGoodbye.")
            break

        # ── Mode 1: Search ───────────────────────────────────────────────────
        elif mode == "1":
            print("\n  Search operators you can use:")
            print("    #hashtag        from:username     to:username")
            print("    min_faves:N     min_retweets:N    since:YYYY-MM-DD")
            print("    until:YYYY-MM-DD  lang:en         filter:links\n")
            query = _ask("Search query (e.g. #Python, from:elonmusk)")
            if not query:
                print("  No query entered, returning to menu.")
                continue
            limit = _ask_int("Max tweets to collect", 200)
            sort  = _ask_sort()

            print(f"\n  Scraping search: '{query}' | limit={limit} | sort={sort}")
            tweets = scraper.scrape_search(query, limit=limit, sort=sort)
            if tweets:
                safe  = re.sub(r'[^\w\-]', '_', query)[:40]
                fname = f"search_{safe}_{datetime.now():%Y%m%d_%H%M%S}.json"
                fp    = scraper.save(tweets, fname)
                print(f"\n  Collected {len(tweets)} tweets")
                print(f"  Saved to:  {fp}")
                _show_sample(tweets)
            else:
                print("  No tweets collected.")

        # ── Mode 2: User profile + tweets ────────────────────────────────────
        elif mode == "2":
            username = _ask("Twitter username (without @)").lstrip("@")
            if not username:
                continue
            limit = _ask_int("Max tweets", 200)

            print(f"\n  Scraping @{username} | limit={limit}")
            result = scraper.scrape_user(username, limit=limit)

            if result["profile"]:
                p = result["profile"]
                print(f"\n  Profile: @{p['username']} ({p['name']})")
                print(f"    Followers: {p['followers']:,} | Following: {p['following']:,}")
                print(f"    Tweets:    {p['tweet_count']:,}")
                print(f"    Bio:       {p['bio'][:100]}")
                scraper.save(result["profile"], f"profile_{username}.json")
            else:
                print("  Profile metadata not captured (may need more scroll time).")

            if result["tweets"]:
                fname = f"tweets_{username}_{datetime.now():%Y%m%d_%H%M%S}.json"
                fp    = scraper.save(result["tweets"], fname)
                print(f"\n  {len(result['tweets'])} tweets saved to {fp}")
                _show_sample(result["tweets"])

        # ── Mode 3: User tweets + replies ────────────────────────────────────
        elif mode == "3":
            username = _ask("Twitter username (without @)").lstrip("@")
            if not username:
                continue
            limit = _ask_int("Max tweets+replies", 200)

            print(f"\n  Scraping @{username} tweets + replies | limit={limit}")
            tweets = scraper.scrape_user_replies(username, limit=limit)
            if tweets:
                fname = f"replies_{username}_{datetime.now():%Y%m%d_%H%M%S}.json"
                fp    = scraper.save(tweets, fname)
                print(f"\n  {len(tweets)} tweets+replies saved to {fp}")
                _show_sample(tweets)
            else:
                print("  Nothing collected.")

        # ── Mode 4: User likes ───────────────────────────────────────────────
        elif mode == "4":
            username = _ask("Twitter username (without @)").lstrip("@")
            if not username:
                continue
            limit = _ask_int("Max liked tweets", 100)

            print(f"\n  Scraping @{username} likes | limit={limit}")
            tweets = scraper.scrape_user_likes(username, limit=limit)
            if tweets:
                fname = f"likes_{username}_{datetime.now():%Y%m%d_%H%M%S}.json"
                fp    = scraper.save(tweets, fname)
                print(f"\n  {len(tweets)} liked tweets saved to {fp}")
                _show_sample(tweets)
            else:
                print("  Nothing collected (likes may be private).")

        # ── Mode 5: Tweet thread ─────────────────────────────────────────────
        elif mode == "5":
            print("\n  Paste a tweet URL or just the numeric tweet ID.")
            tweet_input = _ask("Tweet URL or ID")
            if not tweet_input:
                continue
            limit = _ask_int("Max replies to collect", 100)

            print(f"\n  Scraping thread: {tweet_input}")
            tweets = scraper.scrape_tweet_thread(tweet_input, limit=limit)
            if tweets:
                tid   = tweet_input.split("/")[-1].split("?")[0]
                fname = f"thread_{tid}_{datetime.now():%Y%m%d_%H%M%S}.json"
                fp    = scraper.save(tweets, fname)
                print(f"\n  {len(tweets)} tweets in thread saved to {fp}")
                _show_sample(tweets)
            else:
                print("  Nothing collected.")

        # ── Mode 6: Multiple users ───────────────────────────────────────────
        elif mode == "6":
            print("\n  Enter usernames one per line. Press Enter on a blank line when done.")
            usernames = []
            while True:
                u = _ask(f"  Username {len(usernames)+1} (blank to finish)").lstrip("@")
                if not u:
                    break
                usernames.append(u)
            if not usernames:
                continue
            limit = _ask_int("Tweets per user", 100)

            print(f"\n  Scraping {len(usernames)} users...")
            results = scraper.scrape_multiple_users(usernames, tweets_per_user=limit)
            for uname, data in results.items():
                if data["tweets"]:
                    fname = f"tweets_{uname}_{datetime.now():%Y%m%d_%H%M%S}.json"
                    fp    = scraper.save(data["tweets"], fname)
                    print(f"  @{uname}: {len(data['tweets'])} tweets -> {fp}")
                if data["profile"]:
                    scraper.save(data["profile"], f"profile_{uname}.json")

        # ── Mode 7: Multiple queries ─────────────────────────────────────────
        elif mode == "7":
            print("\n  Enter search queries one per line. Blank line to finish.")
            queries = []
            while True:
                q = _ask(f"  Query {len(queries)+1} (blank to finish)")
                if not q:
                    break
                queries.append(q)
            if not queries:
                continue
            limit = _ask_int("Tweets per query", 200)
            sort  = _ask_sort()

            print(f"\n  Scraping {len(queries)} queries...")
            results = scraper.scrape_multiple_queries(queries, tweets_per_query=limit, sort=sort)
            for q, tweets in results.items():
                if tweets:
                    safe  = re.sub(r'[^\w\-]', '_', q)[:30]
                    fname = f"search_{safe}_{datetime.now():%Y%m%d_%H%M%S}.json"
                    fp    = scraper.save(tweets, fname)
                    print(f"  '{q}': {len(tweets)} tweets -> {fp}")

        # ── Mode 8: Hashtag ──────────────────────────────────────────────────
        elif mode == "8":
            tag = _ask("Hashtag (without #)").lstrip("#")
            if not tag:
                continue
            limit = _ask_int("Max tweets", 500)
            sort  = _ask_sort()

            print(f"\n  Scraping #{tag} | limit={limit} | sort={sort}")
            tweets = scraper.scrape_hashtag(tag, limit=limit, sort=sort)
            if tweets:
                fname = f"hashtag_{tag}_{datetime.now():%Y%m%d_%H%M%S}.json"
                fp    = scraper.save(tweets, fname)
                print(f"\n  {len(tweets)} tweets saved to {fp}")
                _show_sample(tweets)
            else:
                print("  Nothing collected.")

        else:
            print("  Unknown option. Please enter 1-8 or q.")
            continue

        print(f"\n{scraper.report()}")
        again = _ask("\nRun another scrape? (y/n)", "y").lower()
        if again not in ("y", "yes"):
            print("\nAll done. Files saved to:", scraper.output.resolve())
            break


def _show_sample(tweets: List[Dict], n: int = 3) -> None:
    """Print a brief sample of collected tweets."""
    print(f"\n  Sample (first {min(n, len(tweets))}):")
    for t in tweets[:n]:
        author = f"@{t['author']}" if t.get("author") else "(author unknown)"
        text   = t.get("text", "")[:90].replace("\n", " ")
        print(f"    {author}: {text}")


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    if not PLAYWRIGHT_AVAILABLE:
        print("\nPlaywright not installed.\nRun: pip install playwright && playwright install chromium\n")
        return

    cookies_path = Path("twitter_cookies.json")
    if not cookies_path.exists():
        print(
            "\n" + "=" * 55 + "\n"
            "  ERROR: twitter_cookies.json not found\n"
            "=" * 55 + "\n"
            "  Run python twitter_login.py first.\n"
        )
        return

    cfg = Config(
        proxies_file            = "twitter_working_proxies.json",
        cookies_file            = "twitter_cookies.json",
        max_parallel_sessions   = 5,
        tweets_per_session      = 50,
        max_scrolls_per_session = 5,
        scroll_pause_min        = 2.0,
        scroll_pause_max        = 5.0,
        page_load_pause_min     = 2.0,
        page_load_pause_max     = 4.0,
        stagger_min_seconds     = 0.0,
        stagger_max_seconds     = 30.0,
        headless                = True,
        output_dir              = "twitter_data",
    )

    try:
        pq = ProxyQueue(cfg.proxies_file)
    except FileNotFoundError as e:
        print(f"\nERROR: {e}\n")
        return

    scraper = TwitterScraper(pq, cfg)

    print(
        "\n" + "=" * 60 + "\n"
        "  TWITTER/X SCRAPER -- STICKY SESSION MODE\n"
        f"  {pq.total()} proxies loaded | "
        f"{cfg.max_parallel_sessions} browsers at a time\n"
        f"  Output folder: {scraper.output.resolve()}\n"
        "=" * 60
    )

    try:
        _run_cli(scraper)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        print(scraper.report())


if __name__ == "__main__":
    main()
