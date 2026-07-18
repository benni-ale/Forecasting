#!/usr/bin/env python3
"""Collect Italian financial news from RSS feeds into the articles table.

First concrete step of the Italian-market extension: articles are ingested
WITHOUT sentiment scoring (ticker_sentiment stays empty), so they don't feed
the dashboard KPIs yet, but they accumulate in the hot store and — via the
nightly archive_job — in the MotherDuck cold store, building the corpus for
the future LLM-as-annotator + fine-tuning phase.

Insertion is idempotent on the article URL (reuses DatabaseManager.save_articles).
Articles are tagged with topic "mercato_italiano" so they're easy to filter:

    SELECT * FROM articles WHERE topics @> '[{"topic": "mercato_italiano"}]';

Usage:
    python italian_news_collector.py                  # all default feeds
    python italian_news_collector.py --feeds ansa_economia,sole24ore_economia

Scheduling (Heroku Scheduler): run daily, any time before archive_job.
Connection: DATABASE_URL (Heroku) when present, otherwise local DB_* env vars.
"""
import argparse
import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from job_logging import finish_execution, start_execution
from news_collector import DatabaseManager

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("italian_news_collector")

# Feed verificati funzionanti (luglio 2026). Chiave = nome breve per --feeds.
DEFAULT_FEEDS = {
    "ansa_economia": ("ANSA Economia", "https://www.ansa.it/sito/notizie/economia/economia_rss.xml"),
    "sole24ore_economia": ("Il Sole 24 Ore", "https://www.ilsole24ore.com/rss/economia.xml"),
    "sole24ore_finanza": ("Il Sole 24 Ore", "https://www.ilsole24ore.com/rss/finanza.xml"),
    "investing_it": ("Investing.com Italia", "https://it.investing.com/rss/news.rss"),
    "trend_online": ("Trend Online", "https://www.trend-online.com/rss/"),
    "soldionline": ("SoldiOnline", "https://www.soldionline.it/rss"),
    "repubblica_economia": ("Repubblica Economia", "https://www.repubblica.it/rss/economia/rss2.0.xml"),
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DollarPunkBot/1.0; +https://dollarpunk-26786f215068.herokuapp.com)"}
ATOM_NS = "{http://www.w3.org/2005/Atom}"
TAG_RE = re.compile(r"<[^>]+>")


def _clean(text):
    """Strip HTML tags and collapse whitespace from feed fields."""
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub(" ", text))
    return re.sub(r"\s+", " ", text).strip()


def _parse_pubdate(item):
    """Return the item timestamp in Alpha Vantage format (YYYYMMDDTHHMMSS) or None."""
    raw = (
        item.findtext("pubDate")
        or item.findtext(f"{ATOM_NS}published")
        or item.findtext(f"{ATOM_NS}updated")
        or item.findtext("{http://purl.org/dc/elements/1.1/}date")
    )
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw.strip())
    except (ValueError, TypeError):
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.strftime("%Y%m%dT%H%M%S")


def parse_feed(source_name, content):
    """Parse an RSS 2.0 or Atom feed into Alpha Vantage-shaped article dicts."""
    root = ET.fromstring(content)
    items = root.findall(".//item") or root.findall(f".//{ATOM_NS}entry")
    articles = []
    for item in items:
        link = item.findtext("link")
        if link is None:  # Atom: <link href="..."/>
            link_el = item.find(f"{ATOM_NS}link")
            link = link_el.get("href") if link_el is not None else None
        link = (link or "").strip()
        title = _clean(item.findtext("title") or item.findtext(f"{ATOM_NS}title"))
        if not link or not title:
            continue
        summary = _clean(
            item.findtext("description") or item.findtext(f"{ATOM_NS}summary") or ""
        )
        articles.append({
            "url": link,
            "title": title,
            "source": source_name,
            "time_published": _parse_pubdate(item),
            "summary": summary,
            "overall_sentiment_score": None,
            "overall_sentiment_label": "",
            # Nessuno scoring in questa fase: il campo resta vuoto e gli
            # articoli non entrano nei KPI (le viste filtrano su ticker_sentiment).
            "ticker_sentiment": [],
            "topics": [{"topic": "mercato_italiano", "relevance_score": "1.0"}],
            "banner_image": "",
            "source_domain": urlparse(link).netloc,
            "provider": "rss_italia",
        })
    return articles


def _db():
    dsn = os.getenv("DATABASE_URL")
    if dsn and dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    return DatabaseManager(dsn=dsn) if dsn else DatabaseManager()


def main():
    parser = argparse.ArgumentParser(description="Collect Italian financial news from RSS feeds")
    parser.add_argument(
        "--feeds",
        help="Comma-separated feed keys (default: all). Available: " + ", ".join(DEFAULT_FEEDS),
    )
    args = parser.parse_args()

    keys = [k.strip() for k in args.feeds.split(",")] if args.feeds else list(DEFAULT_FEEDS)
    unknown = [k for k in keys if k not in DEFAULT_FEEDS]
    if unknown:
        raise SystemExit(f"Unknown feed keys: {unknown}. Available: {list(DEFAULT_FEEDS)}")

    trigger = os.getenv("JOB_TRIGGER_SOURCE", "manual")
    exec_id = start_execution(
        "italian_news_collector",
        trigger,
        extra_metrics={"feeds": keys},
    )

    totals = {"found": 0, "inserted": 0, "skipped": 0}
    feed_errors = []
    try:
        articles = []
        for key in keys:
            source_name, url = DEFAULT_FEEDS[key]
            try:
                resp = requests.get(url, headers=HEADERS, timeout=20)
                resp.raise_for_status()
                parsed = parse_feed(source_name, resp.content)
                logger.info("[%s] %s articoli dal feed", key, len(parsed))
                articles.extend(parsed)
            except Exception as e:
                # Un feed rotto non deve fermare gli altri.
                logger.warning("[%s] feed non disponibile: %s", key, e)
                feed_errors.append(key)

        totals["found"] = len(articles)
        db = _db()
        with db:
            result = db.save_articles(articles)
        totals["inserted"] = result["inserted"]
        totals["skipped"] = result["skipped"]

        summary = (
            f"Italian news: {totals['found']} found, {totals['inserted']} inserted, "
            f"{totals['skipped']} duplicates skipped"
            + (f" | feed falliti: {feed_errors}" if feed_errors else "")
        )
        logger.info(summary)
        finish_execution(
            exec_id,
            "completed",
            articles_found=totals["found"],
            articles_inserted=totals["inserted"],
            articles_skipped=totals["skipped"],
            summary_message=summary,
            extra_metrics={"feeds": keys, "feed_errors": feed_errors},
        )
    except Exception as e:
        finish_execution(
            exec_id,
            "error",
            articles_found=totals["found"],
            articles_inserted=totals["inserted"],
            error_message=str(e),
            summary_message=f"Italian news collection failed: {e}",
        )
        raise


if __name__ == "__main__":
    main()
