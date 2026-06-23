"""Persist job execution history (manual admin runs + Heroku Scheduler)."""
import json
import logging
import os
from datetime import datetime

from news_collector import DatabaseManager

logger = logging.getLogger(__name__)

STALE_RUNNING_HOURS = 48


def _db():
    dsn = os.getenv('DATABASE_URL')
    if dsn and dsn.startswith('postgres://'):
        dsn = dsn.replace('postgres://', 'postgresql://', 1)
    return DatabaseManager(dsn=dsn) if dsn else DatabaseManager()


def ensure_job_executions_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS job_executions (
            id SERIAL PRIMARY KEY,
            job_name TEXT NOT NULL,
            trigger_source TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            duration_seconds NUMERIC,
            status TEXT NOT NULL DEFAULT 'running',
            num_days INT,
            max_minutes INT,
            articles_found INT DEFAULT 0,
            articles_inserted INT DEFAULT 0,
            articles_skipped INT DEFAULT 0,
            topics_completed INT DEFAULT 0,
            tickers_completed INT DEFAULT 0,
            error_message TEXT,
            summary_message TEXT,
            extra_metrics JSONB
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_executions_started
        ON job_executions (started_at DESC)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_executions_name
        ON job_executions (job_name, started_at DESC)
        """
    )


def _mark_stale_running(cursor):
    cursor.execute(
        """
        UPDATE job_executions
        SET status = 'error',
            finished_at = CURRENT_TIMESTAMP,
            duration_seconds = EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)),
            error_message = 'Marked stale (process likely crashed)',
            summary_message = COALESCE(summary_message, '') || ' [stale]'
        WHERE status = 'running'
          AND started_at < (CURRENT_TIMESTAMP - make_interval(hours => %s))
        """,
        (STALE_RUNNING_HOURS,),
    )


def is_job_running(job_name):
    """True if a recent execution of this job is still marked running."""
    db = _db()
    with db:
        cur = db.conn.cursor()
        try:
            ensure_job_executions_table(cur)
            _mark_stale_running(cur)
            db.conn.commit()
            cur.execute(
                """
                SELECT 1 FROM job_executions
                WHERE job_name = %s AND status = 'running'
                LIMIT 1
                """,
                (job_name,),
            )
            return cur.fetchone() is not None
        finally:
            cur.close()


def start_execution(job_name, trigger_source, *, num_days=None, max_minutes=None, extra_metrics=None):
    """Insert a running job_executions row; return its id."""
    db = _db()
    with db:
        cur = db.conn.cursor()
        try:
            ensure_job_executions_table(cur)
            _mark_stale_running(cur)
            cur.execute(
                """
                INSERT INTO job_executions (
                    job_name, trigger_source, status, num_days, max_minutes, extra_metrics
                )
                VALUES (%s, %s, 'running', %s, %s, %s)
                RETURNING id
                """,
                (
                    job_name,
                    trigger_source,
                    num_days,
                    max_minutes,
                    json.dumps(extra_metrics) if extra_metrics else None,
                ),
            )
            exec_id = cur.fetchone()[0]
            db.conn.commit()
            logger.info(
                "Job %s started (id=%s, trigger=%s)", job_name, exec_id, trigger_source
            )
            return exec_id
        finally:
            cur.close()


def finish_execution(
    exec_id,
    status,
    *,
    articles_found=0,
    articles_inserted=0,
    articles_skipped=0,
    topics_completed=0,
    tickers_completed=0,
    summary_message='',
    error_message=None,
    extra_metrics=None,
):
    """Close out a job_executions row."""
    if not exec_id:
        return
    db = _db()
    with db:
        cur = db.conn.cursor()
        try:
            cur.execute(
                """
                UPDATE job_executions SET
                    finished_at = CURRENT_TIMESTAMP,
                    duration_seconds = EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)),
                    status = %s,
                    articles_found = %s,
                    articles_inserted = %s,
                    articles_skipped = %s,
                    topics_completed = %s,
                    tickers_completed = %s,
                    summary_message = %s,
                    error_message = %s,
                    extra_metrics = COALESCE(%s, extra_metrics)
                WHERE id = %s
                """,
                (
                    status,
                    articles_found,
                    articles_inserted,
                    articles_skipped,
                    topics_completed,
                    tickers_completed,
                    summary_message,
                    error_message,
                    json.dumps(extra_metrics) if extra_metrics else None,
                    exec_id,
                ),
            )
            db.conn.commit()
            logger.info("Job execution id=%s finished with status=%s", exec_id, status)
        finally:
            cur.close()


def list_executions(job_name=None, limit=100):
    """Return recent job executions newest first."""
    db = _db()
    with db:
        cur = db.conn.cursor()
        try:
            ensure_job_executions_table(cur)
            db.conn.commit()
            if job_name:
                cur.execute(
                    """
                    SELECT id, job_name, trigger_source, started_at, finished_at,
                           duration_seconds, status, num_days, max_minutes,
                           articles_found, articles_inserted, articles_skipped,
                           topics_completed, tickers_completed,
                           error_message, summary_message, extra_metrics
                    FROM job_executions
                    WHERE job_name = %s
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (job_name, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, job_name, trigger_source, started_at, finished_at,
                           duration_seconds, status, num_days, max_minutes,
                           articles_found, articles_inserted, articles_skipped,
                           topics_completed, tickers_completed,
                           error_message, summary_message, extra_metrics
                    FROM job_executions
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = []
            for r in cur.fetchall():
                rows.append({
                    'id': r[0],
                    'job_name': r[1],
                    'trigger_source': r[2],
                    'started_at': r[3].isoformat(sep=' ', timespec='seconds') if r[3] else None,
                    'finished_at': r[4].isoformat(sep=' ', timespec='seconds') if r[4] else None,
                    'duration_seconds': float(r[5]) if r[5] is not None else None,
                    'status': r[6],
                    'num_days': r[7],
                    'max_minutes': r[8],
                    'articles_found': r[9] or 0,
                    'articles_inserted': r[10] or 0,
                    'articles_skipped': r[11] or 0,
                    'topics_completed': r[12] or 0,
                    'tickers_completed': r[13] or 0,
                    'error_message': r[14],
                    'summary_message': r[15],
                    'extra_metrics': r[16],
                })
            return rows
        finally:
            cur.close()
