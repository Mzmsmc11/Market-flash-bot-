"""
Watcher Guru-style market + news bot.

Posts to every platform you configure (Telegram channel, Facebook Page, X):
  * "JUST IN:" price alerts when Bitcoin, Gold, Silver or NAS100 move fast
  * A "MARKET UPDATE" snapshot every few hours
  * "BREAKING:" / "JUST IN:" headlines from RSS feeds

Install:
  pip install feedparser requests requests-oauthlib

Environment variables (set only the platforms you want; at least one needed):
  Telegram:  BOT_TOKEN, CHANNEL_ID            (e.g. @yourchannel)
  Facebook:  FB_PAGE_ID, FB_PAGE_TOKEN        (Page access token)
             FB_API_VERSION                   (optional, default v26.0)
  X:         X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET

Run:  python market_news_bot.py
"""

import calendar
import html
import logging
import os
import re
import sqlite3
import time
from collections import deque

import feedparser
import requests

# ---------------------------------------------------------------- config ---
DB_PATH = os.getenv("DB_PATH", "seen.db")

NEWS_POLL_SECONDS = int(os.getenv("NEWS_POLL_SECONDS", "60"))
PRICE_POLL_SECONDS = int(os.getenv("PRICE_POLL_SECONDS", "30"))
SNAPSHOT_HOURS = float(os.getenv("SNAPSHOT_HOURS", "4"))  # 0 disables snapshots
ALERT_COOLDOWN = 30 * 60   # min seconds between alerts for the same asset
MIN_HISTORY = 10 * 60      # need this much price history before alerting
WINDOW = 60 * 60           # compare current price to the last hour

MAX_POSTS_PER_CYCLE = 5
MAX_AGE_HOURS = 3

# threshold = % move inside the window that triggers an alert (2x = "surges"/"plunges").
# Yahoo symbols: GC=F gold futures, SI=F silver futures, NQ=F Nasdaq-100 futures.
ASSETS = {
    "Bitcoin": {"symbol": "BTC-USD", "threshold": 1.5, "prefix": "$", "tag": "$BTC"},
    "Gold":    {"symbol": "GC=F",    "threshold": 0.5, "prefix": "$", "tag": "$XAU"},
    "Silver":  {"symbol": "SI=F",    "threshold": 1.0, "prefix": "$", "tag": "$XAG"},
    "NAS100":  {"symbol": "NQ=F",    "threshold": 0.7, "prefix": "",  "tag": "$NDX"},
}

# Check each URL still works; publishers change them.
FEEDS = {
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "CoinDesk": "https://www.coindesk.com/arc/outputfeeds/rss/",
}

BREAKING_WORDS = re.compile(
    r"\b(breaking|just in|fed|rate (cut|hike)|crash|surge|plunge|soar|"
    r"bitcoin|etf|sec|inflation|cpi|jobs report|tariff|default|bankrupt)\b",
    re.IGNORECASE,
)

HEADERS = {"User-Agent": "Mozilla/5.0 marketnewsbot"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ----------------------------------------------------------- storage -------
db = sqlite3.connect(DB_PATH)
db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts INTEGER)")
db.commit()


def already_seen(uid):
    return db.execute("SELECT 1 FROM seen WHERE id = ?", (uid,)).fetchone() is not None


def mark_seen(uid):
    db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (uid, int(time.time())))
    db.commit()


# -------------------------------------------------------- publishers -------
# Each publisher takes plain text plus an optional source link/name and
# returns True on success.

class Telegram:
    name = "Telegram"

    def __init__(self):
        self.token = os.getenv("BOT_TOKEN")
        self.chat = os.getenv("CHANNEL_ID")

    def enabled(self):
        return bool(self.token and self.chat)

    def post(self, text, link=None, source=None):
        body = html.escape(text)
        if link:
            body += f'\n\n<a href="{html.escape(link)}">Source: {html.escape(source or "link")}</a>'
        payload = {
            "chat_id": self.chat,
            "text": body,
            "parse_mode": "HTML",
            "disable_web_page_preview": not link,
        }
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for _ in range(3):
            try:
                r = requests.post(url, json=payload, timeout=20)
            except requests.RequestException as e:
                log.warning("Telegram network error: %s", e)
                time.sleep(5)
                continue
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram rate limited, sleeping %ss", wait)
                time.sleep(wait + 1)
                continue
            log.error("Telegram error %s: %s", r.status_code, r.text)
            return False
        return False


class Facebook:
    name = "Facebook"

    def __init__(self):
        self.page_id = os.getenv("FB_PAGE_ID")
        self.token = os.getenv("FB_PAGE_TOKEN")
        self.version = os.getenv("FB_API_VERSION", "v26.0")

    def enabled(self):
        return bool(self.page_id and self.token)

    def post(self, text, link=None, source=None):
        data = {"message": text, "access_token": self.token}
        if link:
            data["link"] = link
        url = f"https://graph.facebook.com/{self.version}/{self.page_id}/feed"
        try:
            r = requests.post(url, data=data, timeout=20)
        except requests.RequestException as e:
            log.warning("Facebook network error: %s", e)
            return False
        if r.status_code == 200:
            return True
        log.error("Facebook error %s: %s", r.status_code, r.text)
        return False


class X:
    """Posts text only. X charges far more for posts containing links,
    so the source is credited as '(via CNBC)' instead of a URL."""

    name = "X"
    LIMIT = 280

    def __init__(self):
        keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")
        self.creds = [os.getenv(k) for k in keys]
        self.auth = None
        if all(self.creds):
            try:
                from requests_oauthlib import OAuth1
                self.auth = OAuth1(*self.creds)
            except ImportError:
                log.error("X disabled: pip install requests-oauthlib")

    def enabled(self):
        return self.auth is not None

    def post(self, text, link=None, source=None):
        suffix = f"\n\n(via {source})" if source else ""
        if len(text) + len(suffix) > self.LIMIT:
            text = text[: self.LIMIT - len(suffix) - 1].rstrip() + "…"
        try:
            r = requests.post(
                "https://api.x.com/2/tweets",
                auth=self.auth,
                json={"text": text + suffix},
                timeout=20,
            )
        except requests.RequestException as e:
            log.warning("X network error: %s", e)
            return False
        if r.status_code in (200, 201):
            return True
        log.error("X error %s: %s", r.status_code, r.text)
        return False


PUBLISHERS = [p for p in (Telegram(), Facebook(), X()) if p.enabled()]


def publish(text, link=None, source=None):
    """Send to every enabled platform. True if at least one succeeded."""
    ok = False
    for p in PUBLISHERS:
        try:
            if p.post(text, link, source):
                ok = True
        except Exception:
            log.exception("%s publish failed", p.name)
    return ok


# ------------------------------------------------------------ prices -------
history = {name: deque() for name in ASSETS}   # (timestamp, price)
last_alert = {name: 0.0 for name in ASSETS}
latest = {}                                    # name -> (price, previous_close)


def fetch_price(symbol):
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"interval": "1m", "range": "1d"},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"]), meta.get("chartPreviousClose")


def money(name, price):
    return f"{ASSETS[name]['prefix']}{price:,.2f}"


def price_text(name, price, change, minutes):
    cfg = ASSETS[name]
    big = abs(change) >= 2 * cfg["threshold"]
    window = "in the past hour" if minutes >= 55 else f"in the last {minutes} minutes"
    if change > 0:
        emoji, verb = ("🚀", "surges") if big else ("🟢", "rises")
    else:
        emoji, verb = ("🚨", "plunges") if big else ("🔴", "falls")
    return (f"JUST IN: {emoji} {name} {cfg['tag']} {verb} {abs(change):.1f}% "
            f"{window} to {money(name, price)}")


def snapshot_text():
    lines = ["📊 MARKET UPDATE", ""]
    for name, (price, prev) in latest.items():
        day, dot = "", "⚪"
        if prev:
            pct = (price - prev) / prev * 100
            day = f" ({pct:+.2f}% today)"
            dot = "🟢" if pct >= 0 else "🔴"
        lines.append(f"{dot} {name}: {money(name, price)}{day}")
    return "\n".join(lines)


def check_prices():
    now = time.time()
    for name, cfg in ASSETS.items():
        try:
            price, prev = fetch_price(cfg["symbol"])
        except Exception as e:
            log.warning("Price fetch failed for %s: %s", name, e)
            continue
        latest[name] = (price, prev)

        h = history[name]
        h.append((now, price))
        while h and now - h[0][0] > WINDOW:
            h.popleft()

        old_ts, old_price = h[0]
        if now - old_ts < MIN_HISTORY or old_price == 0:
            continue
        change = (price - old_price) / old_price * 100
        if abs(change) >= cfg["threshold"] and now - last_alert[name] >= ALERT_COOLDOWN:
            minutes = round((now - old_ts) / 60)
            if publish(price_text(name, price, change, minutes)):
                last_alert[name] = now
                h.clear()                 # new baseline so one move = one alert
                h.append((now, price))


# -------------------------------------------------------------- news -------
def entry_age_hours(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return (time.time() - calendar.timegm(parsed)) / 3600


def news_text(title):
    title = re.sub(r"\s+", " ", title).strip()
    prefix = "🚨 BREAKING:" if BREAKING_WORDS.search(title) else "JUST IN:"
    return f"{prefix} {title}"


def collect_new(first_run):
    fresh = []
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url, request_headers=HEADERS)
        except Exception as e:
            log.warning("Feed %s failed: %s", source, e)
            continue
        for entry in feed.entries:
            uid = entry.get("id") or entry.get("link")
            if not uid or already_seen(uid):
                continue
            if first_run:
                mark_seen(uid)
                continue
            age = entry_age_hours(entry)
            if age is not None and age > MAX_AGE_HOURS:
                mark_seen(uid)
                continue
            fresh.append((age if age is not None else 0, uid, source, entry))
    fresh.sort(key=lambda x: -x[0])  # oldest first
    return fresh


def post_news(first_run):
    posted = 0
    for _, uid, source, entry in collect_new(first_run):
        if posted >= MAX_POSTS_PER_CYCLE:
            break
        if publish(news_text(entry.get("title", "")), entry.get("link"), source):
            mark_seen(uid)
            posted += 1
            time.sleep(3)
    if posted:
        log.info("Posted %d news item(s)", posted)


# -------------------------------------------------------------- main -------
def main():
    if not PUBLISHERS:
        raise SystemExit("No platform configured. Set the Telegram, Facebook or X "
                         "environment variables (see the top of this file).")
    log.info("Publishing to: %s", ", ".join(p.name for p in PUBLISHERS))

    first_news_run = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    next_news = next_price = 0.0
    next_snapshot = time.time() + SNAPSHOT_HOURS * 3600 if SNAPSHOT_HOURS else float("inf")

    while True:
        now = time.time()
        try:
            if now >= next_price:
                check_prices()
                next_price = now + PRICE_POLL_SECONDS
            if now >= next_news:
                post_news(first_news_run)
                first_news_run = False
                next_news = now + NEWS_POLL_SECONDS
            if now >= next_snapshot and len(latest) == len(ASSETS):
                publish(snapshot_text())
                next_snapshot = now + SNAPSHOT_HOURS * 3600
        except Exception:
            log.exception("Loop iteration failed")
        time.sleep(5)


if __name__ == "__main__":
    main()
