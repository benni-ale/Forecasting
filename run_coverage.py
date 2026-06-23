#!/usr/bin/env python3
"""Run coverage ingestion (Heroku Scheduler: python run_coverage.py).

Env vars:
  ALPHA_VANTAGE_API_KEY  - required
  COVERAGE_NUM_DAYS      - days to cover (default 7, max 30)
  COVERAGE_MAX_MINUTES   - time budget per run (default 120; 0 = unlimited)
  DATABASE_URL           - Postgres connection
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('run_coverage')


def main():
    from job_logging import is_job_running
    from coverage_ingestion_job import run_coverage_ingestion

    if is_job_running('coverage_ingestion'):
        logger.warning('Coverage ingestion already running; skipping scheduled run.')
        return

    num_days = int(os.getenv('COVERAGE_NUM_DAYS', '7'))
    num_days = max(1, min(num_days, 30))
    max_minutes = int(os.getenv('COVERAGE_MAX_MINUTES', '120'))
    max_minutes = max(0, min(max_minutes, 1440))

    logger.info(
        'Starting scheduled coverage: num_days=%s max_minutes=%s',
        num_days, max_minutes or 'unlimited',
    )
    try:
        result = run_coverage_ingestion(
            num_days=num_days,
            max_minutes=max_minutes,
            trigger_source='scheduler',
        )
        logger.info('Scheduled coverage finished: %s', result.get('message'))
    except RuntimeError as e:
        logger.warning('%s', e)
    except Exception:
        logger.exception('Scheduled coverage failed')
        raise


if __name__ == '__main__':
    main()
