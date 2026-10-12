"""GR GT mention monitor.

Runs on a schedule in GitHub Actions. Pulls mentions from Google News, YouTube,
Bluesky, NewsData.io and forum RSS feeds, filters noise, removes duplicates,
scores sentiment, and writes:

  docs/data.json     the mention list the dashboard reads
  docs/status.json   per source health, shown on the dashboard
  data/excluded.json archive of anything filtered out (nothing is ever deleted)
  alerts.md          only written when something needs attention

Each run first migrates the existing data (idempotent), so older rows get the
same cleanup as new ones.
"""
import calendar
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import feedparser
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# ---------------------------------------------------------------- settings

KEYWORDS = ["GR GT", "Toyota GR GT", "GRGT"]  # search terms sent to each source
DATA_PATH = "docs/data.json"
STATUS_PATH = "docs/status.json"
EXCLUDED_PATH = "data/excluded.json"
FEEDS_PATH = "data/forum_feeds.txt"
ALERT_PATH = "alerts.md"

# A real mention contains GR GT / GR-GT / GRGT / GR GT3 as its own token.
# Lookarounds instead of \b so "トヨタGR GT" (no word break) still matches.
KEYWORD_RE = re.compile(r"(?<![A-Za-z0-9])GR[\s\-]?GT3?(?![A-Za-z0-9])", re.I)

# Video game and other off topic content that mentions the car by name.
NOISE_RE = re.compile(
    r"forza|\bfh[3-6]\b|horizon\s*6|roblox|gameplay|gran\s*turismo|\bgt\s?7\b|"
    r"assetto|need for speed|beamng|\bgta\b|minecraft",
    re.I,
)

DEDUPE_TITLE_PLATFORMS = {"news", "forum"}  # syndicated stories share a headline
EXPECT_NONEMPTY = ("google_news", "youtube")  # plus every forum feed
ALERT_AFTER_FAILED_RUNS = 3
ALERT_AFTER_EMPTY_RUNS = 8
ALERT_AFTER_QUIET_DAYS = 14
MAX_TITLE = 240
SENTIMENT_THRESHOLD = 0.05

HEADERS = {"User-Agent": "gr-gt-monitor/2.0 (personal research tool)"}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, application/json, */*",
}

# ------------------------------------------------------------- sentiment

analyzer = SentimentIntensityAnalyzer()
analyzer.lexicon.update({
    # negatives VADER does not know
    "underpowered": -1.8, "cramped": -1.5, "plasticky": -1.8, "sluggish": -1.5,
    "clunky": -1.5, "gimmick": -1.5, "gimmicky": -1.5, "overhyped": -1.5,
    "overpriced": -1.8, "underwhelming": -1.8, "recall": -2.2, "flawed": -1.8,
    "scalper": -1.8, "scalpers": -1.8, "markup": -1.5, "markups": -1.5,
    "delayed": -1.3, "delay": -1.0, "controversy": -1.5, "lawsuit": -1.8,
    # positives, including car slang VADER reads as negative
    "planted": 1.5, "composed": 1.3, "balanced": 1.3, "flagship": 1.8,
    "dominant": 1.8, "premium": 1.3, "refined": 1.5, "sleek": 1.5,
    "gorgeous": 2.0, "insane": 1.5, "sick": 1.5, "killer": 1.5,
    "beast": 1.5, "monster": 1.2, "savage": 1.0, "ferocious": 1.3,
    "dangerous": 0.3, "weird": 0.0, "hard": 0.0, "scarcity": -1.0,
})

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
URL_RE = re.compile(r"https?://\S+|\b[\w.-]+\.(?:com|be|co|net)/\S*", re.I)
ENGLISH_WORDS = {
    "the", "and", "is", "of", "to", "for", "with", "new", "on", "at", "this", "that",
    "it", "its", "you", "my", "we", "vs", "are", "was", "will", "has", "have", "how",
    "what", "why", "first", "review", "price", "drive",
}
FOREIGN_WORDS = {
    "el", "la", "los", "las", "de", "del", "una", "por", "para", "con", "der", "die",
    "das", "und", "ist", "mit", "nicht", "le", "les", "du", "des", "une", "est", "et",
    "pour", "dans", "che", "della", "uma", "não", "zu", "auf", "von", "für", "ein",
    "eine", "wie", "su", "sus", "gli", "nel", "sul", "som", "och", "het", "een",
}


def prep_text(text):
    t = html.unescape(text or "")
    t = URL_RE.sub(" ", t)
    t = EMOJI_RE.sub(" ", t)
    t = t.replace("#", " ")
    return re.sub(r"\s+", " ", t).strip()


def is_scorable(text):
    """VADER only understands English. Skip other scripts and obvious foreign text."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    non_latin = sum(1 for c in letters if ord(c) > 0x24F)
    if non_latin / len(letters) > 0.3:
        return False
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    foreign = sum(1 for w in words if w in FOREIGN_WORDS)
    english = sum(1 for w in words if w in ENGLISH_WORDS)
    if foreign >= 3 or (foreign >= 2 and english == 0):
        return False
    return True


def score_text(text):
    """Returns (label, score). Label is 'unscored' when the text is not English."""
    t = prep_text(text)
    if not is_scorable(t):
        return "unscored", None
    compound = analyzer.polarity_scores(t)["compound"]
    if compound >= SENTIMENT_THRESHOLD:
        label = "positive"
    elif compound <= -SENTIMENT_THRESHOLD:
        label = "negative"
    else:
        label = "neutral"
    return label, round(compound, 3)


# ---------------------------------------------------------------- helpers

def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso(value):
    """Normalise ISO strings, RFC 822 strings, struct_time and epochs to UTC ISO."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, time.struct_time):
            dt = datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
        elif isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            s = str(value).strip()
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return iso(dt)
    except Exception:
        return None


TRACKING_RE = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src|igshid|si$|feature$)", re.I)


def canon_url(url):
    """Strip tracking parameters and fragments. Google News links keep their query."""
    url = (url or "").strip()
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    if not p.scheme or "news.google.com" in p.netloc:
        return url
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_RE.match(k)]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, urlencode(query), ""))


def url_key(url):
    u = canon_url(url)
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u.rstrip("/")


def clean_title(title, source=None):
    t = re.sub(r"\s+", " ", html.unescape(title or "")).strip()
    if source and t.lower().endswith(" - " + source.lower()):
        t = t[: -(len(source) + 3)].rstrip()
    if len(t) > MAX_TITLE:
        t = t[: MAX_TITLE - 1].rstrip() + "…"
    return t


def title_key(title):
    t = re.sub(r"[\W_]+", " ", html.unescape(title or "").lower()).strip()
    return t if len(t) >= 15 else None  # short titles are too generic to compare


def strip_html(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def load_json(path, default, strict=False):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        if strict:
            print(f"ERROR: {path} exists but could not be read ({e}). Aborting so nothing is overwritten.")
            sys.exit(2)
        print(f"WARNING: could not read {path} ({e}), starting that file fresh.")
        return default


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def dumps_rows(rows):
    """One row per line: compact file, readable diffs."""
    if not rows:
        return "[]\n"
    return "[\n" + ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n]\n"


def http_get(url, params=None, headers=None, timeout=20, attempts=3):
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers=headers or HEADERS, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and i < attempts - 1:
                ra = r.headers.get("Retry-After", "")
                time.sleep(min(int(ra), 20) if ra.isdigit() else 2 * (i + 1))
                continue
            return r
        except requests.RequestException as e:
            last = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    raise last


# ----------------------------------------------------------------- health

class Health:
    def __init__(self, name):
        self.name = name
        self.requests = 0
        self.failures = 0
        self.fetched = 0
        self.skipped = False
        self.errors = []

    def ok(self, n):
        self.requests += 1
        self.fetched += n

    def fail(self, msg):
        self.requests += 1
        self.failures += 1
        self.errors.append(str(msg)[:200])
        print(f"[{self.name}] FAILED: {msg}")

    def status(self):
        if self.skipped:
            return "skipped"
        if self.requests and self.failures == self.requests:
            return "failed"
        return "degraded" if self.failures else "ok"


HEALTH = {}


def health(name):
    return HEALTH.setdefault(name, Health(name))


# --------------------------------------------------------------- fetchers

def parse_feed_entries(content):
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"unreadable feed: {feed.bozo_exception}")
    return feed


def fetch_google_news():
    h = health("google_news")
    out = []
    for kw in KEYWORDS:
        try:
            r = http_get(
                f"https://news.google.com/rss/search?q={quote(kw)}&hl=en-US&gl=US&ceid=US:en",
                headers=BROWSER_HEADERS,
            )
            if r.status_code != 200:
                h.fail(f"HTTP {r.status_code} for '{kw}'")
                continue
            feed = parse_feed_entries(r.content)
            for e in feed.entries:
                out.append({
                    "_src": "google_news", "title": e.get("title", ""), "url": e.get("link", ""),
                    "source": e.get("source", {}).get("title", "Google News"), "platform": "news",
                    "occurred_at": e.get("published_parsed") or e.get("published"),
                })
            h.ok(len(feed.entries))
        except Exception as ex:
            h.fail(f"'{kw}': {ex}")
    return out


def fetch_youtube():
    h = health("youtube")
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        h.skipped = True
        print("No YOUTUBE_API_KEY set, skipping YouTube.")
        return []
    out = []
    for kw in KEYWORDS:
        try:
            r = http_get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "snippet", "type": "video", "order": "date",
                        "maxResults": 25, "q": kw, "key": key},
            )
            data = r.json()
            if r.status_code != 200:
                err = data.get("error", {})
                reason = (err.get("errors") or [{}])[0].get("reason", "")
                h.fail(f"HTTP {r.status_code} {reason} {err.get('message', '')[:120]}".strip())
                continue
            items = data.get("items", [])
            for it in items:
                vid = it.get("id", {}).get("videoId")
                if not vid:
                    continue
                sn = it["snippet"]
                out.append({
                    "_src": "youtube", "title": sn.get("title", ""),
                    "url": f"https://youtube.com/watch?v={vid}", "source": sn.get("channelTitle", "YouTube"),
                    "platform": "youtube", "occurred_at": sn.get("publishedAt"),
                })
            h.ok(len(items))
        except Exception as ex:
            h.fail(f"'{kw}': {ex}")
    return out


def bluesky_token(h):
    handle = os.environ.get("BLUESKY_HANDLE")
    pw = os.environ.get("BLUESKY_APP_PASSWORD")
    if not handle or not pw:
        h.skipped = True
        print("No BLUESKY_HANDLE/BLUESKY_APP_PASSWORD set, skipping Bluesky.")
        return None
    try:
        r = requests.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                          json={"identifier": handle, "password": pw}, timeout=20)
        if r.status_code != 200:
            h.fail(f"login HTTP {r.status_code}: {r.text[:120]}")
            return None
        return r.json().get("accessJwt")
    except Exception as ex:
        h.fail(f"login: {ex}")
        return None


def fetch_bluesky():
    h = health("bluesky")
    token = bluesky_token(h)
    if not token:
        return []
    headers = dict(BROWSER_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    out = []
    for kw in KEYWORDS:
        try:
            r = http_get("https://bsky.social/xrpc/app.bsky.feed.searchPosts",
                         params={"q": kw, "limit": 50, "sort": "latest"}, headers=headers)
            if r.status_code != 200:
                h.fail(f"HTTP {r.status_code} for '{kw}': {r.text[:120]}")
                continue
            posts = r.json().get("posts", [])
            for post in posts:
                uri = post.get("uri", "")
                handle = post.get("author", {}).get("handle", "")
                rkey = uri.rsplit("/", 1)[-1] if uri else ""
                rec = post.get("record", {})
                out.append({
                    "_src": "bluesky", "title": rec.get("text", ""),
                    "url": f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else uri,
                    "source": f"@{handle}" if handle else "Bluesky", "platform": "bluesky",
                    "occurred_at": rec.get("createdAt"),
                })
            h.ok(len(posts))
            print(f"[bluesky] {len(posts)} posts for '{kw}'")
        except Exception as ex:
            h.fail(f"'{kw}': {ex}")
    return out


def fetch_newsdata():
    h = health("newsdata")
    key = os.environ.get("NEWSDATA_API_KEY")
    if not key:
        h.skipped = True
        print("No NEWSDATA_API_KEY set, skipping NewsData.io.")
        return []
    out = []
    for kw in KEYWORDS:
        try:
            r = http_get("https://newsdata.io/api/1/news",
                         params={"apikey": key, "q": kw, "language": "en"})
            data = r.json()
            if r.status_code != 200 or data.get("status") == "error":
                msg = data.get("results", {}) if isinstance(data.get("results"), dict) else {}
                h.fail(f"HTTP {r.status_code} for '{kw}': {msg.get('message', r.text[:120])}")
                continue
            results = data.get("results") or []
            for it in results:
                if not it.get("link"):
                    continue
                out.append({
                    "_src": "newsdata", "title": it.get("title") or "", "url": it["link"],
                    "source": it.get("source_name") or it.get("source_id") or "NewsData",
                    "platform": "news", "occurred_at": it.get("pubDate"),
                    "_summary": it.get("description") or "",
                })
            h.ok(len(results))
            print(f"[newsdata] {len(results)} articles for '{kw}'")
        except Exception as ex:
            h.fail(f"'{kw}': {ex}")
    return out


def read_feed_list():
    """Lines: URL, optionally 'URL | all' to count every thread (dedicated GR GT forums)."""
    if not os.path.exists(FEEDS_PATH):
        return []
    feeds = []
    with open(FEEDS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url, _, flag = line.partition("|")
            feeds.append((url.strip(), flag.strip().lower() == "all"))
    return feeds


def get_forum_feed(feed_url):
    """Try feedparser's own user agent first (what worked before, some forums block
    browser-looking agents), then fall back to a plain request. Returns (feed, error)."""
    errors = []
    try:
        feed = feedparser.parse(feed_url)
        code = feed.get("status")
        if feed.entries:
            return feed, None
        errors.append(f"feedparser HTTP {code}" if code and code != 200 else "feedparser got no entries")
    except Exception as ex:
        errors.append(f"feedparser: {ex}")
    for headers in (HEADERS, BROWSER_HEADERS):
        try:
            r = http_get(feed_url, headers=headers, timeout=25)
            if r.status_code != 200:
                errors.append(f"HTTP {r.status_code}")
                continue
            feed = parse_feed_entries(r.content)
            if feed.entries:
                return feed, None
            errors.append("empty feed")
        except Exception as ex:
            errors.append(str(ex)[:80])
    return None, "; ".join(errors)


def fetch_forum_feeds():
    out = []
    for feed_url, take_all in read_feed_list():
        p = urlsplit(feed_url)
        h = health("forum:" + p.netloc.replace("www.", "") + p.path.rstrip("/"))
        try:
            feed, error = get_forum_feed(feed_url)
            if error:
                h.fail(error)
                continue
            title = feed.feed.get("title", p.netloc)
            for e in feed.entries:
                published = e.get("published_parsed") or e.get("updated_parsed")
                out.append({
                    "_src": h.name, "_all": take_all, "title": e.get("title", ""),
                    "url": e.get("link", ""), "source": title, "platform": "forum",
                    "occurred_at": published,
                    "activity_at": e.get("updated_parsed") if e.get("published_parsed") else None,
                    "_summary": strip_html(e.get("summary", "")),
                })
            h.ok(len(feed.entries))
        except Exception as ex:
            h.fail(str(ex))
    return out


# -------------------------------------------------------------- migration

def normalize_row(row):
    r = dict(row)
    r["title"] = clean_title(row.get("title"), row.get("source"))
    r["url"] = canon_url(row.get("url"))
    added = to_iso(row.get("added_at")) or row.get("added_at")
    occurred = to_iso(row.get("occurred_at"))
    if not occurred:
        occurred = added if isinstance(added, str) else None
        r["date_estimated"] = True
    r["added_at"] = added
    r["occurred_at"] = occurred
    if "sentiment_score" not in r:  # legacy row: score it with the current scorer
        r["sentiment_label"], r["sentiment_score"] = score_text(r["title"])
    return r


def migrate(rows):
    """Clean existing rows. Returns (kept, archived). Idempotent, never drops silently."""
    kept, archived = [], []
    seen_urls, seen_titles, seen_acts = set(), set(), set()
    norm = [normalize_row(r) for r in rows]
    norm.sort(key=lambda r: (r.get("occurred_at") or "", r.get("id") or ""))
    for r in norm:
        reason = None
        if NOISE_RE.search(r["title"]):
            reason = "noise"
        elif r.get("kind") == "activity":
            key = f"{url_key(r['url'])}@{(r['occurred_at'] or '')[:10]}"
            if key in seen_acts:
                reason = "duplicate-activity"
            seen_acts.add(key)
        else:
            uk = url_key(r["url"])
            tk = title_key(r["title"]) if r.get("platform") in DEDUPE_TITLE_PLATFORMS else None
            if uk in seen_urls:
                reason = "duplicate-url"
            elif tk and tk in seen_titles:
                reason = "duplicate-title"
            else:
                seen_urls.add(uk)
                if tk:
                    seen_titles.add(tk)
        if reason:
            archived.append({"reason": reason, "item": r})
        else:
            kept.append(r)
    return kept, archived


# ------------------------------------------------------------------- main

def new_stats():
    return {"fetched": 0, "new": 0, "offtopic": 0, "noise": 0, "duplicate": 0, "invalid": 0}


def main():
    now_dt = utcnow()
    now = iso(now_dt)

    existing = load_json(DATA_PATH, [], strict=True)
    if not isinstance(existing, list):
        print(f"ERROR: {DATA_PATH} is not a list. Aborting.")
        sys.exit(2)
    archive = load_json(EXCLUDED_PATH, [])
    prev = load_json(STATUS_PATH, {})

    kept, dropped = migrate(existing)
    if len(kept) + len(dropped) != len(existing):
        print("ERROR: migration lost rows. Aborting without writing.")
        sys.exit(2)
    if dropped:
        counts = {}
        for d in dropped:
            counts[d["reason"]] = counts.get(d["reason"], 0) + 1
        print(f"Migration archived {len(dropped)} rows: {counts}")

    url_keys = {url_key(r["url"]) for r in kept if r.get("kind") != "activity"}
    title_keys = {title_key(r["title"]) for r in kept
                  if r.get("platform") in DEDUPE_TITLE_PLATFORMS and r.get("kind") != "activity"} - {None}
    act_keys = {f"{url_key(r['url'])}@{r['occurred_at'][:10]}" for r in kept if r.get("kind") == "activity"}
    latest_by_url = {}
    for r in kept:
        k = url_key(r["url"])
        latest_by_url[k] = max(latest_by_url.get(k, ""), r["occurred_at"] or "")

    candidates = []
    for name, fn in (("google_news", fetch_google_news), ("youtube", fetch_youtube),
                     ("bluesky", fetch_bluesky), ("newsdata", fetch_newsdata),
                     ("forums", fetch_forum_feeds)):
        try:
            candidates += fn()
        except Exception as ex:
            health(name).fail(f"unexpected error: {ex!r}")

    stats = {name: new_stats() for name in HEALTH}
    for name, h in HEALTH.items():
        stats[name]["fetched"] = h.fetched

    added_rows = []
    counter = 0
    future_cutoff = iso(now_dt + timedelta(hours=1))
    for c in candidates:
        st = stats[c["_src"]]
        title = clean_title(c.get("title"), c.get("source"))
        url = canon_url(c.get("url"))
        if not title or not url:
            st["invalid"] += 1
            continue
        summary = c.get("_summary", "")
        if not c.get("_all") and not KEYWORD_RE.search(title + " " + summary):
            st["offtopic"] += 1
            continue
        if NOISE_RE.search(title):
            st["noise"] += 1
            continue

        occurred = to_iso(c.get("occurred_at"))
        estimated = False
        if not occurred or occurred > future_cutoff:
            occurred, estimated = now, True

        uk = url_key(url)
        kind = None
        if uk in url_keys:
            act = to_iso(c.get("activity_at"))
            base = latest_by_url.get(uk, "")
            akey = f"{uk}@{(act or '')[:10]}"
            newer = act and base and act > iso(datetime.fromisoformat(base.replace("Z", "+00:00")) + timedelta(days=1))
            if c["platform"] == "forum" and newer and akey not in act_keys and act <= future_cutoff:
                kind, occurred, estimated = "activity", act, False
                act_keys.add(akey)
                latest_by_url[uk] = act
            else:
                st["duplicate"] += 1
                continue
        else:
            tk = title_key(title) if c["platform"] in DEDUPE_TITLE_PLATFORMS else None
            if tk and tk in title_keys:
                st["duplicate"] += 1
                continue
            url_keys.add(uk)
            latest_by_url[uk] = occurred
            if tk:
                title_keys.add(tk)

        text = title + (" " + summary[:400] if c["platform"] == "forum" and summary else "")
        label, score = score_text(text)
        row = {
            "id": f"{int(time.time() * 1000)}-{counter}", "url": url, "title": title,
            "source": c.get("source") or c["platform"], "platform": c["platform"],
            "sentiment_label": label, "sentiment_score": score,
            "added_at": now, "occurred_at": occurred,
        }
        if estimated:
            row["date_estimated"] = True
        if kind:
            row["kind"] = kind
        added_rows.append(row)
        st["new"] += 1
        counter += 1

    final = kept + added_rows
    final.sort(key=lambda r: (r.get("occurred_at") or "", r.get("id") or ""))

    # Safety: never write fewer rows than we can account for.
    if len(final) + len(dropped) < len(existing):
        print("ERROR: row count would shrink unexpectedly. Aborting without writing.")
        sys.exit(2)

    # Archive (deduped by id) and data.
    known = {a["item"].get("id") for a in archive if isinstance(a, dict) and "item" in a}
    for d in dropped:
        if d["item"].get("id") not in known:
            archive.append({"archived_at": now, **d})
    write_atomic(DATA_PATH, dumps_rows(final))
    write_atomic(EXCLUDED_PATH, dumps_rows(archive))

    # Status and alerts.
    prev_sources = prev.get("sources", {}) if isinstance(prev, dict) else {}
    sources, alerts = {}, []
    for name, h in HEALTH.items():
        p = prev_sources.get(name, {})
        status, st = h.status(), stats[name]
        failed_runs = p.get("consecutive_failures", 0) + 1 if status == "failed" else 0
        expects = name in EXPECT_NONEMPTY or name.startswith("forum:")
        empty_runs = p.get("consecutive_empty", 0) + 1 if (status in ("ok", "degraded") and h.fetched == 0) else 0
        sources[name] = {
            "status": status, "fetched": h.fetched, "new": st["new"], "noise": st["noise"],
            "duplicate": st["duplicate"], "errors": h.errors[:3],
            "consecutive_failures": failed_runs, "consecutive_empty": empty_runs,
            "last_success": now if status in ("ok", "degraded") else p.get("last_success"),
            "last_new": now if st["new"] else p.get("last_new"),
        }
        if failed_runs >= ALERT_AFTER_FAILED_RUNS:
            alerts.append(f"**{name}** has failed {failed_runs} runs in a row. Last error: "
                          f"{(h.errors or ['unknown'])[0]}")
        if expects and empty_runs >= ALERT_AFTER_EMPTY_RUNS:
            alerts.append(f"**{name}** returned zero entries for {empty_runs} runs in a row. "
                          "The feed or query may be broken.")

    seen_new = [s["last_new"] for s in sources.values() if s.get("last_new")]
    if isinstance(prev, dict) and prev.get("last_new"):
        seen_new.append(prev["last_new"])
    last_new_all = max(seen_new) if seen_new else ""
    if added_rows:
        last_new_all = now
    if last_new_all:
        quiet = (now_dt - datetime.fromisoformat(last_new_all.replace("Z", "+00:00"))).days
        if quiet >= ALERT_AFTER_QUIET_DAYS:
            alerts.append(f"No new mentions in {quiet} days across all sources.")

    active = [h for h in HEALTH.values() if not h.skipped]
    all_failed = bool(active) and all(h.status() == "failed" for h in active)
    if all_failed:
        alerts.append("Every configured source failed this run.")

    status_doc = {
        "last_run": now, "total": len(final), "added": len(added_rows),
        "archived_total": len(archive), "last_new": last_new_all,
        "sources": sources, "alerts": alerts,
    }
    write_atomic(STATUS_PATH, json.dumps(status_doc, indent=2, ensure_ascii=False) + "\n")

    if alerts:
        write_atomic(ALERT_PATH, "GR GT Monitor needs attention (" + now + "):\n\n" +
                     "\n".join(f"- {a}" for a in alerts) + "\n")
    elif os.path.exists(ALERT_PATH):
        os.remove(ALERT_PATH)

    for name, s in sources.items():
        print(f"[{name}] {s['status']}: fetched {s['fetched']}, new {s['new']}, "
              f"noise {s['noise']}, duplicate {s['duplicate']}")
    print(f"Added {len(added_rows)} new rows. Total tracked: {len(final)}. Archived total: {len(archive)}.")
    if all_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
