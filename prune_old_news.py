#!/usr/bin/env python3
"""Delete articles older than RETENTION_DAYS (default 30).

Intended to run as a scheduled job (e.g. Heroku Scheduler: `python prune_old_news.py`)
to keep the rolling window of news bounded and the database under its storage limit.

Connection:
  - Uses DATABASE_URL (Heroku) when present, otherwise the local DB_* env vars.
"""
import os
import logging

from news_collector import DatabaseManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("prune_old_news")


def _dsn():
    dsn = os.getenv("DATABASE_URL")
    if dsn and dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    return dsn


def main():
    days = int(os.getenv("RETENTION_DAYS", "30"))
    dsn = _dsn()
    db = DatabaseManager(dsn=dsn) if dsn else DatabaseManager()

    with db:
        cur = db.conn.cursor()
        try:
            cur.execute(
                "DELETE FROM articles WHERE time_published < (now() - make_interval(days => %s))",
                (days,),
            )
            deleted = cur.rowcount
            db.conn.commit()
            logger.info("Pruned %s articles older than %s days", deleted, days)
        except Exception:
            db.conn.rollback()
            logger.exception("Prune failed; rolled back")
            raise
        finally:
            cur.close()


if __name__ == "__main__":
    main()
