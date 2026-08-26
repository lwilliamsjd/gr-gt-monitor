import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

KEYWORDS = ["GR GT", "Toyota GR GT"]
DATA_PATH = "docs/data.json"
FEEDS_PATH = "data/forum_feeds.txt"
HEADERS = {"User-Agent": "gr-gt-monitor/1.0 (personal research tool)"}

analyzer = SentimentIntensityAnalyzer()

# VADER's default lexicon doesn't cover automotive/forum specific terms.
# Boost it with the same vocabulary used in the local HTML tool.
AUTOMOTIVE_LEXICON = {
    "underpowered": -1.8, "cramped": -1.5, "plasticky": -1.8, "sluggish": -1.5,
    "clunky": -1.5, "gimmick": -1.5, "gimmicky": -1.5, "overhyped": -1.5,
    "overpriced": -1.8, "underwhelming": -1.8, "recall": -2.2, "flawed": -1.8,
    "planted": 1.5, "composed": 1.3, "balanced": 1.3, "flagship": 1.8,
    "dominant": 1.8, "premium": 1.3, "refined": 1.5, "sleek": 1.5, "gorgeous": 2.0,
}
analyzer.lexicon.update(AUTOMOTIVE_LEXICON)


def score_sentiment(text):
    compound = analyzer.polarity_scores(text or "")["compound"]
    if compound >= 0.05:
        return "positive"
    if compound <= -0.05:
        return "negative"
    return "neutral"


def matches_keywords(text):
    if not text:
        return False
    lower = text.lower()
    return any(k.lower() in lower for k in KEYWORDS)


def fetch_google_news():
    results = []
    for kw in KEYWORDS:
        url = f"https://news.google.com/rss/search?q={quote(kw)}&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(url)
        for e in feed.entries:
            results.append({
                "title": e.title,
                "url": e.link,
                "source": e.get("source", {}).get("title", "Google News"),
                "platform": "news",
                "occurred_at": e.get("published", ""),
            })
    return results


def fetch_reddit():
    results = []
    for kw in KEYWORDS:
        try:
            resp = requests.get(
                "https://www.reddit.com/search.json",
                params={"q": kw, "sort": "new", "limit": 50},
                headers=HEADERS, timeout=15,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            for child in data.get("data", {}).get("children", []):
                d = child["data"]
                results.append({
                    "title": d.get("title", ""),
                    "url": f"https://reddit.com{d.get('permalink', '')}",
                    "source": f"r/{d.get('subreddit', '')}",
                    "platform": "reddit",
                    "occurred_at": datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc).isoformat(),
                })
        except Exception as e:
            print(f"Reddit fetch failed for '{kw}': {e}")
    return results


def fetch_youtube():
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("No YOUTUBE_API_KEY set, skipping YouTube.")
        return []
    results = []
    for kw in KEYWORDS:
        try:
            resp = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "snippet", "type": "video", "order": "date", "maxResults": 25, "q": kw, "key": api_key},
                timeout=15,
            )
            data = resp.json()
            for item in data.get("items", []):
                results.append({
                    "title": item["snippet"]["title"],
                    "url": f"https://youtube.com/watch?v={item['id']['videoId']}",
                    "source": item["snippet"]["channelTitle"],
                    "platform": "youtube",
                    "occurred_at": item["snippet"]["publishedAt"],
                })
        except Exception as e:
            print(f"YouTube fetch failed for '{kw}': {e}")
    return results


def fetch_forum_feeds():
    results = []
    if not os.path.exists(FEEDS_PATH):
        return results
    with open(FEEDS_PATH) as f:
        feed_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url)
            feed_title = feed.feed.get("title", feed_url)
            for e in feed.entries:
                summary = e.get("summary", "")
                results.append({
                    "title": e.title,
                    "url": e.link,
                    "source": feed_title,
                    "platform": "forum",
                    "occurred_at": e.get("published", ""),
                    "_summary": summary,
                })
        except Exception as e:
            print(f"Forum feed failed for '{feed_url}': {e}")
    return results


def main():
    existing = []
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH) as f:
            existing = json.load(f)
    existing_urls = {item["url"] for item in existing}

    all_items = []
    all_items += fetch_google_news()
    all_items += fetch_reddit()
    all_items += fetch_youtube()
    all_items += fetch_forum_feeds()

    now = datetime.now(timezone.utc).isoformat()
    added = 0

    for item in all_items:
        if item["url"] in existing_urls:
            continue
        text_to_check = item["title"] + " " + item.get("_summary", "")
        if not matches_keywords(text_to_check):
            continue

        existing.append({
            "id": f"{int(time.time() * 1000)}-{added}",
            "url": item["url"],
            "title": item["title"],
            "source": item["source"],
            "platform": item["platform"],
            "sentiment_label": score_sentiment(item["title"]),
            "added_at": now,
            "occurred_at": item.get("occurred_at") or now,
        })
        existing_urls.add(item["url"])
        added += 1

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(existing, f, indent=2)

    print(f"Added {added} new items. Total tracked: {len(existing)}")


if __name__ == "__main__":
    main()
