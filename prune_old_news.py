#!/usr/bin/env python3
"""Delete articles older than RETENTION_DAYS (default 30).

Intended to run as a scheduled job (e.g. Heroku Scheduler: `python prune_old_news.py`)
to keep the rolling window of news bounded and the database under its storage limit.

Connection:
  - Uses DATABASE_URL (Heroku) when present, otherwise the local DB_* env vars.
"""
import os
import logging

from dotenv import load_dotenv
from news_collector import DatabaseManager
from job_logging import finish_execution, start_execution

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("prune_old_news")


def _dsn():
    dsn = os.getenv("DATABASE_URL")
    if dsn and dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    return dsn


def main():
    days = int(os.getenv("RETENTION_DAYS", "30"))
    trigger = os.getenv("JOB_TRIGGER_SOURCE", "scheduler")
    exec_id = start_execution(
        "prune_old_news",
        trigger,
        extra_metrics={"retention_days": days},
    )
    deleted = 0
    try:
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

        finish_execution(
            exec_id,
            "completed",
            articles_found=deleted,
            summary_message=f"Deleted {deleted} articles older than {days} days",
            extra_metrics={"retention_days": days, "articles_deleted": deleted},
        )
    except Exception as e:
        finish_execution(
            exec_id,
            "error",
            error_message=str(e),
            summary_message=f"Prune failed: {e}",
            extra_metrics={"retention_days": days, "articles_deleted": deleted},
        )
        raise


if __name__ == "__main__":
    main()
