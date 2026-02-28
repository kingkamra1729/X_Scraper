# Twitter / X Scraper

A production-grade Twitter scraper built on **Playwright** with sticky session
architecture, automatic proxy rotation, browser fingerprint spoofing, and an
interactive CLI. Scrapes tweets, user profiles, threads, likes, and hashtags
without using the official API.

---

## Files

| File | Purpose |
|---|---|
| `twitter_login.py` | One-time manual login — saves your session cookies |
| `twitter_proxy_finder.py` | Harvests and tests free proxies against x.com |
| `twitter_scraper.py` | Main scraper with interactive CLI |
| `requirements.txt` | Python dependencies |

---

## How It Works

### The Core Problem
Twitter detects bots through **browser fingerprinting** (not just how text is typed).
When Playwright launches a browser in automation mode, it sets `navigator.webdriver = true`
and leaks other signals that Twitter's Arkose Labs security script checks instantly.

### The Solution: Three-Layer Defence

**Layer 1 — Real Chrome (`channel="chrome"`)**
The login script opens your actual installed Google Chrome instead of the Playwright
Chromium binary. Real Chrome has a full hardware fingerprint (GPU, fonts, plugins)
that the bare Chromium binary lacks.

**Layer 2 — playwright-stealth**
Patches ~20 JavaScript signals per page before any navigation:
- Removes `navigator.webdriver`
- Removes `"HeadlessChrome"` from the User-Agent string
- Spoofs canvas and WebGL fingerprints
- Fixes `navigator.plugins`, `navigator.languages`, `window.chrome.runtime`
- Patches notification permissions and other automation signals

**Layer 3 — Sticky Session Proxy Rotation**
Each browser session uses exactly one proxy for its entire lifetime, then discards it.
This mimics a real user browsing from a fixed IP instead of rapidly rotating IPs
mid-session (which is a strong bot signal).

---

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

Also install **Google Chrome** if you don't have it:
https://www.google.com/chrome/

### 2. Log In Once

```bash
python twitter_login.py
```

- A Chrome window opens at `x.com/i/flow/login`
- Log in manually (username, password, any 2FA/CAPTCHA)
- The script detects when you reach your home feed and saves `twitter_cookies.json`
- You only need to do this **once per month** (cookies expire after ~30 days)

### 3. Find Working Proxies

```bash
python twitter_proxy_finder.py
```

- Harvests thousands of free proxies from 20+ GitHub repositories
- Tests every proxy against `x.com` with 100 parallel workers
- Saves working proxies to `twitter_working_proxies.json` sorted fastest-first
- Takes 5–15 minutes depending on your network

### 4. Scrape

```bash
python twitter_scraper.py
```

An interactive menu appears:

```
  ╔══════════════════════════════════════════════╗
  ║         TWITTER / X  SCRAPER                ║
  ╠══════════════════════════════════════════════╣
  ║  1. Search  (keyword / hashtag / operator)  ║
  ║  2. User    (profile + tweets)              ║
  ║  3. User tweets + replies                   ║
  ║  4. User likes                              ║
  ║  5. Tweet thread                            ║
  ║  6. Multiple users  (parallel)              ║
  ║  7. Multiple queries                        ║
  ║  8. Hashtag                                 ║
  ║  q. Quit                                    ║
  ╚══════════════════════════════════════════════╝
```

Results are saved as JSON files in the `twitter_data/` folder.

---

## Scrape Modes

### Mode 1 — Search
Scrape tweets matching any search query. Supports Twitter's full operator syntax:

| Operator | Example | Meaning |
|---|---|---|
| Plain keyword | `python tutorial` | Tweets containing both words |
| Hashtag | `#MachineLearning` | Tweets with that hashtag |
| From user | `from:elonmusk` | Tweets by a specific user |
| To user | `to:openai` | Replies directed at a user |
| Min likes | `min_faves:1000` | Viral tweets only |
| Min retweets | `min_retweets:500` | Widely shared tweets |
| Date range | `since:2025-01-01 until:2025-03-01` | Tweets within a date window |
| Language | `python lang:en` | English tweets only |
| Has links | `AI filter:links` | Tweets containing URLs |
| Combined | `#AI from:sama min_faves:500` | Multiple operators together |

### Mode 2 — User Profile + Tweets
Scrapes a user's full profile metadata alongside their recent tweets.

**Profile data captured:**
- Username, display name, user ID
- Bio, location, website URL
- Followers, following, total tweet count
- Verified status, account creation date
- Profile picture URL, banner URL
- Pinned tweet ID

### Mode 3 — User Tweets + Replies
Same as Mode 2 but uses the `with_replies` tab, capturing both original tweets
and the user's replies to other people. Useful for conversation analysis.

### Mode 4 — User Likes
Scrapes tweets that a user has liked. Only works for accounts that haven't
set their likes to private.

### Mode 5 — Tweet Thread
Scrapes a single tweet and its full reply thread. Accepts either a full URL
(`https://x.com/user/status/123456789`) or just the numeric tweet ID.

### Mode 6 — Multiple Users (Parallel)
Scrapes multiple users **simultaneously**. All users' sessions run at the same
time sharing the proxy pool — not one user after another. With 3 users and
10 parallel sessions, all three users finish at roughly the same time as
scraping one user alone.

### Mode 7 — Multiple Queries
Scrapes several search queries sequentially, saving separate JSON files for each.

### Mode 8 — Hashtag
Convenience wrapper around search. Type `Python` and it searches `#Python`.

---

## Output Format

Each tweet is saved as a JSON object with these fields:

```json
{
  "id":               "1234567890123456789",
  "text":             "Tweet content here",
  "created_at":       "Sat Feb 28 16:00:15 +0000 2026",
  "author":           "username",
  "author_name":      "Display Name",
  "author_id":        "987654321",
  "author_followers": 12500,
  "author_following": 430,
  "author_verified":  true,
  "likes":            247,
  "retweets":         83,
  "replies":          14,
  "quotes":           5,
  "bookmarks":        31,
  "views":            "18400",
  "tweet_type":       "original",
  "lang":             "en",
  "reply_to_id":      "",
  "reply_to_user":    "",
  "hashtags":         ["Python", "AI"],
  "mentions":         ["someuser"],
  "urls":             ["https://example.com/article"],
  "media":            [{"type": "photo", "url": "https://pbs.twimg.com/..."}],
  "url":              "https://x.com/username/status/1234567890123456789",
  "scraped_at":       "2026-02-28T22:16:47.838909"
}
```

`tweet_type` is one of: `original`, `retweet`, `quote`, `reply`

---

## Architecture

### Sticky Sessions
Each browser session is bound to one proxy for its entire lifetime. When the
session ends, the proxy is returned to the pool (and recycled if needed).
This is critical — rotating proxies mid-session is a strong bot signal.

### Proxy Auto-Refill
When the proxy queue empties, it automatically shuffles and reloads the full
proxy list. A long job with 3 users × 50 proxies = 150 sessions will cycle
through the 50 proxies 3 times. The log shows `Refill #1: 47 proxies recycled`.

### Rate Limit Protection
A global `RATE_LIMITED` threading Event is shared across all sessions. When any
session receives an HTTP 429 (or Twitter error code 88 inside a 200 response),
it sets the flag. Every other session checks this flag before each scroll and
stops immediately. The scraper saves all collected tweets before exiting.

### RAM Guard
A `ThreadPoolExecutor` cap (`max_parallel_sessions = 10`) prevents all sessions
from opening at once. With 50 proxies, only 10 browsers run at any time.
When one finishes, the next starts automatically. Peak RAM usage stays bounded
at approximately `10 × 150 MB = 1.5 GB`.

### Cookie Safety
A global `COOKIE_WRITE_LOCK` (threading.Lock) ensures only one session writes
`twitter_cookies.json` at a time. Without this, two threads writing simultaneously
would corrupt the JSON file.

### Staggered Start
Each session sleeps a random 0–15 seconds before navigating to Twitter.
This spreads 50 session navigations over a 15-second window instead of
all hitting Twitter at the exact same millisecond.

---

## Configuration

Edit these values at the top of `main()` in `twitter_scraper.py`:

```python
cfg = Config(
    max_parallel_sessions   = 10,   # browsers open at once
                                    # 8 GB RAM  -> 8-12
                                    # 16 GB RAM -> 20-30
                                    # 32 GB RAM -> 40-50

    tweets_per_session      = 75,   # tweets per proxy before rotating
    max_scrolls_per_session = 8,    # page scrolls per session
    scroll_pause_min        = 1.5,  # seconds between scrolls (min)
    scroll_pause_max        = 3.5,  # seconds between scrolls (max)
    stagger_max_seconds     = 15.0, # max random delay before each session starts
    headless                = True, # False = show browser windows (debug mode)
    output_dir              = "twitter_data",
)
```

---

## File Structure After Running

```
your_project/
├── twitter_login.py
├── twitter_proxy_finder.py
├── twitter_scraper.py
├── requirements.txt
│
├── twitter_cookies.json          # saved by twitter_login.py
├── twitter_working_proxies.json  # saved by twitter_proxy_finder.py
│
├── twitter_login.log             # login session log
├── twitter_proxy_finder.log      # proxy test log
├── twitter_scraper.log           # scraper session log
│
└── twitter_data/                 # all scraped output
    ├── search_python_20260228_221647.json
    ├── tweets_gvanrossum_20260228_221900.json
    ├── profile_gvanrossum.json
    ├── hashtag_MachineLearning_20260228_222100.json
    └── thread_1234567890_20260228_222300.json
```

---

## Troubleshooting

**`twitter_cookies.json` not created after login**
The emergency save runs automatically on timeout and saves whatever cookies exist.
Check `twitter_login.log` for the final URL — if it still shows `/i/flow/login`,
the browser's URL wasn't changing (possible Chrome profile isolation issue).
Try deleting the file and re-running `twitter_login.py`.

**`author` field is empty in output**
This happens when Twitter changes its GraphQL schema. The scraper tries 6 different
paths to find the author data including a deep recursive fallback. If all fail,
the tweet is still saved — just without author metadata. Re-running a fresh proxy
scan sometimes helps as different proxies may hit different CDN nodes.

**Sessions fail immediately / `open() failed`**
Usually a bad proxy. The scraper automatically skips failed proxies and moves on.
If most sessions are failing, run `twitter_proxy_finder.py` again to get a fresh
proxy list — free proxies have a lifespan of hours to days.

**429 Too Many Requests**
All sessions stop immediately when a 429 is detected. Wait 5–15 minutes before
re-running. Reduce `max_parallel_sessions` or increase `scroll_pause_min/max`
to reduce request rate.

**`playwright install chromium` not found**
Make sure Playwright installed correctly: `pip show playwright`
Then: `python -m playwright install chromium`

**Chrome not found (`channel="chrome"`)**
Install Google Chrome from https://www.google.com/chrome/ or change
`channel="chrome"` to `channel="chromium"` in `twitter_login.py` to use the
Playwright-managed Chromium binary instead (slightly less effective against
fingerprinting but works without Chrome installed).

---

## Legal Notice

This tool is for **personal research and educational use only**. Scraping Twitter/X
may violate their Terms of Service. Do not use this to:
- Collect data for commercial purposes without permission
- Harass, stalk, or target individuals
- Build datasets for training AI models without proper licensing
- Violate any applicable privacy laws (GDPR, CCPA, etc.)

You are solely responsible for how you use this tool.
