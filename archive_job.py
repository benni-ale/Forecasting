#!/usr/bin/env python3
"""Archive articles and stock prices into a DuckDB cold store (idempotent).

Copies rows from one or more Postgres sources (local Docker DB and/or the
Heroku DB) into a DuckDB archive, deduplicating on the natural keys
(articles.url, stock_prices.(ticker, price_date)). Safe to re-run any number
of times: already-archived rows are skipped via INSERT OR IGNORE.

Intended usage:
  - One-shot backfill from the local machine:
        python archive_job.py --source both      # local Docker DB + Heroku
        python archive_job.py --source local     # local Docker DB only
  - Scheduled on Heroku (Heroku Scheduler, BEFORE prune_old_news.py):
        python archive_job.py --source heroku

Archive target (ARCHIVE_DB env var, default: archive.duckdb):
  - a local file path  -> archive.duckdb next to the repo
  - md:<database>      -> MotherDuck cloud database (requires MOTHERDUCK_TOKEN)

Sources:
  - heroku: HEROKU_DATABASE_URL or DATABASE_URL (postgres:// DSN)
  - local:  DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD (same defaults as
            news_collector.DatabaseManager: localhost:5432 newsdb/newsuser)
"""
import argparse
import logging
import os

import duckdb
from dotenv import load_dotenv

from job_logging import finish_execution, start_execution

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("archive_job")

# NOTE: la deduplica NON si affida alla PRIMARY KEY (MotherDuck non supporta i
# vincoli): gli insert usano un anti-join sulla chiave naturale. La PK viene
# comunque tentata sul file locale come difesa in più (vedi _ensure_schema).
ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY,
    title TEXT,
    source TEXT,
    time_published TIMESTAMP,
    summary TEXT,
    overall_sentiment_score DECIMAL(5, 4),
    overall_sentiment_label TEXT,
    ticker_sentiment JSON,
    topics JSON,
    banner_image TEXT,
    source_domain TEXT,
    provider TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    archived_from TEXT,
    archived_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS stock_prices (
    ticker TEXT,
    price_date DATE,
    close DECIMAL(18, 6),
    volume BIGINT,
    updated_at TIMESTAMP,
    archived_from TEXT,
    archived_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (ticker, price_date)
);
"""


def _heroku_dsn():
    dsn = os.getenv("HEROKU_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        return None
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    if "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
    return dsn


def _local_dsn():
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "newsdb")
    user = os.getenv("DB_USER", "newsuser")
    password = os.getenv("DB_PASSWORD", "newspass")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def _ensure_schema(con):
    try:
        con.execute(ARCHIVE_SCHEMA)
    except duckdb.Error:
        # MotherDuck non supporta PRIMARY KEY: ricrea lo schema senza vincoli.
        con.execute(
            ARCHIVE_SCHEMA
            .replace("url TEXT PRIMARY KEY", "url TEXT")
            .replace(",\n    PRIMARY KEY (ticker, price_date)", "")
        )
    # Migrazione per archivi creati prima dell'introduzione della colonna provider.
    con.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS provider TEXT")


def _connect_archive():
    target = os.getenv("ARCHIVE_DB", "archive.duckdb")
    logger.info("Opening archive: %s", target)
    if target.startswith("md:"):
        # MotherDuck: il database va creato esplicitamente prima di usarlo.
        name = target[3:]
        con = duckdb.connect("md:")
        con.execute(f'CREATE DATABASE IF NOT EXISTS "{name}"')
        con.execute(f'USE "{name}"')
    else:
        con = duckdb.connect(target)
    _ensure_schema(con)
    return con, target


def _count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _source_has_table(con, table):
    return con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'src' AND table_name = ?",
        [table],
    ).fetchone()[0] > 0


def merge_from_attached(con, label):
    """Merge rows from the catalog attached as 'src' into the archive.

    Idempotent via anti-join on the natural keys (works both on local DuckDB
    files and on MotherDuck, where PRIMARY KEY constraints are unsupported).
    """
    stats = {}
    before = _count(con, "articles")
    con.execute(
        """
        INSERT INTO articles (
            url, title, source, time_published, summary,
            overall_sentiment_score, overall_sentiment_label,
            ticker_sentiment, topics, banner_image, source_domain,
            provider, created_at, updated_at, archived_from
        )
        SELECT
            s.url, s.title, s.source, s.time_published, s.summary,
            s.overall_sentiment_score, s.overall_sentiment_label,
            s.ticker_sentiment::JSON, s.topics::JSON, s.banner_image, s.source_domain,
            s.provider, s.created_at, s.updated_at, ?
        FROM src.articles s
        WHERE s.url IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM articles a WHERE a.url = s.url)
        """,
        [label],
    )
    stats["articles_inserted"] = _count(con, "articles") - before

    if _source_has_table(con, "stock_prices"):
        before = _count(con, "stock_prices")
        con.execute(
            """
            INSERT INTO stock_prices (
                ticker, price_date, close, volume, updated_at, archived_from
            )
            SELECT s.ticker, s.price_date, s.close, s.volume, s.updated_at, ?
            FROM src.stock_prices s
            WHERE NOT EXISTS (
                SELECT 1 FROM stock_prices p
                WHERE p.ticker = s.ticker AND p.price_date = s.price_date
            )
            """,
            [label],
        )
        stats["prices_inserted"] = _count(con, "stock_prices") - before
    else:
        logger.info("[%s] no stock_prices table in source, skipping", label)
        stats["prices_inserted"] = 0

    logger.info(
        "[%s] merged: +%s articles, +%s stock prices",
        label, stats["articles_inserted"], stats["prices_inserted"],
    )
    return stats


def archive_source(con, label, dsn):
    """Attach one Postgres source and merge its rows into the archive."""
    logger.info("[%s] attaching Postgres source", label)
    con.execute(f"ATTACH '{dsn}' AS src (TYPE postgres, READ_ONLY)")
    try:
        return merge_from_attached(con, label)
    finally:
        con.execute("DETACH src")


def main():
    parser = argparse.ArgumentParser(description="Archive Postgres data into DuckDB cold store")
    parser.add_argument(
        "--source",
        choices=["local", "heroku", "both"],
        default=os.getenv("ARCHIVE_SOURCE", "heroku" if os.getenv("DATABASE_URL") else "local"),
        help="Which Postgres source(s) to archive from",
    )
    args = parser.parse_args()

    sources = []
    if args.source in ("local", "both"):
        sources.append(("local", _local_dsn()))
    if args.source in ("heroku", "both"):
        dsn = _heroku_dsn()
        if not dsn:
            raise SystemExit("Heroku source requested but HEROKU_DATABASE_URL/DATABASE_URL is not set")
        sources.append(("heroku", dsn))

    trigger = os.getenv("JOB_TRIGGER_SOURCE", "manual")
    exec_id = start_execution(
        "archive_job",
        trigger,
        extra_metrics={"source": args.source, "archive_db": os.getenv("ARCHIVE_DB", "archive.duckdb")},
    )

    totals = {"articles_inserted": 0, "prices_inserted": 0}
    try:
        con, target = _connect_archive()
        try:
            for label, dsn in sources:
                stats = archive_source(con, label, dsn)
                totals["articles_inserted"] += stats["articles_inserted"]
                totals["prices_inserted"] += stats["prices_inserted"]

            archive_articles = _count(con, "articles")
            archive_prices = _count(con, "stock_prices")
        finally:
            con.close()

        summary = (
            f"Archived +{totals['articles_inserted']} articles, "
            f"+{totals['prices_inserted']} prices into {target} "
            f"(archive now: {archive_articles} articles, {archive_prices} prices)"
        )
        logger.info(summary)
        finish_execution(
            exec_id,
            "completed",
            articles_inserted=totals["articles_inserted"],
            summary_message=summary,
            extra_metrics={
                **totals,
                "archive_total_articles": archive_articles,
                "archive_total_prices": archive_prices,
            },
        )
    except Exception as e:
        finish_execution(
            exec_id,
            "error",
            error_message=str(e),
            summary_message=f"Archive failed: {e}",
            extra_metrics=totals,
        )
        raise


if __name__ == "__main__":
    main()
