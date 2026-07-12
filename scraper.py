# SCHOLARSHIP & VISA EMAIL ROBOT - SELF-SUSTAINING ENGINE
# Adapted from the "romance_robot" template for the lulllitcloud Scholarship & Visa Services niche.
# Layer 1: URL TTL        — URLs expire after 7 days, get revisited weekly
# Layer 2: Daily modifier — rotating search terms, fresh DDG results each day
# Layer 3: Auto-keywords  — 1.04M combinatorial pool, 750 selected per day via date-seed
# Layer 4: Blog targeting — blogspot.com + wordpress.com per keyword
# Layer 5: Email dorking  — "gmail.com/yahoo/hotmail" + scholarship-applicant terms → email in snippet

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
import time
import re
import os
import hashlib
from datetime import datetime, timedelta
import json
import random
from fake_useragent import UserAgent
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # suppress SSL verify=False warnings

# Detect GitHub Actions environment
IS_GITHUB_ACTIONS = os.environ.get('GITHUB_ACTIONS', 'false').lower() == 'true'
BATCH = int(os.environ.get('BATCH', '0'))  # 0 = run all (local), 1-6 = batch

# ============================================
# CONSTANTS
# ============================================

TRACKER_FILE      = "last_run.json"
VISITED_URLS_FILE = "visited_urls.json"
MASTER_EMAILS_FILE = "master_emails.txt"
YIELD_TRACKER_FILE = "yield_tracker.json"
URL_TTL_DAYS      = 7    # revisit URLs after 7 days (weekly cycle = sustainable yield)
KEYWORDS_PER_DAY  = 750  # 125 keywords/batch × 6 batches; Bing primary search fits 82-min budget

# Adaptive engine thresholds
TARGET_DAILY      = 750   # emails/day target
DROP_L1           = 0.30  # 30% drop → expand keyword pool
DROP_L2           = 0.50  # 50% drop → add new platforms to dork
DROP_L3           = 0.70  # 70% drop → purge stale cache + max dork volume

# 4 DDG regions — kept for region-mapping logic in dork engine
DDG_REGIONS = ['us-en', 'uk-en', 'au-en', 'ca-en']

# Search: Bing primary (Azure→Azure, no proxy) + DDG HTML via proxy fallback
# SEARXNG_INSTANCES removed — searxng_search() was dead code, never called in pipeline

# Daily rotating search modifier — different DDG results each day of week
DAILY_MODIFIERS = [
    "scholarship",       # Monday
    "study abroad",      # Tuesday
    "admission",         # Wednesday
    "funding",           # Thursday
    "visa",              # Friday
    "tuition free",      # Saturday
    "fully funded",      # Sunday
]

# Scholarship/study-abroad blog directories — bypasses search engine entirely
# Verified live via Feedspot search (2026-07-12). Feedspot's directory subdomain is
# bloggers.feedspot.com, not blog.feedspot.com (this template's original used the older subdomain).
BLOG_DIRECTORIES = [
    "https://bloggers.feedspot.com/scholarship_blogs/",
    "https://bloggers.feedspot.com/study_abroad_blogs/",
    "https://bloggers.feedspot.com/international_education_blogs/",
    "https://bloggers.feedspot.com/higher_education_blogs/",
    "https://bloggers.feedspot.com/student_blogs/",
    "https://bloggers.feedspot.com/exchange_student_blogs/",
    # Community/forum sources — verified live 2026-07-12
    "https://www.reddit.com/r/IWantOut/",
    "https://www.reddit.com/r/StudyAbroad/",
    "https://www.nairaland.com/education",
    # African scholarship-listing blogs (verified live 2026-07-12)
    "https://www.afterschoolafrica.com/",
    "https://www.scholarshipregion.com/",
    "https://www.scholars4dev.com/",
]

# ============================================
# PROXY & USER AGENT
# ============================================

import threading

_proxy_env = os.environ.get('PROXY_LIST', '')
PROXY_LIST = [p.strip().rstrip('/') for p in _proxy_env.split(',') if p.strip()]
_PROXY_LOCK = threading.Lock()           # guards all PROXY_LIST mutations
SKIP_DDG_NO_PROXY = False               # Set True at startup if 0 proxies found
PROXY_DEPLETED = False                  # Set True mid-run if pool drops to 0

_token_env = os.environ.get('GITHUB_TOKENS', '')
GITHUB_TOKENS = [t.strip() for t in _token_env.split(',') if t.strip()]

# Module-level scraper deadline — set by daily_scrape(), checked anywhere including dork_search()
_SCRAPER_DEADLINE = None  # float (time.time() + seconds) or None = no limit

def _out_of_time():
    """Returns True if the soft deadline has passed. Safe to call from any function."""
    if _SCRAPER_DEADLINE is None:
        return False
    if time.time() >= _SCRAPER_DEADLINE:
        print("  SOFT TIMEOUT: 82 min reached — saving and exiting for auto-commit")
        return True
    return False

# ============================================
# FREE PROXY AUTO-FETCH (runs at startup if no paid proxies)
# ============================================
# Strategy: 13 sources → ~3,000-8,000 candidates
# Parallel testing (50 threads, 2s timeout) → ~1,125 proxies tested in 45s
# At 3-5% success rate → ~33-56 working proxies reliably
# HARD RULE: NEVER use GitHub raw IP for DDG — skip batch if 0 proxies found

from concurrent.futures import ThreadPoolExecutor, as_completed

FREE_PROXY_SOURCES = [
    # API sources (largest lists)
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all",
    "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http",
    # GitHub maintained lists (most reliable uptime)
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/opsxcq/proxy-list/master/list.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/mertguvencli/http-proxy-list/main/proxy-list/data.txt",
    "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/http.txt",
]

def _fetch_free_proxies():
    """Download raw proxy lists from all sources in parallel."""
    raw = []
    def _fetch_one(source):
        try:
            r = requests.get(source, timeout=4)
            # geonode returns JSON, others return plain text
            if 'geonode' in source:
                data = json.loads(r.text)
                return [item['ip'] + ':' + item['port'] for item in data.get('data', [])]
            return [p.strip() for p in r.text.strip().splitlines() if p.strip()]
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=13) as ex:
        for result in ex.map(_fetch_one, FREE_PROXY_SOURCES):
            raw.extend(result)

    # Deduplicate and format as http://IP:PORT
    seen = set()
    proxies = []
    for p in raw:
        p = p.strip()
        if ':' in p and p not in seen:
            seen.add(p)
            proxies.append('http://' + p if not p.startswith('http') else p)
    return proxies

def _test_proxy(proxy):
    """
    Basic HTTPS liveness check against a neutral target.
    DDG blocks most free proxies at homepage level even when the proxy is HTTPS-capable.
    Proxies that fail DDG specifically get evicted at scrape time via _evict_proxy().
    2s timeout: dead proxies fail in <0.3s, live ones respond in <1s.
    """
    try:
        r = requests.get('https://api.ipify.org',
                         proxies={'http': proxy, 'https': proxy},
                         timeout=2, verify=False)
        return r.status_code == 200
    except Exception:
        return False

def _load_working_free_proxies(target=150, time_limit=45):
    """
    Fetch + parallel-test proxies within time_limit seconds.
    50 threads × 2s timeout → ~1,125 proxies tested in 45s.
    target=150: fetch more to account for DDG-specific failures evicted at scrape time.
    At 5% success rate → ~56 working proxies minimum.
    At 3% success rate → ~33 working proxies minimum.
    Returns up to target working proxies.
    """
    print("  Fetching proxy lists from " + str(len(FREE_PROXY_SOURCES)) + " sources in parallel...")
    raw = _fetch_free_proxies()
    random.shuffle(raw)
    # Test up to 2,000 candidates — larger pool compensates for DDG evictions at scrape time
    candidates = raw[:2000]
    print("  " + str(len(raw)) + " candidates found — parallel-testing " + str(len(candidates)) + " (max 45s, 50 threads)...")

    working = []
    start = time.time()
    tested = 0

    executor = ThreadPoolExecutor(max_workers=100)
    futures = {executor.submit(_test_proxy, p): p for p in candidates}
    try:
        for future in as_completed(futures):
            if time.time() - start > time_limit or len(working) >= target:
                break
            tested += 1
            try:
                if future.result():
                    working.append(futures[future])
            except Exception:
                pass
            if tested % 200 == 0:
                print("  Tested " + str(tested) + " | Working: " + str(len(working)) + " | " +
                      str(int(time.time() - start)) + "s elapsed")
    finally:
        # cancel_futures=True (Python 3.9+) kills queued threads instantly — no waiting
        executor.shutdown(wait=False, cancel_futures=True)

    elapsed = int(time.time() - start)
    print("  RESULT: " + str(len(working)) + " HTTPS-capable proxies found in " + str(elapsed) + "s")
    print("  NOTE: Proxies confirmed for basic HTTPS — DDG failures evicted at scrape time")
    return working

def get_next_proxy():
    """Thread-safe proxy selection. Returns None if pool is empty."""
    with _PROXY_LOCK:
        if not PROXY_LIST:
            return None
        return random.choice(PROXY_LIST)

def _init_proxy_list():
    """
    Called once at startup. Validates paid proxies in parallel if present.
    Removes dead ones, keeps alive ones, supplements with free proxies if pool < 20.
    HARD RULE: if no proxies found, set a global flag to skip DDG entirely.
    GitHub's raw IP must NEVER be used for DDG — it will get blacklisted.
    """
    global PROXY_LIST, SKIP_DDG_NO_PROXY
    SKIP_DDG_NO_PROXY = False

    if PROXY_LIST:
        # Validate sample in parallel — same speed as free proxy testing, no startup penalty
        sample = PROXY_LIST[:10]
        print("  Validating " + str(len(sample)) + " paid proxies in parallel...")
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(_test_proxy, sample))
        alive_proxies = [p for p, ok in zip(sample, results) if ok]
        dead_proxies  = set(p for p, ok in zip(sample, results) if not ok)
        alive = len(alive_proxies)
        print("  Paid proxy check: " + str(alive) + "/" + str(len(sample)) + " alive")

        # Remove confirmed-dead proxies from pool immediately
        with _PROXY_LOCK:
            PROXY_LIST[:] = [p for p in PROXY_LIST if p not in dead_proxies]
        print("  Removed " + str(len(dead_proxies)) + " dead proxies — " + str(len(PROXY_LIST)) + " remaining")

        if len(PROXY_LIST) >= 20:
            print("  PROXY_LIST: " + str(len(PROXY_LIST)) + " paid proxies accepted")
            return  # pool healthy — use them as-is

        # Pool too thin — supplement with free proxies
        print("  Pool thin (" + str(len(PROXY_LIST)) + ") — supplementing with free proxies...")

    if IS_GITHUB_ACTIONS:
        free = _load_working_free_proxies(target=200, time_limit=45)
        if free:
            with _PROXY_LOCK:
                existing = set(PROXY_LIST)
                new_only = [p for p in free if p not in existing]
                PROXY_LIST.extend(new_only)
            print("  PROXY_LIST: " + str(len(PROXY_LIST)) + " proxies ready (paid + free) — DDG scraping enabled")
        elif not PROXY_LIST:
            SKIP_DDG_NO_PROXY = True
            print("  CRITICAL: 0 working proxies — DDG keyword phase SKIPPED to protect GitHub IP")
            print("  Only dork engine (proxy-required mode) and blog directories will run")
        else:
            print("  Free proxy fetch failed — continuing with " + str(len(PROXY_LIST)) + " paid proxies")

_TOPUP_IN_PROGRESS = False  # prevents concurrent top-up calls

def _maybe_topup_proxies():
    """
    Called every 10 keywords OR immediately when PROXY_DEPLETED=True.
    If pool drops to ≤10 or is depleted, refetch free proxies (30s cap) to keep scraping alive.
    Single-threaded context only — no lock needed on the flag itself.
    """
    global PROXY_DEPLETED, _TOPUP_IN_PROGRESS
    # NOTE: intentionally NOT blocking on PROXY_DEPLETED — topup is the fix for depletion
    if SKIP_DDG_NO_PROXY or _TOPUP_IN_PROGRESS:
        return
    with _PROXY_LOCK:
        remaining = len(PROXY_LIST)
    if remaining > 10 and not PROXY_DEPLETED:
        return  # pool healthy and not depleted — no action needed
    print("  PROXY LOW (" + str(remaining) + " remaining) — topping up mid-run...")
    _TOPUP_IN_PROGRESS = True
    try:
        fresh = _load_working_free_proxies(target=100, time_limit=45)
        if fresh:
            with _PROXY_LOCK:
                existing = set(PROXY_LIST)
                new_only = [p for p in fresh if p not in existing]
                PROXY_LIST.extend(new_only)
            PROXY_DEPLETED = False  # reset — pool is alive again
            print("  Top-up complete: +" + str(len(new_only)) + " proxies → pool now " + str(len(PROXY_LIST)))
        else:
            print("  Top-up failed: no new proxies found — continuing with remaining pool")
    finally:
        _TOPUP_IN_PROGRESS = False

try:
    _ua = UserAgent()
    def get_random_user_agent():
        return _ua.random
except Exception:
    def get_random_user_agent():
        return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'

# ============================================
# DIAGNOSTICS
# ============================================

def print_startup_diagnostics():
    print("=" * 60)
    print("DIAGNOSTICS:")
    if PROXY_LIST:
        p = PROXY_LIST[0]
        parts = p.split('@')
        masked = parts[0].split(':')[0] + ':****@' + parts[1] if len(parts) == 2 else p[:20]
        print("  PROXY_LIST      : YES - " + str(len(PROXY_LIST)) + " proxies (" + masked + ")")
    else:
        print("  PROXY_LIST      : NO - page scraping uses direct connection (Bing search unaffected — Azure→Azure)")
    print("  Search engine   : Bing primary (Azure→Azure, no proxy) + DDG HTML via proxy fallback")
    print("  DDG regions     : " + str(DDG_REGIONS) + " (region-map logic only)")
    print("  Daily modifier  : " + get_daily_modifier())
    print("  Batch           : " + str(BATCH))
    print("  URL TTL         : " + str(URL_TTL_DAYS) + " days")
    print("=" * 60)

# ============================================
# URL TTL SYSTEM (Layer 1)
# ============================================

def load_visited_urls():
    if not os.path.exists(VISITED_URLS_FILE):
        return {}
    try:
        with open(VISITED_URLS_FILE, 'r') as f:
            data = json.load(f)
        # Migrate old list format → dict format
        if isinstance(data, list):
            old_date = (datetime.now() - timedelta(days=URL_TTL_DAYS)).strftime('%Y-%m-%d')
            print("  Migrating visited_urls to TTL format...")
            return {url: old_date for url in data}
        return data
    except Exception:
        return {}

def is_url_stale(visited_dict, url):
    """
    Returns True if URL was visited RECENTLY (within TTL) → should be SKIPPED.
    Returns False if never visited OR TTL has expired → safe to visit again.
    NOTE: 'stale' here means 'too fresh to revisit' — skip when True.
    """
    if url not in visited_dict:
        return False  # never visited — process it
    try:
        visited_date = datetime.strptime(visited_dict[url], '%Y-%m-%d')
        age_days = (datetime.now() - visited_date).days
        return age_days < URL_TTL_DAYS  # True = visited within 7 days = skip it
    except Exception:
        return False

def mark_visited(visited_dict, url):
    visited_dict[url] = datetime.now().strftime('%Y-%m-%d')

def save_visited_urls(visited_dict):
    try:
        with open(VISITED_URLS_FILE, 'w') as f:
            json.dump(visited_dict, f)
    except Exception:
        pass

def count_fresh_urls(visited_dict):
    """Count URLs eligible for revisiting (older than TTL)."""
    today = datetime.now()
    expired = 0
    for date_str in visited_dict.values():
        try:
            age = (today - datetime.strptime(date_str, '%Y-%m-%d')).days
            if age >= URL_TTL_DAYS:
                expired += 1
        except Exception:
            pass
    return expired

# ============================================
# MASTER EMAIL LIST
# ============================================

def load_master_emails():
    if not os.path.exists(MASTER_EMAILS_FILE):
        return set()
    try:
        with open(MASTER_EMAILS_FILE, 'r') as f:
            return set(line.strip() for line in f if line.strip())
    except Exception:
        return set()

EMAIL_LOG_FILE = "emails_log.txt"

def save_master_emails(new_emails):
    existing = load_master_emails()
    combined = existing | set(new_emails)
    truly_new = combined - existing  # emails added this call only
    try:
        with open(MASTER_EMAILS_FILE, 'w') as f:
            for email in sorted(combined):
                f.write(email + '\n')
    except Exception:
        pass

    # Append truly new emails to the date-separated log (keeps last 30 days only)
    if truly_new:
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            header = '===== ' + today + ' ====='

            # Read existing log, parse into sections keyed by date header
            sections = {}   # {header_line: [email lines]}
            order    = []   # insertion order of headers
            current  = None
            if os.path.exists(EMAIL_LOG_FILE):
                with open(EMAIL_LOG_FILE, 'r') as f:
                    for line in f:
                        line = line.rstrip('\n')
                        if line.startswith('=====') and line.endswith('====='):
                            current = line
                            if current not in sections:
                                sections[current] = []
                                order.append(current)
                        elif current and line:
                            sections[current].append(line)

            # Add today's new emails
            if header not in sections:
                sections[header] = []
                order.append(header)
            existing_today = set(sections[header])
            for email in sorted(truly_new):
                if email not in existing_today:
                    sections[header].append(email)

            # Trim to last 30 days
            if len(order) > 30:
                order = order[-30:]
                sections = {k: sections[k] for k in order if k in sections}

            # Rewrite log
            with open(EMAIL_LOG_FILE, 'w') as f:
                for hdr in order:
                    f.write('\n' + hdr + '\n')
                    for email in sections[hdr]:
                        f.write(email + '\n')
        except Exception:
            pass

    return len(combined) - len(existing)


# ============================================
# ADAPTIVE YIELD ENGINE
# Monitors daily email yield and auto-expands
# sources when a drop is detected.
# ============================================

def load_yield_tracker():
    if not os.path.exists(YIELD_TRACKER_FILE):
        return {'daily_yields': [], 'baseline': 0, 'expansion_level': 0}
    try:
        with open(YIELD_TRACKER_FILE) as f:
            return json.load(f)
    except Exception:
        return {'daily_yields': [], 'baseline': 0, 'expansion_level': 0}

def save_yield_tracker(tracker):
    try:
        with open(YIELD_TRACKER_FILE, 'w') as f:
            json.dump(tracker, f, indent=2)
    except Exception:
        pass

def record_batch_yield(new_emails):
    """Record how many NEW unique emails this batch found."""
    tracker = load_yield_tracker()
    today = datetime.now().strftime('%Y-%m-%d')

    # Accumulate daily total across all 6 batches
    if not tracker.get('today_date') or tracker['today_date'] != today:
        # New day — push yesterday's total into history
        if tracker.get('today_total', 0) > 0:
            tracker['daily_yields'].append(tracker['today_total'])
            tracker['daily_yields'] = tracker['daily_yields'][-30:]  # keep 30 days
        tracker['today_date'] = today
        tracker['today_total'] = 0

    tracker['today_total'] = tracker.get('today_total', 0) + new_emails

    # Set baseline from first 3 complete days
    if len(tracker['daily_yields']) == 3 and tracker.get('baseline', 0) == 0:
        tracker['baseline'] = sum(tracker['daily_yields']) / 3
        print("  YIELD BASELINE SET: " + str(int(tracker['baseline'])) + " emails/day")

    save_yield_tracker(tracker)
    return tracker

def get_expansion_level():
    """
    Compare recent 3-day average to baseline.
    Returns expansion level (0-3) needed.
    0 = normal, 1 = keyword expand, 2 = new platforms, 3 = full expansion + cache purge
    """
    tracker = load_yield_tracker()
    yields = tracker.get('daily_yields', [])
    baseline = tracker.get('baseline', 0)

    if len(yields) < 3 or baseline == 0:
        return 0  # not enough history yet

    recent_avg = sum(yields[-3:]) / 3
    drop = (baseline - recent_avg) / baseline

    current_level = tracker.get('expansion_level', 0)

    if drop >= DROP_L3 and current_level < 3:
        new_level = 3
    elif drop >= DROP_L2 and current_level < 2:
        new_level = 2
    elif drop >= DROP_L1 and current_level < 1:
        new_level = 1
    else:
        new_level = current_level

    if new_level > current_level:
        print("\n  ADAPTIVE ENGINE: yield dropped " + str(int(drop * 100)) +
              "% — triggering expansion level " + str(new_level))
        tracker['expansion_level'] = new_level
        save_yield_tracker(tracker)

    return new_level

def apply_expansion(level):
    """
    Level 1 (30% drop): Pull more keywords per day (+200)
    Level 2 (50% drop): Add Wattpad/Royal Road/Tumblr to dork queries
    Level 3 (70% drop): Purge oldest 40% of visited_urls cache + max dork volume
    Each level is cumulative — level 3 includes levels 1 and 2.
    """
    global KEYWORDS_PER_DAY

    if level >= 1:
        KEYWORDS_PER_DAY = min(KEYWORDS_PER_DAY + 200, 1000)
        print("  EXPAND L1: keywords → " + str(KEYWORDS_PER_DAY) + "/day")

    if level >= 2:
        print("  EXPAND L2: adding Wattpad / Royal Road / Tumblr to dork pool")
        # Injected into generate_dork_queries at runtime via module-level flag
        globals()['DORK_EXTRA_PLATFORMS'] = True

    if level >= 3:
        print("  EXPAND L3: purging oldest 40% of URL cache to force re-discovery")
        _purge_old_cache(keep_pct=0.60)

def _purge_old_cache(keep_pct=0.60):
    """Remove the oldest (keep_pct)% of visited_urls entries so pages get re-crawled."""
    visited = load_visited_urls()
    if not visited:
        return
    # Sort by date ascending (oldest first)
    sorted_urls = sorted(visited.items(), key=lambda x: x[1])
    keep_count = int(len(sorted_urls) * keep_pct)
    kept = dict(sorted_urls[len(sorted_urls) - keep_count:])
    save_visited_urls(kept)
    print("  Cache purged: " + str(len(visited) - len(kept)) + " old URLs removed → " +
          str(len(kept)) + " retained")


# ============================================
# EMAIL FINDING
# ============================================

def find_emails(text):
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(email_pattern, text)

    obfuscated = re.findall(
        r'[a-zA-Z0-9._%+-]+\s*[\[\(]?at[\]\)]?\s*[a-zA-Z0-9.-]+\s*[\[\(]?dot[\]\)]?\s*[a-zA-Z]{2,}',
        text, re.IGNORECASE
    )
    for match in obfuscated:
        cleaned = re.sub(r'\s*[\[\(]?at[\]\)]?\s*', '@', match, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s*[\[\(]?dot[\]\)]?\s*', '.', cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip()
        if '@' in cleaned:
            emails.append(cleaned)

    return emails

def clean_emails(email_list):
    email_list = list(set(email_list))

    blocked_local_exact = {
        'admin', 'webmaster', 'noreply', 'no-reply', 'donotreply',
        'support', 'help', 'info', 'contact', 'sales', 'marketing',
        'press', 'media', 'editor', 'editors', 'pr', 'ceo', 'cfo',
        'cto', 'founder', 'hello', 'team', 'staff', 'office'
    }

    blocked_domains_exact = {
        'example.com', 'test.com', 'sentry.io', 'amazonaws.com',
        'cloudflare.com', 'noreply.github.com', 'users.noreply.github.com'
    }

    clean_list = []
    for email in email_list:
        if '@' not in email:
            continue
        parts = email.lower().split('@')
        if len(parts) != 2:
            continue
        local, domain = parts[0], parts[1]
        if local in blocked_local_exact:
            continue
        if domain in blocked_domains_exact:
            continue
        clean_list.append(email)

    return clean_list

# ============================================
# DAILY MODIFIER (Layer 2)
# ============================================

def get_daily_modifier():
    return DAILY_MODIFIERS[datetime.now().weekday()]

# ============================================
# AUTO-KEYWORD GENERATOR (Layer 3)
# ============================================

# ============================================
# 1.04M COMBINATORIAL KEYWORD ENGINE
# Pool = KW_SUBGENRES × KW_ACTIVITIES × KW_MODIFIERS = 117 x 105 x 85 = 1,044,225
# No list stored — keywords computed from index on the fly
# At 750/day: ~3.8 years to exhaust the full pool
# ============================================

KW_SUBGENRES = [
    # Named scholarship programs + funding/degree-level types + destination-study angles
    "DAAD scholarship","Chevening scholarship","CSC scholarship","Turkiye Burslari scholarship",
    "Erasmus scholarship","Commonwealth scholarship","Fulbright scholarship","Mastercard Foundation scholarship",
    "MEXT scholarship","Gates Cambridge scholarship","Rhodes scholarship","Rotary scholarship",
    "UN scholarship","World Bank scholarship","Australia Awards scholarship","Vanier scholarship",
    "Fulbright Fellowship","fully funded scholarship","partial scholarship","tuition-free scholarship",
    "need-based scholarship","merit scholarship","undergraduate scholarship","masters scholarship",
    "PhD scholarship","postgraduate scholarship","doctoral scholarship","research scholarship",
    "exchange scholarship","study abroad scholarship","international scholarship","government scholarship",
    "university scholarship","faculty scholarship","engineering scholarship","medicine scholarship",
    "MBBS scholarship","law scholarship","business scholarship","MBA scholarship",
    "science scholarship","technology scholarship","agriculture scholarship","nursing scholarship",
    "public health scholarship","education scholarship","arts scholarship","humanities scholarship",
    "social sciences scholarship","study in Germany","study in France","study in UK",
    "study in Canada","study in USA","study in Australia","study in China",
    "study in Turkey","study in Norway","study in Finland","study in Sweden",
    "study in Belgium","study in Netherlands","tuition-free Europe","tuition-free Germany",
    "visa sponsorship","student visa","study permit","admission letter",
    "university admission","college admission","financial aid","grant funding",
    "bursary","stipend funding","living allowance scholarship","internship abroad",
    "volunteer abroad program","gap year program","summer school abroad","language school abroad",
    "foundation program abroad","pathway program abroad","exchange program","dual degree program",
    "joint degree program","distance learning scholarship","online degree scholarship","part-time scholarship",
    "sponsored scholarship","corporate scholarship","NGO scholarship","foundation scholarship",
    "diversity scholarship","women in STEM scholarship","African leaders scholarship","young leaders scholarship",
    "Mandela Washington Fellowship","YALI fellowship","Chevening Fellowship","Erasmus Mundus scholarship",
    "DAAD EPOS scholarship","DAAD In-Country scholarship","Chinese Government Scholarship","Turkish Government Scholarship",
    "MEXT Japan scholarship","Korean Government Scholarship","GKS scholarship","Study in Russia scholarship",
    "Study in India scholarship","ICCR scholarship","Study in Malaysia scholarship","Study in Singapore scholarship",
    "renewable energy scholarship","climate change scholarship","public policy scholarship","international relations scholarship",
    "development studies scholarship",
]  # 117 entries

KW_ACTIVITIES = [
    # How the audience self-identifies
    "scholarship applicant","scholarship seeker","scholarship hunter","final year student","final year undergraduate",
    "recent graduate","fresh graduate","African student","Nigerian student","Ghanaian student",
    "Kenyan student","South African student","Ugandan student","Cameroonian student","Senegalese student",
    "Ivorian student","Congolese student","Tanzanian student","Ethiopian student","Zimbabwean student",
    "Zambian student","Rwandan student","Moroccan student","Tunisian student","Algerian student",
    "Egyptian student","study abroad candidate","study abroad hopeful","study abroad applicant","international student hopeful",
    "undergraduate","undergraduate student","postgraduate applicant","postgraduate student","graduate student",
    "masters applicant","masters candidate","PhD applicant","PhD candidate","doctoral candidate",
    "diploma holder","HND holder","bachelor's degree holder","university graduate","college graduate",
    "campus leader","student leader","class representative","student union member","honor student",
    "dean's list student","top student","valedictorian","first-class graduate","second-class graduate",
    "distinction holder","merit scholar","academic scholar","research scholar","exchange student",
    "international student","overseas student","diaspora hopeful","first-generation student","first-generation graduate",
    "low-income student","underprivileged student","underrepresented student","rural student","community leader",
    "youth leader","student ambassador","campus ambassador","education seeker","funding seeker",
    "admission seeker","visa seeker","tuition-free seeker","fully-funded seeker","DAAD hopeful",
    "Chevening hopeful","CSC hopeful","Erasmus hopeful","Commonwealth hopeful","Fulbright hopeful",
    "Mastercard Foundation scholar","MEXT hopeful","Turkiye Burslari hopeful","career changer","working professional",
    "young professional","entry-level professional","aspiring professional","aspiring academic","aspiring researcher",
    "aspiring engineer","aspiring doctor","aspiring lawyer","aspiring entrepreneur","gap year student",
    "gap year graduate","self-funded student","family-funded student","working student","part-time student",
]  # 105 entries

KW_MODIFIERS = [
    # Geographic (source African countries + destination countries + major cities) + platform + year + context
    "Nigeria","Ghana","Kenya","South Africa","Uganda","Cameroon","Senegal","Ivory Coast",
    "DRC","Tanzania","Ethiopia","Zimbabwe","Zambia","Rwanda","Morocco","Tunisia",
    "Algeria","Egypt","Germany","France","UK","Canada","USA","Australia",
    "China","Turkey","Norway","Finland","Sweden","Belgium","Netherlands",
    "Lagos","Accra","Nairobi","Johannesburg","Cape Town","Kampala","Abuja","Kigali","Cairo","Casablanca",
    "blogspot","wordpress","reddit","facebook group","whatsapp group","telegram group","instagram","youtube","tiktok","quora","linkedin",
    "2025","2026","2027",
    "contact","email","gmail","yahoo","hotmail","apply","application","deadline",
    "requirements","eligibility","how to apply","join","subscribe","sign up","connect","reach out",
    "guide","tips","checklist","list","review","community","forum","group",
    "discussion","chat","network","consultation","admission letter","offer letter",
]  # 85 entries

# ── Combinatorial pool size ───────────────────────────────────────────
_KW_TOTAL = len(KW_SUBGENRES) * len(KW_ACTIVITIES) * len(KW_MODIFIERS)

def _index_to_keyword(idx):
    """Convert flat index → 3-component keyword string. Zero memory, instant."""
    mod_i = idx % len(KW_MODIFIERS)
    act_i = (idx // len(KW_MODIFIERS)) % len(KW_ACTIVITIES)
    sub_i = idx // (len(KW_MODIFIERS) * len(KW_ACTIVITIES))
    return KW_SUBGENRES[sub_i] + ' ' + KW_ACTIVITIES[act_i] + ' ' + KW_MODIFIERS[mod_i]

def get_daily_keywords():
    """
    Draw KEYWORDS_PER_DAY keywords from the 1.04M combinatorial space.
    Date-seeded: same date = same full set. Different every day for ~3.8 years.
    Zero memory footprint — each keyword is computed from its index.
    In GitHub Actions: each batch gets its own 1/6 non-overlapping slice so
    all 6 batches cover different keywords — no duplicate work across the day.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    seed  = int(hashlib.md5(today.encode()).hexdigest(), 16)
    rng   = random.Random(seed)

    n = min(KEYWORDS_PER_DAY, _KW_TOTAL)
    indices  = rng.sample(range(_KW_TOTAL), n)
    keywords = [_index_to_keyword(i) for i in indices]

    # Slice into batch-specific segment — each of 6 batches gets ~84 unique keywords
    if IS_GITHUB_ACTIONS and BATCH > 0:
        batch_size = max(1, n // 6)
        start = (BATCH - 1) * batch_size
        end   = start + batch_size if BATCH < 6 else n  # Batch 6 gets remainder
        keywords = keywords[start:end]
        print("  Keyword pool    : {:,} combinatorial ({} × {} × {})".format(
            _KW_TOTAL, len(KW_SUBGENRES), len(KW_ACTIVITIES), len(KW_MODIFIERS)))
        print("  Batch {}/6 slice : keywords {:,}–{:,} ({} unique keywords this run)".format(
            BATCH, start + 1, end, len(keywords)))
    else:
        print("  Keyword pool    : {:,} combinatorial ({} × {} × {})".format(
            _KW_TOTAL, len(KW_SUBGENRES), len(KW_ACTIVITIES), len(KW_MODIFIERS)))
        print("  Selected today  : {} (date-seeded, exhausted in {:,} days)".format(
            n, _KW_TOTAL // n))

    return keywords


# ============================================
# SEARCH (multi-region + modifier + blogs)
# ============================================

def _ddgs_lite_search(query, max_results=10):
    """
    DDG Lite endpoint via proxy — hard fallback when Bing scrape fails.
    backend='lite' hits lite.duckduckgo.com — less rate-limited than html endpoint.
    Hard 10s timeout via ThreadPoolExecutor so a hung DDGS call can't block the engine.
    """
    if not PROXY_LIST:
        return [], []
    proxy = get_next_proxy()
    if not proxy:
        return [], []
    def _run():
        with DDGS(proxy=proxy, verify=False) as ddgs_client:
            return list(ddgs_client.text(query, max_results=max_results, backend="lite"))
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_run)
            results = future.result(timeout=20)  # 20s — free proxies are slow; was 10s
        urls = []
        snippet_emails = []
        for r in results:
            url = r.get('href', '')
            if url:
                urls.append(url)
            snippet = r.get('body', '') + ' ' + r.get('title', '')
            snippet_emails.extend(find_emails(snippet))
        return urls, snippet_emails
    except Exception as e:
        err = str(e)[:60]
        _PROXY_CONN_ERRORS = ('ProxyError', 'ConnectionError', 'Cannot connect', '407',
                               'Tunnel connection failed', 'RemoteDisconnected')
        if any(x in err for x in _PROXY_CONN_ERRORS):
            _evict_proxy(proxy)
        return [], []


def _bing_search(query, max_results=10):
    """
    Bing HTML scraper — primary search engine, tried before DDGS Lite fallback.
    Bing (Microsoft) is hosted on Azure — GitHub Actions Azure IPs are NOT blocked.
    Proxy attempted first; direct connection used if proxy fails (Azure→Azure = clean).
    """
    headers = {
        'User-Agent': get_random_user_agent(),
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    params = {'q': query, 'count': min(max_results, 50), 'setmkt': 'en-US', 'setlang': 'en'}

    def _parse_bing(html):
        soup = BeautifulSoup(html, 'html.parser')
        urls = []
        snippet_emails = []
        for li in soup.select('li.b_algo'):
            a = li.select_one('h2 a')
            href = a.get('href', '') if a else ''
            # Bing often returns bing.com/ck/ tracking redirects — use cite tag instead
            if 'bing.com' in href or not href.startswith('http'):
                cite = li.select_one('cite')
                if cite:
                    raw = cite.get_text(' ', strip=True).split(' ')[0].strip()
                    if raw and '.' in raw:
                        href = raw if raw.startswith('http') else 'https://' + raw.lstrip('/')
                    else:
                        href = ''
            if href and href.startswith('http') and 'bing.com' not in href:
                urls.append(href)
            snippet_emails.extend(find_emails(li.get_text(' ', strip=True)))
        return urls[:max_results], snippet_emails

    # Try with proxy first
    proxy = get_next_proxy()
    if proxy:
        try:
            r = requests.get('https://www.bing.com/search', params=params, headers=headers,
                             proxies={'http': proxy, 'https': proxy}, timeout=8, verify=False)
            urls, emails = _parse_bing(r.text)
            if urls or emails:
                return urls, emails
        except Exception:
            pass

    # Direct connection — Azure→Azure, Bing does not block GitHub Actions IPs
    try:
        r = requests.get('https://www.bing.com/search', params=params, headers=headers,
                         timeout=8, verify=False)
        return _parse_bing(r.text)
    except Exception:
        return [], []



def ddg_search(query, region, num_results, retry):
    """
    Direct DDGS call — proxy-based fallback after Bing.
    backend='html' is the most reliable for proxy IPs.
    Wrapped in 20s ThreadPoolExecutor so a hung call never blocks the engine.
    """
    if not PROXY_LIST:
        return [], []
    proxy = get_next_proxy()
    if not proxy:
        return [], []
    def _run():
        with DDGS(proxy=proxy, verify=False) as d:
            return list(d.text(query, region=region, max_results=max(20, num_results), backend="html"))
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            results = ex.submit(_run).result(timeout=20)
        urls = [r.get('href', '') for r in results if r.get('href', '')]
        snippet_emails = []
        for r in results:
            snippet_emails.extend(find_emails(r.get('body', '') + ' ' + r.get('title', '')))
        if snippet_emails:
            for e in snippet_emails:
                print("  SNIPPET HIT: " + e)
        return urls, snippet_emails
    except Exception as e:
        err = str(e)[:60]
        _PROXY_CONN_ERRORS = ('ProxyError', 'ConnectionError', 'Cannot connect', '407',
                               'Tunnel connection failed', 'RemoteDisconnected')
        if any(x in err for x in _PROXY_CONN_ERRORS):
            _evict_proxy(proxy)
        return [], []


def search_google(keyword, num_results=10, retry=3):
    """
    Two-query search per keyword.
    Path: Bing (Azure->Azure, direct, no proxy needed) → DDG HTML via proxy.
    """
    print("  Searching: " + keyword)
    all_results = []
    all_snippet_emails = []
    seen = set()
    seen_emails = set()

    def _merge(urls, emails):
        for url in urls:
            if url not in seen:
                seen.add(url)
                all_results.append(url)
        for e in emails:
            if e not in seen_emails:
                seen_emails.add(e)
                all_snippet_emails.append(e)

    # Call 1: general keyword — Bing first (Azure→Azure clean path), DDG fallback
    urls, snip = _bing_search(keyword, num_results)
    if not urls and not snip:
        urls, snip = ddg_search(keyword, 'us-en', num_results, retry)
    _merge(urls, snip)
    time.sleep(random.uniform(0.3, 0.5))

    # Call 2: blog-specific — targets personal reader blogs
    blog_query = keyword + ' blog site:blogspot.com OR site:wordpress.com'
    urls, snip = _bing_search(blog_query, num_results)
    if not urls and not snip:
        urls, snip = ddg_search(blog_query, 'us-en', num_results, retry)
    _merge(urls, snip)
    time.sleep(random.uniform(0.3, 0.5))

    if len(all_results) == 0 and len(all_snippet_emails) == 0:
        print("  WARNING: 0 results for this keyword")
    else:
        print("  Found " + str(len(all_results)) + " URLs, " + str(len(all_snippet_emails)) + " snippet emails")
    return all_results, all_snippet_emails

# ============================================
# EMAIL DORK ENGINE (Layer 5)
# ============================================

def generate_dork_queries():
    """
    Tiered email dork queries ranked by likelihood of email appearing in DDG snippet.
    Adapted for the Scholarship & Visa Services niche — every query below targets
    scholarship/study-abroad applicants instead of romance readers.

    TIER 1: Applicant intentionally posted their email (essay swap, SOP review, study group, contact)
    TIER 2: Personal blog contact sections (blogspot/wordpress)
    TIER 3: Country TLD site: filters (source African countries + destination countries)
    TIER 4: Named-scholarship-specific queries (DAAD, Chevening, CSC, etc.)
    """

    # TIER 1: Applicant explicitly shared their email for a purpose
    tier1 = [
        # Essay / SOP review swap
        '"gmail.com" "SOP review" scholarship',
        '"yahoo.com" "SOP review" scholarship',
        '"gmail.com" "essay review" "scholarship applicant"',
        '"@gmail.com" "statement of purpose" review',
        # Study group / study buddy
        '"gmail.com" "scholarship study group"',
        '"gmail.com" "study abroad" "study buddy"',
        '"@gmail.com" "scholarship applicants" group',
        '"gmail.com" "scholarship" "study group" contact',
        # "Email me" invitations
        '"email me" "scholarship applicant" "gmail.com"',
        '"contact me" "study abroad" "gmail.com"',
        '"email me" "scholarship hopeful" "gmail.com"',
        '"reach me" "scholarship applicant" "gmail.com"',
        '"email me at" "scholarship" "gmail.com"',
        '"email me" "study abroad" "yahoo.com"',
        # Organized applicant activities
        '"gmail.com" "scholarship application group"',
        '"gmail.com" "scholarship whatsapp group" contact',
        '"gmail.com" "scholarship telegram group"',
        '"gmail.com" "study abroad group" "contact"',
        '"gmail.com" "scholarship mentorship" contact',
        '"gmail.com" "scholarship cohort" contact',
    ]

    # TIER 2: Personal blogs — highest email surface area
    tier2 = [
        '"gmail.com" "scholarship applicant" site:blogspot.com',
        '"gmail.com" "study abroad" site:blogspot.com',
        '"gmail.com" "scholarship journey" site:blogspot.com',
        '"gmail.com" "my scholarship journey" site:blogspot.com',
        '"gmail.com" "scholarship hopeful" site:blogspot.com',
        '"@gmail.com" "scholarship applicant" site:blogspot.com',
        '"@gmail.com" "study abroad" site:blogspot.com',
        '"@yahoo.com" "scholarship applicant" site:blogspot.com',
        '"gmail.com" "scholarship applicant" site:wordpress.com',
        '"gmail.com" "study abroad" site:wordpress.com',
        '"gmail.com" "scholarship journey" site:wordpress.com',
        '"@gmail.com" "scholarship applicants" site:wordpress.com',
        '"yahoo.com" "study abroad" site:wordpress.com',
        '"yahoo.com" "scholarship applicant" site:blogspot.com',
        '"hotmail.com" "scholarship applicant" site:blogspot.com',
        '"outlook.com" "scholarship applicant" site:blogspot.com',
    ]

    # TIER 3: Country TLD — skips retailer/publisher domains, targets source + destination countries
    tier3 = [
        '"gmail.com" "scholarship applicants" site:co.uk',
        '"hotmail.co.uk" "scholarship applicants"',
        '"hotmail.co.uk" "study abroad"',
        '"@gmail.com" "scholarship applicants" site:co.uk',
        '"gmail.com" "scholarship applicants" site:com.ng',
        '"gmail.com" "study abroad" site:com.ng',
        '"@gmail.com" "scholarship applicants" site:com.ng',
        '"gmail.com" "scholarship applicants" site:co.za',
        '"gmail.com" "study abroad" site:co.za',
        '"gmail.com" "scholarship applicants" site:co.ke',
        '"gmail.com" "study abroad" site:co.ke',
        '"gmail.com" "scholarship applicants" site:com.gh',
        '"gmail.com" "scholarship applicants" site:com.au',
        '"gmail.com" "study abroad" site:com.au',
        '"gmail.com" "scholarship applicants" site:co.nz',
        '"gmail.com" "scholarship applicants" site:ca',
        '"gmail.com" "study abroad" site:ca',
        '"gmail.com" "scholarship applicants" site:ie',
        '"gmail.com" "scholarship applicants" site:ph',
    ]

    # TIER 4: Named-scholarship-specific (high hit rate — very targeted)
    named_scholarships = [
        'DAAD scholarship', 'Chevening scholarship', 'CSC scholarship',
        'Erasmus scholarship', 'Commonwealth scholarship', 'Fulbright scholarship',
        'Mastercard Foundation scholarship', 'MEXT scholarship', 'Turkiye Burslari scholarship',
        'Gates Cambridge scholarship',
    ]
    tier4 = []
    for sch in named_scholarships:
        tier4.append('"gmail.com" "' + sch + ' applicant"')
        tier4.append('"gmail.com" "' + sch + ' hopeful"')
        tier4.append('"gmail.com" "' + sch + '" "how to apply"')
        tier4.append('"gmail.com" "' + sch + ' applicants" site:blogspot.com')
        tier4.append('"gmail.com" "' + sch + ' applicants" site:wordpress.com')
        tier4.append('"@gmail.com" "' + sch + ' applicants"')
        tier4.append('"email me" "' + sch + '" "gmail.com"')

    # TIER 5: Extended platforms (snippet extraction from communities)
    tier5 = [
        '"gmail.com" "scholarship" site:forum.thegradcafe.com',
        '"yahoo.com" "scholarship" site:forum.thegradcafe.com',
        '"gmail.com" "scholarship applicants" site:quora.com',
        '"gmail.com" "study abroad" site:quora.com',
        '"gmail.com" "scholarship applicant" site:tumblr.com',
        '"gmail.com" "study abroad" site:tumblr.com',
        '"@gmail.com" "scholarship applicant" site:tumblr.com',
        '"gmail.com" "scholarship" "study group" site:reddit.com',
        '"gmail.com" "study abroad" site:reddit.com',
        '"@gmail.com" "scholarship" site:reddit.com',
        '"gmail.com" "scholarship applicants" site:facebook.com',
        '"@gmail.com" "scholarship applicants group" site:facebook.com',
        '"gmail.com" "study abroad group" site:facebook.com',
        '"gmail.com" "contact for essay review" scholarship',
        '"gmail.com" "email for SOP review" scholarship',
        '"gmail.com" "contact to join" "scholarship applicants"',
        '"gmail.com" "email to join" "study abroad group"',
        '"yahoo.com" "contact for essay review" scholarship',
        '"gmail.com" "scholarship newsletter" "subscribe"',
        '"@gmail.com" "scholarship newsletter"',
    ]

    # TIER 6: Country keyword matrix — broad sweep
    tier6 = []
    providers = ['gmail.com', 'yahoo.com', 'hotmail.com']
    applicant_terms = ['"scholarship applicants"', '"scholarship hopefuls"', '"study abroad applicants"']
    countries = [
        'Nigeria', '"South Africa"', 'Kenya', 'Ghana',
        'Uganda', 'Cameroon', 'Senegal', 'Ethiopia',
        'Tanzania', 'Zambia', 'Rwanda', 'Zimbabwe',
    ]
    for p in providers:
        for t in applicant_terms:
            for c in countries:
                tier6.append('"' + p + '" ' + t + ' ' + c)

    # TIER 7 (adaptive): injected when yield drops 50%+ (expansion level 2+)
    tier7 = []
    if globals().get('DORK_EXTRA_PLATFORMS'):
        extra_platforms = [
            'site:t.me', 'site:whatsapp.com',
            'site:telegram.me', 'site:medium.com',
        ]
        for site in extra_platforms:
            tier7.append('"gmail.com" "scholarship applicant" ' + site)
            tier7.append('"gmail.com" "study abroad group" ' + site)
            tier7.append('"@gmail.com" "scholarship" ' + site)
            tier7.append('"yahoo.com" "scholarship applicant" ' + site)
        print("  DORK TIER 7 active: " + str(len(tier7)) + " extra-platform queries added")

    # TIER 8: Fresh angles — patterns never run before, different surface area than tiers 1-6
    tier8 = []

    # 8A: Natural language email disclosure — applicants writing their email naturally in text
    tier8 += [
        '"my email is" "scholarship" "gmail.com"',
        '"my email is" "scholarship applicant" gmail',
        '"reach me at" "scholarship" "gmail.com"',
        '"you can email me at" scholarship applicant',
        '"email me at" "study abroad group" gmail',
        '"drop me an email" "scholarship applicant" gmail',
        '"shoot me an email" scholarship gmail.com',
        '"feel free to email" scholarship applicant gmail',
        '"get in touch" "scholarship applicant" gmail.com',
        '"send me an email" "scholarship" "gmail.com"',
    ]

    # 8B: Ambassador / mentorship / alumni-network sign-up pages
    tier8 += [
        '"scholarship ambassador" "gmail.com"',
        '"join our mentorship program" scholarship gmail',
        '"student ambassador" scholarship "gmail.com"',
        '"mentee sign up" scholarship gmail.com',
        '"apply to be a mentee" scholarship "gmail.com" apply',
        '"alumni network" scholarship apply "gmail.com"',
        '"mentorship sign up" scholarship "gmail.com"',
        '"join the network" scholarship applicant gmail',
        '"applicant group" scholarship author "gmail.com"',
        '"cohort sign up" scholarship "gmail.com"',
    ]

    # 8C: Scholarship group join / application language — different from "group contact"
    tier8 += [
        '"join our scholarship group" "gmail.com"',
        '"scholarship applicants" "sign up" "gmail.com"',
        '"group application" scholarship gmail',
        '"group membership" scholarship gmail',
        '"study abroad group" "join" gmail',
        '"group sign up" scholarship gmail.com',
        '"whatsapp group" scholarship "gmail.com"',
        '"telegram group" scholarship "gmail.com" join',
    ]

    # 8D: New platforms not in existing tiers
    tier8 += [
        '"gmail.com" "scholarship applicant" site:substack.com',
        '"gmail.com" "scholarship" site:medium.com',
        '"gmail.com" "scholarship applicant" site:livejournal.com',
        '"gmail.com" "scholarship" site:proboards.com',
        '"gmail.com" "scholarship applicants" site:forumotion.com',
        '"gmail.com" "scholarship group" site:wixsite.com',
        '"gmail.com" "scholarship applicant" site:weebly.com',
        '"gmail.com" "scholarship" site:tapatalk.com',
    ]

    # 8E: New email providers — outlook, icloud, ymail barely used in current tiers
    tier8 += [
        '"outlook.com" "scholarship applicant" site:blogspot.com',
        '"outlook.com" "scholarship group" site:wordpress.com',
        '"outlook.com" "scholarship applicants" "email me"',
        '"outlook.com" "scholarship" "essay review"',
        '"icloud.com" "scholarship applicant"',
        '"icloud.com" "scholarship group"',
        '"ymail.com" "scholarship applicant"',
        '"ymail.com" "scholarship group"',
        '"protonmail.com" "scholarship applicant"',
    ]

    # 8F: Geographic expansion — countries not in tier 6 (English-reading African populations)
    tier8 += [
        '"gmail.com" "scholarship applicants" Morocco',
        '"gmail.com" "scholarship applicant" Tunisia',
        '"gmail.com" "study abroad group" Algeria',
        '"gmail.com" "scholarship applicants" Egypt',
        '"gmail.com" "scholarship applicants" "Ivory Coast"',
        '"gmail.com" "study abroad group" "DRC"',
        '"gmail.com" "scholarship applicant" "Sierra Leone"',
        '"gmail.com" "scholarship applicants" Botswana',
        '"gmail.com" "scholarship applicants" site:com.pk',
        '"gmail.com" "scholarship applicants" site:co.tz',
        '"gmail.com" "scholarship applicant" "East Africa"',
        '"gmail.com" "scholarship applicants" "West Africa"',
    ]

    # 8G: Application deadline, funding, essay-help — action-specific language
    tier8 += [
        '"scholarship deadline" "gmail.com" 2026',
        '"scholarship applicants" "email" gmail',
        '"essay help" scholarship "contact" "gmail.com"',
        '"SOP help" scholarship "gmail.com"',
        '"scholarship essay review" gmail contact',
        '"study group" "scholarship" "outlook.com"',
        '"scholarship" "funding" "email" "gmail.com"',
    ]

    # 8H: Application/review request variants with different phrasing
    tier8 += [
        '"essay reviewer" scholarship "gmail.com"',
        '"review request" scholarship "outlook.com"',
        '"SOP request" scholarship "gmail.com" blog',
        '"advance reader" scholarship "gmail.com" contact',
        '"request an essay review" scholarship gmail',
        '"contact for SOP review" scholarship "outlook.com"',
        '"early applicant" scholarship "gmail.com"',
    ]

    # TIER 9: Campus/student + platform angles — zero saturation, never scraped before
    tier9 = []

    # 9A: University/campus scholarship groups — dense email source, completely untapped
    tier9 += [
        '"scholarship applicants" site:.edu.ng',
        '"scholarship group" site:.edu.ng "gmail.com"',
        '"scholarship" "study group" site:.edu.ng email',
        '"study abroad" "campus group" site:.edu.ng contact',
        '"scholarship" "campus group" site:.edu.ng "sign up"',
        '"campus scholarship group" gmail.com',
        '"university" "scholarship applicants" "gmail.com"',
        '"campus applicant group" scholarship gmail',
        '"scholarship" "study group" "university" "gmail.com"',
        '"scholarship" site:.edu.ng "gmail.com" "applicant group"',
    ]

    # 9B: NYSC / recent-graduate personal emails on scholarship communities
    tier9 += [
        '"corper" "scholarship applicant" "gmail.com"',
        '"NYSC" "scholarship" "gmail.com"',
        '"corper" "essay reviewer" "scholarship" gmail',
        '"student blogger" "scholarship" "gmail.com"',
        '"recent graduate" "scholarship applicants" "contact" "gmail.com"',
        '"fresh graduate" "scholarship applicant" gmail',
        '"NYSC corper" "scholarship group" email gmail',
    ]

    # 9C: Essay/SOP reviewer platform dorks — people sharing reviewer profiles publicly
    tier9 += [
        '"essay reviewer" "scholarship" "contact me" "gmail.com"',
        '"SOP reviewer" "scholarship" "apply" "gmail.com" 2026',
        '"scholarship essay reviewer" "gmail.com" blog',
        '"i review scholarship essays" "gmail.com"',
        '"scholarship essay reviewer" "contact me" gmail',
        '"looking for essay reviewers" scholarship gmail.com',
        '"request a review" "scholarship essay" gmail.com',
        '"scholarship" "review team" apply gmail.com',
    ]

    # 9D: Hiring/recruitment-style pages for essay reviewers, mentors, consultants
    tier9 += [
        '"sensitivity reader" "scholarship essay" apply gmail.com',
        '"scholarship" "paid reviewer" gmail.com contact',
        '"freelance" "scholarship consultant" "gmail.com"',
        '"scholarship" "essay editor" "paid" gmail.com',
        '"scholarship mentor" "paid" "gmail.com"',
        '"scholarship" "mentorship" "compensation" gmail',
    ]

    # 9E: Geographic expansion — untouched English-reading markets
    tier9 += [
        '"gmail.com" "scholarship applicants" "Trinidad and Tobago"',
        '"gmail.com" "scholarship group" "Guyana"',
        '"gmail.com" "scholarship applicants" "Jamaica"',
        '"gmail.com" "scholarship group" "Belize"',
        '"gmail.com" "scholarship applicants" "Papua New Guinea"',
        '"gmail.com" "scholarship group" "Namibia"',
        '"gmail.com" "scholarship applicants" "Malawi"',
        '"gmail.com" "scholarship applicants" "Liberia"',
    ]

    # TIER 10: Recent-graduate / young-professional communities — high scholarship-applicant overlap
    # These demographics (NYSC corpers, recent grads, young professionals) are the exact
    # audience the Marketing Blueprint's Budget Seeker + High Achiever personas target.
    tier10 = [
        # NYSC / recent graduates (Nigeria) — nyscblog.com is indexed and has public comment threads
        '"gmail.com" site:nyscblog.com "scholarship" OR "study abroad"',
        '"@gmail.com" site:nyscblog.com "scholarship" OR "masters"',
        '"gmail.com" "corper" "scholarship applicant" "contact"',
        '"gmail.com" "NYSC" "study abroad" "email me"',
        '"yahoo.com" "corper" "scholarship applicant"',
        # Recent graduates generally
        '"gmail.com" "recent graduate" "scholarship applicant" "contact"',
        '"@gmail.com" site:afterschoolafrica.com "scholarship" comment',
        '"gmail.com" "fresh graduate" "scholarship" "email"',
        '"gmail.com" "final year student" "scholarship applicant"',
        # Young professionals
        '"gmail.com" "young professional" "scholarship applicant"',
        '"gmail.com" "career changer" "scholarship" "contact"',
        # Teachers / educators pursuing further study
        '"gmail.com" "teacher" "scholarship applicant" "contact"',
        '"gmail.com" "educator" "study abroad" "email"',
        # Engineers / STEM graduates (frequent scholarship demographic)
        '"gmail.com" "engineering graduate" "scholarship applicant"',
        '"gmail.com" "STEM graduate" "scholarship" "contact"',
        # Nurses / healthcare workers pursuing scholarships abroad
        '"gmail.com" "nurse" "scholarship applicant" "contact"',
        '"gmail.com" "healthcare worker" "study abroad" "contact"',
        # HR / admin / EA professionals
        '"gmail.com" "HR professional" "scholarship applicant"',
        '"gmail.com" "executive assistant" "scholarship" "study abroad"',
        # Student union / campus leaders
        '"gmail.com" "student union" "scholarship applicant"',
        '"gmail.com" "campus leader" "scholarship" "contact"',
        # General shift-worker/working-student angle
        '"gmail.com" "working student" "scholarship applicant" "contact"',
        '"gmail.com" "part-time student" "scholarship" "contact"',
        '"gmail.com" "self-funded student" "scholarship applicant"',
    ]

    # Ordered dedup: tier1 first = highest yield always runs in earliest batch
    ordered = tier1 + tier2 + tier3 + tier4 + tier5 + tier6 + tier7 + tier8 + tier9 + tier10
    seen = set()
    deduped = []
    for q in ordered:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped


def dork_search(batch_dork_queries):
    """
    Search DDG with email dork queries.
    Extracts emails from snippets directly — no page visit needed.
    Falls back to visiting the page only when snippet has no email.
    """
    print("\n--- Email Dork Engine running (DDG via proxy + Bing fallback) ---")
    print("  Dork queries this batch: " + str(len(batch_dork_queries)))
    # Hard cap: dork engine may never consume more than 10 min of the 82-min budget
    # Keywords are the main email source — dork must not starve them
    _DORK_DEADLINE = time.time() + (10 * 60)

    direct_emails = []
    fallback_urls = []
    seen_emails = set()
    seen_urls = set()

    # Map query content to best DDG region
    DORK_REGION_MAP = {
        'site:co.uk': 'uk-en',   'hotmail.co.uk': 'uk-en',
        'site:com.au': 'au-en',  'Australia': 'au-en',
        'site:co.nz': 'nz-en',   'New Zealand': 'nz-en',
        'site:ca': 'ca-en',      'Canada': 'ca-en',
        'site:ie': 'ie-en',      'Ireland': 'ie-en',
        'site:com.ng': 'wt-wt',  'Nigeria': 'wt-wt',
        'site:co.za': 'wt-wt',   'South Africa': 'wt-wt',
        'site:co.ke': 'wt-wt',   'Kenya': 'wt-wt',
        'site:com.gh': 'wt-wt',  'Ghana': 'wt-wt',
        'site:ph': 'wt-wt',      'Philippines': 'wt-wt',
        'India': 'wt-wt',        'Jamaica': 'wt-wt',
        'Uganda': 'wt-wt',
    }
    _dork_region_cycle = ['us-en', 'uk-en', 'au-en', 'ca-en', 'wt-wt']
    _dork_ridx = [0]

    def pick_dork_region(q):
        for key, reg in DORK_REGION_MAP.items():
            if key in q:
                return reg
        r = _dork_region_cycle[_dork_ridx[0] % len(_dork_region_cycle)]
        _dork_ridx[0] += 1
        return r

    def run_dork_query(query, region):
        """Run one dork query via DDG (region-aware) with Bing fallback — restored to original working path."""
        emails_found = []
        urls_found = []
        try:
            # Primary: DDG via proxy with region — original path that produced DORK HITs
            urls, snip_emails = ddg_search(query, region, 30, 1)
            # Fallback: Bing direct if DDG returned nothing
            if not urls and not snip_emails:
                urls, snip_emails = _bing_search(query, max_results=30)
            for e in snip_emails:
                if e not in seen_emails:
                    seen_emails.add(e)
                    emails_found.append(e)
                    print("  DORK HIT: " + e + " (" + region + ")")
            for url in urls:
                if url and url not in seen_urls and is_reader_website(url) and not snip_emails:
                    if len(urls_found) < 5:
                        seen_urls.add(url)
                        urls_found.append(url)
            if not urls and not snip_emails:
                print("  Dork error (" + region + "): No results found.")
            time.sleep(random.uniform(0.5, 1))
        except Exception as e:
            err = str(e)[:80]
            print("  Dork error (" + region + "): " + err)
            time.sleep(random.uniform(1, 2))
        return emails_found, urls_found

    for idx, query in enumerate(batch_dork_queries):
        if _out_of_time() or time.time() >= _DORK_DEADLINE:
            print("  Dork engine capped at 10 min — " + str(idx + 1) + "/" + str(len(batch_dork_queries)) + " queries done — handing time to keywords")
            break

        # Single call per query — region-mapped via pick_dork_region()
        primary = pick_dork_region(query)
        em, ur = run_dork_query(query, primary)
        direct_emails.extend(em)
        fallback_urls.extend(ur)

        if (idx + 1) % 10 == 0:
            print("  Dork progress: " + str(idx + 1) + "/" + str(len(batch_dork_queries)) + " queries, " + str(len(direct_emails)) + " emails found")

    print("  Dork direct emails   : " + str(len(direct_emails)))
    print("  Dork fallback URLs   : " + str(len(fallback_urls)))
    return direct_emails, fallback_urls


# ============================================
# BLOG DIRECTORY SCRAPING
# ============================================

def scrape_blog_directories(directories=None):
    if directories is None:
        directories = BLOG_DIRECTORIES
    print("\n--- Scraping blog directories (" + str(len(directories)) + " sources) ---")
    found_urls = []
    seen = set()
    headers = {'User-Agent': get_random_user_agent()}

    skip_domains = [
        'feedspot.com', 'alltop.com', 'google.com', 'facebook.com',
        'twitter.com', 'instagram.com', 'youtube.com', 'pinterest.com',
        'amazon.com', 'goodreads.com', 'linkedin.com', 'reddit.com',
        'tiktok.com', 'tumblr.com',
    ]

    for directory_url in directories:
        try:
            proxy = get_next_proxy()
            proxies = {"http": proxy, "https": proxy} if proxy else None
            try:
                response = requests.get(directory_url, headers=headers, proxies=proxies, timeout=6, verify=False)
            except Exception:
                response = requests.get(directory_url, headers=headers, timeout=6, verify=False)
            soup = BeautifulSoup(response.text, 'html.parser')

            for tag in soup.find_all('a', href=True):
                href = tag['href']
                if not href.startswith('http'):
                    continue
                skip = any(d in href for d in skip_domains)
                if skip:
                    continue
                if href not in seen:
                    seen.add(href)
                    found_urls.append(href)

            print("  " + directory_url[:55] + " -> " + str(len(found_urls)) + " URLs")
            time.sleep(random.uniform(2, 3))
        except Exception as e:
            print("  Directory error: " + str(e)[:50])

    print("  Blog directories total: " + str(len(found_urls)) + " unique URLs")
    return found_urls

# ============================================
# URL FILTER
# ============================================

# Personal blog domains — only source type that reliably has exposed reader emails
ALLOWED_DOMAINS = [
    'blogspot.com', 'wordpress.com', 'wixsite.com', 'weebly.com',
    'tumblr.com', 'squarespace.com', 'ghost.io', 'substack.com',
    'medium.com', 'typepad.com', 'blogger.com',
]

# Hard block — never visit regardless
BLOCKED_DOMAINS = [
    # Publishers / retailers (amazon.co. catches .co.uk, .co.jp, .co.au etc.)
    'amazon.com', 'amazon.co.', 'amzn.', 'barnesandnoble.com', 'harlequin.com', 'bookshop.org',
    'penguinrandomhouse.com', 'simonandschuster.com', 'macmillan.com',
    'targetbooks', 'walmart.com', 'ebay.com', 'etsy.com',
    'nextchapterbooksellers', 'thirdplacebooks', 'powells.com',
    'indiebound.org', 'booksamillion.com', 'chapters.indigo.ca',
    # Commercial book platforms (goodreads/reddit NOT blocked — community paths allowed via scraper)
    'bookbub.com', 'overdrive.com', 'libby.com',
    'scribd.com', 'wattpad.com', 'royalroad.com', 'webnovel.com',
    'netgalley.com', 'edelweiss', 'library',
    # Block goodreads book/author pages — allow group/topic/user via scraper
    'goodreads.com/book/', 'goodreads.com/work/', 'goodreads.com/author/',
    'goodreads.com/shelf/', 'goodreads.com/list/', 'goodreads.com/series/',
    'goodreads.com/quotes/', 'goodreads.com/review/',
    # Block reddit listing pages — allow specific post/comment pages via scraper
    'reddit.com/r/romance/new', 'reddit.com/r/romance/hot',
    # Commercial club/event platforms
    'meetup.com', 'eventbrite.com', 'bookclubs.com', 'bookclubz.com',
    'literati.com', 'reese', 'swell', 'libro.fm',
    # Social media
    'facebook.com', 'instagram.com', 'twitter.com', 'tiktok.com',
    'youtube.com', 'pinterest.com', 'linkedin.com', 'snapchat.com',
    'discord.com', 'telegram.org',
    # News / media
    'forbes.com', 'buzzfeed.com', 'huffpost.com', 'theguardian.com',
    'nytimes.com', 'washingtonpost.com', 'bbc.com', 'cnn.com',
    'publishersweekly', 'writersdigest', 'literaryagency',
    'nielsen.com', 'statista.com',
    # Education / college institutional sites (NOT student blogs)
    'nces.ed.gov', 'commonapp.org', 'usnews.com', 'collegeboard.org',
    'collegenavigator', 'cappex.com', 'petersons.com',
    # Job / career sites
    'indeed.com', 'glassdoor.com', 'care.com', 'sittercity.com',
    # Reference / wiki
    'wikipedia.org', 'wikihow.com', 'britannica.com',
    # URL patterns
    '/images/', '/reel/', '/video/', '/watch?', '/tag/', '/category/',
    '/page/', '/search?', '/topics/', '/lists/', '/product/', '/shop/',
    '/dp/', '/gp/', '/store/', '/item/', '/listing/',
]

def is_reader_website(url):
    """
    ALLOWLIST-first: only visit personal blogs and small personal sites.
    Everything else (commercial platforms, social media, publishers) blocked.
    This is why 514 visits returned 0 emails — wrong site types were visited.
    """
    url_lower = url.lower()

    # Hard block first
    for blocked in BLOCKED_DOMAINS:
        if blocked in url_lower:
            return False

    # Allowlist: personal blog platforms always pass
    for allowed in ALLOWED_DOMAINS:
        if allowed in url_lower:
            return True

    # For unknown domains: allow small personal sites
    # Block if URL looks like a commercial directory or list page
    suspicious_patterns = [
        '/join-a-book-club', '/best-book-clubs', '/book-club-picks',
        '/find-a-book-club', '/topics/', '/lists/', '/collections/',
        '/radical-romance', '/women-reading',
    ]
    for pattern in suspicious_patterns:
        if pattern in url_lower:
            return False

    # Unknown domain: allow small personal sites (hard block list above catches junk)
    return True

# ============================================
# PAGE SCRAPING
# ============================================

def _evict_proxy(proxy):
    """Thread-safe removal of a dead proxy. Masks URL in logs. Sets PROXY_DEPLETED if pool empty."""
    global PROXY_DEPLETED
    if not proxy:
        return
    with _PROXY_LOCK:
        if proxy in PROXY_LIST:
            PROXY_LIST.remove(proxy)
            masked = proxy.split('@')[-1][:20] if '@' in proxy else proxy[:20]
            remaining = len(PROXY_LIST)
            print("  PROXY EVICTED (" + str(remaining) + " remaining): ..." + masked)
            if remaining == 0:
                PROXY_DEPLETED = True
                print("  WARNING: All proxies exhausted mid-run — DDG calls halted to protect GitHub IP")

def scrape_page(url, headers, proxies, proxy_str=None):
    """
    Scrape a page for emails. Uses proxy if available.
    On ANY proxy error: falls back to direct connection (no proxy) — page visits don't
    need proxies for GitHub IP protection. Only DDG calls require proxies.
    Proxies are NEVER evicted from page scraping errors — only from DDG connectivity death.
    """
    emails = []
    def _extract(response):
        soup = BeautifulSoup(response.text, 'html.parser')
        found = find_emails(soup.get_text())
        for tag in soup.select('a[href^="mailto:"]'):
            href = tag.get('href', '')
            email = href.replace('mailto:', '').split('?')[0].strip()
            if '@' in email:
                found.append(email)
        return found

    try:
        response = requests.get(url, headers=headers, proxies=proxies, timeout=5, verify=False)
        emails = _extract(response)
    except Exception:
        # Proxy failed — try direct connection as fallback (page visits are safe without proxy)
        try:
            response = requests.get(url, headers=headers, timeout=5, verify=False)
            emails = _extract(response)
        except Exception:
            pass
    return emails

def visit_website(url):
    headers = {'User-Agent': get_random_user_agent()}
    proxy = get_next_proxy()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    emails = scrape_page(url, headers, proxies, proxy_str=proxy)

    # For blog platforms: find dated post links from already-fetched page, visit those for comments
    is_blog = any(p in url.lower() for p in ['blogspot.com', 'wordpress.com', 'blogger.com', 'typepad.com'])
    if is_blog:
        try:
            # Fetch once with verify=False — reuse for both email extraction and post link discovery
            r = requests.get(url, headers=headers, proxies=proxies, timeout=5, verify=False)
            soup = BeautifulSoup(r.text, 'html.parser')
            base_domain = url.split('/')[2]
            post_links = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                if base_domain not in href:
                    continue
                if any(f'/{y}/' in href for y in ['2022', '2023', '2024', '2025', '2026']):
                    if href not in post_links:
                        post_links.append(href)
            for post_url in post_links[:2]:
                emails.extend(scrape_page(post_url, headers, proxies, proxy_str=proxy))
        except Exception:
            pass

    # /contact fallback if still no emails
    if not emails:
        base = url.rstrip('/')
        emails += scrape_page(base + '/contact', headers, proxies, proxy_str=proxy)
    return list(set(emails))

# ============================================
# DAILY RUN CHECK (local only)
# ============================================

def already_ran_today():
    if not os.path.exists(TRACKER_FILE):
        return False
    try:
        with open(TRACKER_FILE, 'r') as f:
            data = json.load(f)
        return data.get('last_run_date', '') == datetime.now().strftime('%Y-%m-%d')
    except Exception:
        return False

def save_run_date():
    try:
        with open(TRACKER_FILE, 'w') as f:
            json.dump({'last_run_date': datetime.now().strftime('%Y-%m-%d')}, f)
    except Exception:
        pass

# ============================================
# EMAIL QUALITY REPORT
# ============================================

def analyze_emails(email_list):
    reader_indicators = [
        'scholar', 'student', 'study', 'edu', 'grad', 'applicant',
        'scholarship', 'academic', 'campus', 'uni', 'college'
    ]
    reader_count = sum(
        1 for e in email_list
        if any(ind in e.lower() for ind in reader_indicators)
    )
    total = len(email_list)
    if total > 0:
        pct = round((reader_count / total) * 100, 1)
        print("\n" + "=" * 60)
        print("EMAIL QUALITY REPORT:")
        print("  Total emails    : " + str(total))
        print("  Reader emails   : " + str(reader_count) + " (" + str(pct) + "%)")
        rating = "Excellent!" if pct > 70 else ("Good - improving" if pct > 50 else "Needs better keywords")
        print("  Rating          : " + rating)
        print("=" * 60)


# ============================================
# COMMUNITY SOURCES (Layer 3)
# Reddit JSON API + Goodreads groups + LibraryThing
# No proxy needed for Reddit — public JSON API
# High volume: new posts added daily, sustainable long-term source
# ============================================

# 12 Reddit searches sliced across 6 batches (2 per batch)
# All 6 subreddits verified live and active 2026-07-12: r/IWantOut, r/StudyAbroad (110k members),
# r/gradadmissions (333k members), r/AskAcademia (2.1M members), r/immigration (260k members), r/Japa
REDDIT_SEARCHES = [
    # r/IWantOut — emigration/relocation subreddit, scholarship-relevant threads
    'https://old.reddit.com/r/IWantOut/search.json?q=scholarship+gmail&sort=new&limit=100&restrict_sr=1',
    'https://old.reddit.com/r/IWantOut/search.json?q=study+abroad+contact&sort=new&limit=100&restrict_sr=1',
    # r/StudyAbroad
    'https://old.reddit.com/r/StudyAbroad/search.json?q=scholarship+gmail&sort=new&limit=100&restrict_sr=1',
    'https://old.reddit.com/r/StudyAbroad/search.json?q=email+contact+scholarship&sort=new&limit=100&restrict_sr=1',
    # r/gradadmissions
    'https://old.reddit.com/r/gradadmissions/search.json?q=scholarship+gmail&sort=new&limit=100&restrict_sr=1',
    'https://old.reddit.com/r/gradadmissions/search.json?q=funding+contact+gmail&sort=new&limit=100&restrict_sr=1',
    # r/AskAcademia
    'https://old.reddit.com/r/AskAcademia/search.json?q=scholarship+gmail&sort=new&limit=100&restrict_sr=1',
    'https://old.reddit.com/r/AskAcademia/search.json?q=funding+gmail+contact&sort=new&limit=100&restrict_sr=1',
    # r/immigration
    'https://old.reddit.com/r/immigration/search.json?q=study+visa+gmail&sort=new&limit=100&restrict_sr=1',
    'https://old.reddit.com/r/immigration/search.json?q=student+visa+contact&sort=new&limit=100&restrict_sr=1',
    # r/Japa (Nigerian relocation-abroad community)
    'https://old.reddit.com/r/Japa/search.json?q=scholarship+gmail&sort=new&limit=100&restrict_sr=1',
    'https://old.reddit.com/r/Japa/search.json?q=study+abroad+contact&sort=new&limit=100&restrict_sr=1',
]

# TheGradCafe forum topic sections — public, high email density in applicant discussion threads.
# Verified live 2026-07-12: forum.thegradcafe.com has 315k+ members, 1.4M+ posts.
GOODREADS_GROUP_TOPICS = [
    'https://forum.thegradcafe.com/forum/17-the-bank/',                          # Assistantships, Fellowships, Scholarships
    'https://forum.thegradcafe.com/forum/22-ihog-international-house-of-grads/', # International students (visas, funding)
    'https://forum.thegradcafe.com/forum/4-applications/',                      # Applications discussion
    'https://forum.thegradcafe.com/forum/5-waiting-it-out/',                    # Waiting-period discussion
    'https://forum.thegradcafe.com/forum/54-decisions-decisions/',              # Choosing between funding offers
    'https://forum.thegradcafe.com/',                                           # Forum home (all sections)
]

# African scholarship-listing blogs with public comment sections — members post questions,
# sometimes with contact info. Verified live 2026-07-12.
LIBRARYTHING_GROUPS = [
    'https://www.afterschoolafrica.com/',
    'https://www.scholarshipregion.com/',
    'https://www.scholars4dev.com/2769/scholarship-application-questions-answers/',
    'https://nyscblog.com/',
]

# Scholarship/study-abroad community forum/blog listing pages — high comment density with applicant emails
# Strategy: fetch listing → extract post links → visit posts for comment section emails
STUDY_FORUM_PAGES = [
    'https://www.nairaland.com/education',
    'https://www.afterschoolafrica.com/category/scholarships/',
    'https://www.scholarshipregion.com/category/scholarships/',
    'https://www.scholars4dev.com/category/scholarships-list/',
    'https://www.scholars4dev.com/category/scholarships-list/scholarship-tips-scholarships-list/',
    'https://forum.thegradcafe.com/forum/17-the-bank/',
]

# Scholarship alumni-network / ambassador-program pages — applicants who explicitly signed up
# for a funded program network. Verified live 2026-07-12 (YALI Network: 700k+ members).
ARC_READER_PLATFORMS = [
    'https://yali.state.gov/',
    'https://yali.state.gov/mwf/',
    'https://www.mandelawashingtonfellowship.org/',
    'https://www.mandelawashingtonfellowship.org/frequently-asked-questions/',
]


# Recent-graduate / young-professional communities — highest scholarship-applicant overlap
# These demographics (NYSC corpers, recent grads, working professionals) are the exact
# audience the Marketing Blueprint's Budget Seeker + High Achiever personas target.
SHIFT_WORKER_FORUMS = [
    'https://nyscblog.com/',
    'https://nyscblog.com/category/nysc-news/',
    'https://campuscybercafe.com/blog/post/top-private-companies-that-accept-nysc-corps-members/',
    'https://monoed.africa/blog/after-nysc-career-paths-for-fresh-graduates-nigeria',
    'https://www.afterschoolafrica.com/category/scholarships/',
    'https://www.scholarshipregion.com/',
]

def scrape_reddit_json(batch_searches):
    """
    Scrapes Reddit study-abroad/scholarship subreddits via public JSON API.
    No proxy needed — Reddit's public API is open and rate-limit friendly with 1-2s delays.
    Returns emails extracted from post titles + bodies + flair.
    """
    print("\n--- Reddit Community Sources (" + str(len(batch_searches)) + " searches) ---")
    emails_found = []
    seen = set()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }
    for url in batch_searches:
        if _out_of_time():
            break
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200 or not r.text.strip().startswith('{'):
                print("  Reddit blocked/rate-limited (" + str(r.status_code) + ") — skipping")
                time.sleep(random.uniform(3, 5))
                continue
            data = r.json()
            posts = data.get('data', {}).get('children', [])
            batch_emails = []
            for post in posts:
                pd = post.get('data', {})
                text = pd.get('title', '') + ' ' + pd.get('selftext', '') + ' ' + pd.get('url', '')
                for e in find_emails(text):
                    if e not in seen:
                        seen.add(e)
                        batch_emails.append(e)
                        print("  REDDIT HIT: " + e)
            emails_found.extend(batch_emails)
            sub = url.split('/r/')[1].split('/')[0] if '/r/' in url else 'reddit'
            print("  r/" + sub + ": " + str(len(posts)) + " posts → " + str(len(batch_emails)) + " emails")
            time.sleep(random.uniform(2, 3))
        except Exception as e:
            print("  Reddit error: " + str(e)[:60])
            time.sleep(2)
    print("  Reddit total: " + str(len(emails_found)) + " emails")
    return emails_found


def scrape_goodreads_groups(batch_groups):
    """
    Scrapes Goodreads group discussion topic pages.
    These are public pages — no login needed.
    Uses proxy to avoid rate limiting.
    """
    print("\n--- GradCafe Forum Sources (" + str(len(batch_groups)) + " sections) ---")
    emails_found = []
    seen = set()
    headers = {'User-Agent': get_random_user_agent()}
    for url in batch_groups:
        if _out_of_time():
            break
        try:
            proxy = get_next_proxy()
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            r = requests.get(url, headers=headers, proxies=proxies, timeout=8, verify=False)
            soup = BeautifulSoup(r.text, 'html.parser')
            text = soup.get_text()
            batch_emails = []
            for e in find_emails(text):
                if e not in seen:
                    seen.add(e)
                    batch_emails.append(e)
                    print("  GRADCAFE HIT: " + e)
            emails_found.extend(batch_emails)
            print("  " + url[-50:] + " → " + str(len(batch_emails)) + " emails")
            time.sleep(random.uniform(3, 5))
        except Exception as ex:
            print("  Goodreads error: " + str(ex)[:60])
    print("  GradCafe total: " + str(len(emails_found)) + " emails")
    return emails_found


def scrape_librarything_groups(batch_groups):
    """
    Scrapes African scholarship-listing blogs with public comment sections.
    Public pages, applicants sometimes post contact info in comments.
    """
    print("\n--- Scholarship Blog Sources (" + str(len(batch_groups)) + " sites) ---")
    emails_found = []
    seen = set()
    headers = {'User-Agent': get_random_user_agent()}
    for url in batch_groups:
        if _out_of_time():
            break
        try:
            proxy = get_next_proxy()
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            r = requests.get(url, headers=headers, proxies=proxies, timeout=8, verify=False)
            soup = BeautifulSoup(r.text, 'html.parser')
            text = soup.get_text()
            for e in find_emails(text):
                if e not in seen:
                    seen.add(e)
                    emails_found.append(e)
                    print("  SCHOLARSHIP-BLOG HIT: " + e)
            time.sleep(random.uniform(2, 3))
        except Exception as ex:
            print("  LibraryThing error: " + str(ex)[:60])
    print("  Scholarship-blog total: " + str(len(emails_found)) + " emails")
    return emails_found


def scrape_forum_pages(batch_pages):
    """
    Scrape scholarship/study-abroad community forum/blog listing pages.
    Fetches listing page → finds individual post/thread links → visits those for comment emails.
    Comment sections are the highest-density email surface on these sites.
    """
    print("\n--- Scholarship Forums (" + str(len(batch_pages)) + " pages) ---")
    emails_found = []
    seen = set()
    headers = {'User-Agent': get_random_user_agent()}

    for listing_url in batch_pages:
        if _out_of_time():
            break
        try:
            proxy = get_next_proxy()
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            try:
                r = requests.get(listing_url, headers=headers, proxies=proxies, timeout=8, verify=False)
            except Exception:
                r = requests.get(listing_url, headers=headers, timeout=8, verify=False)
            soup = BeautifulSoup(r.text, 'html.parser')

            # Extract emails directly from listing page
            for e in find_emails(soup.get_text()):
                if e not in seen:
                    seen.add(e)
                    emails_found.append(e)
                    print("  FORUM HIT: " + e)

            # Find individual post/thread links on same domain
            base_domain = '/'.join(listing_url.split('/')[:3])
            post_links = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                if not href.startswith('http'):
                    href = base_domain + href if href.startswith('/') else ''
                if not href or base_domain.split('//')[1] not in href:
                    continue
                skip = any(p in href for p in ['/tag/', '/category/', '/page/', '?s=', '#', '/author/'])
                if not skip and href != listing_url and href not in post_links:
                    post_links.append(href)

            # Visit up to 5 post pages for comment section emails
            for post_url in post_links[:5]:
                if _out_of_time():
                    break
                try:
                    pr = requests.get(post_url, headers=headers, proxies=proxies, timeout=6, verify=False)
                    for e in find_emails(BeautifulSoup(pr.text, 'html.parser').get_text()):
                        if e not in seen:
                            seen.add(e)
                            emails_found.append(e)
                            print("  FORUM COMMENT HIT: " + e)
                    time.sleep(random.uniform(1, 2))
                except Exception:
                    pass

            print("  " + listing_url[-60:] + " → " + str(len(emails_found)) + " total")
            time.sleep(random.uniform(3, 5))
        except Exception as ex:
            print("  Forum error: " + str(ex)[:60])

    print("  Forums total: " + str(len(emails_found)) + " emails")
    return emails_found


def scrape_arc_platforms(batch_pages):
    """
    Scrape ARC reader platform directories and recruitment pages.
    These are applicants who explicitly signed up for a funded program network — warm prospects.
    """
    print("\n--- Scholarship Alumni Networks (" + str(len(batch_pages)) + " pages) ---")
    emails_found = []
    seen = set()
    headers = {'User-Agent': get_random_user_agent()}

    for url in batch_pages:
        if _out_of_time():
            break
        try:
            proxy = get_next_proxy()
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            try:
                r = requests.get(url, headers=headers, proxies=proxies, timeout=8, verify=False)
            except Exception:
                r = requests.get(url, headers=headers, timeout=8, verify=False)
            soup = BeautifulSoup(r.text, 'html.parser')
            for e in find_emails(soup.get_text()):
                if e not in seen:
                    seen.add(e)
                    emails_found.append(e)
                    print("  ALUMNI-NETWORK HIT: " + e)
            print("  " + url[-60:] + " → " + str(len(emails_found)) + " total")
            time.sleep(random.uniform(2, 4))
        except Exception as ex:
            print("  ARC platform error: " + str(ex)[:60])

    print("  Alumni networks total: " + str(len(emails_found)) + " emails")
    return emails_found


# ============================================
# MAIN SCRAPER
# ============================================

def daily_scrape():
    print("=" * 60)
    print("SCHOLARSHIP & VISA EMAIL ROBOT - SELF-SUSTAINING ENGINE")
    print("Date: " + datetime.now().strftime('%B %d, %Y at %I:%M %p'))
    print("=" * 60)

    # Soft timeout — exit all loops at 82 min so auto-commit always gets its window
    # GitHub hard-kills at 90 min; 8 min buffer covers auto-commit + cache + artifact steps
    global _SCRAPER_DEADLINE
    _SCRAPER_DEADLINE = time.time() + (82 * 60)
    _scraper_start = time.time()  # kept for per-source timing

    # ── Init proxy list FIRST so diagnostics shows correct count ──
    _init_proxy_list()
    print_startup_diagnostics()

    # ── Adaptive engine: MUST run before get_daily_keywords so expansion affects count ──
    expansion_level = get_expansion_level()
    if expansion_level > 0:
        apply_expansion(expansion_level)

    # --- Get today's keyword set (batch slice handled inside get_daily_keywords) ---
    all_keywords = get_daily_keywords()

    # --- Sleep config ---
    INTER_URL_SLEEP = (0.5, 1.0) if IS_GITHUB_ACTIONS else (3, 6)
    KEYWORD_SLEEP   = (0.3, 0.5) if IS_GITHUB_ACTIONS else (12, 18)
    COOLDOWN_SLEEP  = (40, 60)

    all_emails = []
    total_websites = 0
    skipped_ttl = 0
    skipped_blocked = 0
    _initial_master_count = len(load_master_emails())  # snapshot before batch starts

    visited_urls = load_visited_urls()
    fresh_count = count_fresh_urls(visited_urls)
    print("URL cache       : " + str(len(visited_urls)) + " tracked (" + str(fresh_count) + " expired and eligible for revisit)")
    print("Keywords        : " + str(len(all_keywords)) + " active this batch")
    print("Expansion level : " + str(expansion_level) + " (0=normal, 1=+kw, 2=+platforms, 3=+cache purge)")
    print("=" * 60)

    # --- Source 1: Reddit (FIRST — no proxy needed, guaranteed to always run) ---
    # Reddit JSON API is open — completes in 2-3 min regardless of proxy state
    _t_reddit_start = time.time()
    if IS_GITHUB_ACTIONS and BATCH > 0:
        reddit_size  = max(1, len(REDDIT_SEARCHES) // 6)
        r_start      = (BATCH - 1) * reddit_size
        r_end        = r_start + reddit_size if BATCH < 6 else len(REDDIT_SEARCHES)
        batch_reddit = REDDIT_SEARCHES[r_start:r_end]
    else:
        batch_reddit = REDDIT_SEARCHES
    reddit_emails = scrape_reddit_json(batch_reddit)
    reddit_emails = clean_emails(reddit_emails)
    all_emails.extend(reddit_emails)
    if reddit_emails:
        save_master_emails(all_emails)
        print("  Reddit checkpoint: " + str(len(reddit_emails)) + " emails saved")
    _t_reddit_elapsed = int(time.time() - _t_reddit_start)

    # --- Source 2: Email Dork Engine ---
    _t_dork_start = time.time()
    all_dork_queries = generate_dork_queries()
    if IS_GITHUB_ACTIONS and BATCH > 0:
        dork_batch_size = len(all_dork_queries) // 6
        dork_start = (BATCH - 1) * dork_batch_size
        dork_end = dork_start + dork_batch_size if BATCH < 6 else len(all_dork_queries)
        batch_dork_queries = all_dork_queries[dork_start:dork_end]
    else:
        batch_dork_queries = all_dork_queries

    dork_emails, dork_fallback_urls = dork_search(batch_dork_queries)
    _t_dork_elapsed = int(time.time() - _t_dork_start)

    dork_emails = clean_emails(dork_emails)
    all_emails.extend(dork_emails)
    if dork_emails:
        save_master_emails(all_emails)
        print("  Dork checkpoint: " + str(len(dork_emails)) + " emails saved immediately")

    # Visit dork fallback URLs (pages where snippet had no email)
    MAX_FALLBACK = 75
    dork_fallback_urls = dork_fallback_urls[:MAX_FALLBACK]
    print("  Visiting " + str(len(dork_fallback_urls)) + " fallback URLs (capped at " + str(MAX_FALLBACK) + ")")
    for url in dork_fallback_urls:
        if _out_of_time():
            break
        if is_url_stale(visited_urls, url):
            continue
        print("  [DORK FALLBACK] Visiting: " + url[:70])
        emails = visit_website(url)
        mark_visited(visited_urls, url)
        if emails:
            print("  Found " + str(len(emails)) + " email(s)!")
            all_emails.extend(emails)
        time.sleep(random.uniform(*INTER_URL_SLEEP))

    # --- Source 3: Blog directories ---
    _t_dir_start = time.time()
    if IS_GITHUB_ACTIONS and BATCH > 0:
        dir_batch_size = max(1, len(BLOG_DIRECTORIES) // 6)
        dir_start = (BATCH - 1) * dir_batch_size
        dir_end   = dir_start + dir_batch_size if BATCH < 6 else len(BLOG_DIRECTORIES)
        batch_dirs = BLOG_DIRECTORIES[dir_start:dir_end]
        print("  Blog dir slice  : Batch " + str(BATCH) + " gets dirs " + str(dir_start+1) + "–" + str(dir_end))
    else:
        batch_dirs = BLOG_DIRECTORIES
    directory_urls = scrape_blog_directories(batch_dirs)
    directory_urls = directory_urls[:60]
    total_websites += len(directory_urls)
    for url in directory_urls:
        if _out_of_time():
            break
        if is_url_stale(visited_urls, url):
            skipped_ttl += 1
            continue
        if not is_reader_website(url):
            skipped_blocked += 1
            continue
        print("  [DIR] Visiting: " + url[:70])
        emails = visit_website(url)
        mark_visited(visited_urls, url)
        if emails:
            print("  Found " + str(len(emails)) + " email(s)!")
            all_emails.extend(emails)
        time.sleep(random.uniform(*INTER_URL_SLEEP))
    _t_dir_elapsed = int(time.time() - _t_dir_start)

    # --- Source 4: Goodreads + LibraryThing (before keyword loop) ---
    _t_community_start = time.time()
    if not _out_of_time():
        if IS_GITHUB_ACTIONS and BATCH > 0:
            gr_size  = max(1, len(GOODREADS_GROUP_TOPICS) // 6)
            g_start  = (BATCH - 1) * gr_size
            g_end    = g_start + gr_size if BATCH < 6 else len(GOODREADS_GROUP_TOPICS)
            batch_gr = GOODREADS_GROUP_TOPICS[g_start:g_end]
            lt_size  = max(1, len(LIBRARYTHING_GROUPS) // 6)
            l_start  = (BATCH - 1) * lt_size
            l_end    = l_start + lt_size if BATCH < 6 else len(LIBRARYTHING_GROUPS)
            batch_lt = LIBRARYTHING_GROUPS[l_start:l_end]
        else:
            batch_gr = GOODREADS_GROUP_TOPICS
            batch_lt = LIBRARYTHING_GROUPS
        community_emails = []
        community_emails.extend(scrape_goodreads_groups(batch_gr))
        if not _out_of_time():
            community_emails.extend(scrape_librarything_groups(batch_lt))
        community_emails = clean_emails(community_emails)
        all_emails.extend(community_emails)
        print("  Goodreads+LT total: " + str(len(community_emails)) + " emails")
    _t_community_elapsed = int(time.time() - _t_community_start)

    # --- Source 6: Study-Abroad Forums + Alumni Networks + Recent-Graduate Communities ---
    _t_forum_start = time.time()
    if not _out_of_time():
        if IS_GITHUB_ACTIONS and BATCH > 0:
            forum_size = max(1, len(STUDY_FORUM_PAGES) // 6)
            f_start    = (BATCH - 1) * forum_size
            f_end      = f_start + forum_size if BATCH < 6 else len(STUDY_FORUM_PAGES)
            batch_forums = STUDY_FORUM_PAGES[f_start:f_end]
            arc_size   = max(1, len(ARC_READER_PLATFORMS) // 6)
            a_start    = (BATCH - 1) * arc_size
            a_end      = a_start + arc_size if BATCH < 6 else len(ARC_READER_PLATFORMS)
            batch_arc  = ARC_READER_PLATFORMS[a_start:a_end]
            sw_size    = max(1, len(SHIFT_WORKER_FORUMS) // 6)
            sw_start   = (BATCH - 1) * sw_size
            sw_end     = sw_start + sw_size if BATCH < 6 else len(SHIFT_WORKER_FORUMS)
            batch_sw   = SHIFT_WORKER_FORUMS[sw_start:sw_end]
        else:
            batch_forums = STUDY_FORUM_PAGES
            batch_arc    = ARC_READER_PLATFORMS
            batch_sw     = SHIFT_WORKER_FORUMS
        forum_emails = clean_emails(scrape_forum_pages(batch_forums))
        all_emails.extend(forum_emails)
        arc_emails = []
        if not _out_of_time():
            arc_emails = clean_emails(scrape_arc_platforms(batch_arc))
            all_emails.extend(arc_emails)
        sw_emails = []
        if not _out_of_time():
            sw_emails = clean_emails(scrape_forum_pages(batch_sw))
            all_emails.extend(sw_emails)
        if forum_emails or arc_emails or sw_emails:
            save_master_emails(all_emails)
            print("  Forum+ARC+SW checkpoint: " + str(len(forum_emails) + len(arc_emails) + len(sw_emails)) + " emails saved")
    _t_forum_elapsed = int(time.time() - _t_forum_start)

    # --- Source 5 (keyword loop): DDG multi-region + modifier + blog searches (fills remaining time) ---
    _t_kw_start = time.time()
    consecutive_failures = 0
    for idx, keyword in enumerate(all_keywords):
        if _out_of_time():
            break
        # Top-up proxy pool every 10 keywords OR immediately if pool is depleted
        if IS_GITHUB_ACTIONS:
            if PROXY_DEPLETED or (idx > 0 and idx % 10 == 0):
                _maybe_topup_proxies()

        print("\n[" + str(idx + 1) + "/" + str(len(all_keywords)) + "] " + keyword)

        urls, snippet_emails = search_google(keyword, num_results=10, retry=1)

        # Collect snippet emails immediately — free hits with no page visit
        if snippet_emails:
            all_emails.extend(snippet_emails)

        total_websites += len(urls)
        if len(urls) == 0 and len(snippet_emails) == 0:
            consecutive_failures += 1
            if consecutive_failures == 15:
                print("  WARNING: 15 consecutive 0-result keywords — search unreliable, continuing anyway")
            # No break — keep trying remaining keywords (Bing direct should recover)
        else:
            consecutive_failures = 0

        visited_this_keyword = 0
        for url in urls:
            if visited_this_keyword >= 5:  # max 5 URL visits per keyword
                break
            if is_url_stale(visited_urls, url):
                skipped_ttl += 1
                continue
            if not is_reader_website(url):
                skipped_blocked += 1
                continue

            print("  Visiting: " + url[:70])
            emails = visit_website(url)
            mark_visited(visited_urls, url)
            visited_this_keyword += 1

            if emails:
                print("  Found " + str(len(emails)) + " email(s)!")
                all_emails.extend(emails)

            time.sleep(random.uniform(*INTER_URL_SLEEP))

        if (idx + 1) % 5 == 0:
            print("\n--- Progress: " + str(len(all_emails)) + " emails so far ---")
            save_visited_urls(visited_urls)

        # Mid-run checkpoint every 10 keywords — survives timeout kill
        if IS_GITHUB_ACTIONS and (idx + 1) % 10 == 0 and all_emails:
            save_master_emails(all_emails)
            try:
                import subprocess
                # Inject GITHUB_TOKEN into remote URL so push works from inside the script
                _gh_token = os.environ.get('GITHUB_TOKEN', '')
                _gh_repo  = os.environ.get('GITHUB_REPOSITORY', '')
                if _gh_token and _gh_repo:
                    _remote = 'https://x-access-token:' + _gh_token + '@github.com/' + _gh_repo + '.git'
                    subprocess.run(['git', 'remote', 'set-url', 'origin', _remote], capture_output=True)
                subprocess.run(['git', 'add', 'master_emails.txt', 'visited_urls.json', 'emails_log.txt'], capture_output=True)
                subprocess.run(['git', 'commit', '-m', 'bot: mid-run checkpoint [skip ci]'], capture_output=True)
                r = subprocess.run(['git', 'push', 'origin', 'main'], capture_output=True, text=True)
                if r.returncode == 0:
                    print("  Checkpoint committed (" + str(len(all_emails)) + " emails so far)")
                else:
                    print("  Checkpoint push failed: " + r.stderr[:60])
            except Exception as e:
                print("  Checkpoint skipped: " + str(e)[:40])

        if not IS_GITHUB_ACTIONS and (idx + 1) % 10 == 0:
            print("\n--- Cooling down... ---")
            time.sleep(random.uniform(*COOLDOWN_SLEEP))
        else:
            time.sleep(random.uniform(*KEYWORD_SLEEP))

    _t_kw_elapsed = int(time.time() - _t_kw_start)

    # --- Final save and report ---
    _t_total_elapsed = int(time.time() - _scraper_start)

    save_visited_urls(visited_urls)
    all_emails = clean_emails(all_emails)
    save_master_emails(all_emails)
    new_email_count = len(load_master_emails()) - _initial_master_count  # true count across all checkpoint saves
    record_batch_yield(new_email_count)

    print("=" * 60)
    print("BATCH COMPLETE")
    print("  Emails found today      : " + str(len(all_emails)))
    print("  Added to master list    : " + str(new_email_count))
    print("  Websites visited        : " + str(total_websites - skipped_ttl - skipped_blocked))
    print("  Skipped (TTL - recent)  : " + str(skipped_ttl))
    print("  Skipped (blocked site)  : " + str(skipped_blocked))
    print("  --- Time breakdown ---")
    print("  Reddit (Source 1)       : " + str(_t_reddit_elapsed) + "s")
    print("  Dork engine (Source 2)  : " + str(_t_dork_elapsed) + "s")
    print("  Blog dirs (Source 3)    : " + str(_t_dir_elapsed) + "s")
    print("  Goodreads+LT (Source 4) : " + str(_t_community_elapsed) + "s")
    print("  Forums+ARC (Source 6)   : " + str(_t_forum_elapsed) + "s")
    print("  Keywords (Source 5)     : " + str(_t_kw_elapsed) + "s")
    print("  Total elapsed           : " + str(_t_total_elapsed) + "s / " + str(int(_t_total_elapsed / 60)) + "m")
    print("=" * 60)

    analyze_emails(all_emails)

    if len(all_emails) < 100:
        print("LOW: Check DIAGNOSTICS above — proxy or DDG issue")
    elif len(all_emails) < 400:
        print("BUILDING: Growing across batches toward 750")
    elif len(all_emails) < 750:
        print("GOOD: Heading toward 750+")
    else:
        print("TARGET REACHED: 750+ emails today!")
    print("=" * 60)

if __name__ == '__main__':
    daily_scrape()
