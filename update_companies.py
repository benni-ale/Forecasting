#!/usr/bin/env python3
"""Daily job: fill in missing company descriptions for tickers seen in articles.

Finds tickers that appear in the articles table but don't yet have a usable
business_description in the companies table, then fetches their profile from
Alpha Vantage (first) or Financial Modeling Prep (fallback) and upserts it.

Intended to run on Heroku Scheduler:  python update_companies.py

Env vars:
  ALPHA_VANTAGE_API_KEY / FMP_API_KEY  - at least one required
  COMPANIES_MAX_PER_RUN   - max tickers to fetch per run (default 20).
                            Alpha Vantage free tier is ~25 requests/day, so keep
                            this low; the rest are picked up on later runs.
  COMPANIES_REQUEST_SLEEP - seconds to wait between API calls (default 1.0)
  DATABASE_URL            - used automatically by app.get_db_manager()
"""
import os
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("update_companies")

# Reuse the exact fetch/save logic already used by the admin UI.
from app import (
    get_db_manager,
    get_alpha_vantage_company_overview,
    get_fmp_company_profile,
    save_company_to_db,
)


def find_missing_tickers():
    """Return tickers present in articles but lacking a usable description."""
    db = get_db_manager()
    with db:
        if not db.conn:
            raise ConnectionError("Database connection not established")
        cur = db.conn.cursor()
        try:
            cur.execute(
                """
                SELECT DISTINCT jsonb_array_elements(ticker_sentiment)->>'ticker' AS ticker
                FROM articles
                WHERE ticker_sentiment IS NOT NULL
                  AND jsonb_array_length(ticker_sentiment) > 0
                ORDER BY ticker
                """
            )
            all_tickers = [r[0] for r in cur.fetchall() if r[0]]

            cur.execute(
                """
                SELECT ticker FROM companies
                WHERE business_description IS NOT NULL
                  AND TRIM(business_description) != ''
                  AND UPPER(TRIM(business_description)) NOT IN ('NO DESCRIPTION', 'NONE', 'N/A', 'NA', '-')
                  AND LENGTH(TRIM(business_description)) > 5
                """
            )
            have = {r[0] for r in cur.fetchall()}
        finally:
            cur.close()
    return [t for t in all_tickers if t not in have]


def main():
    av_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    fmp_key = os.getenv('FMP_API_KEY', '')
    if not av_key and not fmp_key:
        logger.error("No ALPHA_VANTAGE_API_KEY or FMP_API_KEY configured; nothing to do.")
        return

    cap = int(os.getenv('COMPANIES_MAX_PER_RUN', '20'))
    sleep_s = float(os.getenv('COMPANIES_REQUEST_SLEEP', '1.0'))

    missing = find_missing_tickers()
    logger.info("Found %s tickers without a usable description", len(missing))
    if not missing:
        return

    batch = missing[:cap]
    logger.info("Processing %s this run (cap=%s, %s left for next runs)",
                len(batch), cap, max(0, len(missing) - len(batch)))

    success = fail = 0
    for i, ticker in enumerate(batch, 1):
        try:
            data = None
            if av_key:
                data = get_alpha_vantage_company_overview(ticker, av_key)
            if not data and fmp_key:
                data = get_fmp_company_profile(ticker, fmp_key)

            if not data:
                logger.warning("[%s/%s] %s: no profile found", i, len(batch), ticker)
                fail += 1
            elif save_company_to_db(ticker, data):
                logger.info("[%s/%s] %s: saved", i, len(batch), ticker)
                success += 1
            else:
                logger.error("[%s/%s] %s: save failed", i, len(batch), ticker)
                fail += 1
        except Exception:
            logger.exception("[%s/%s] %s: error while fetching", i, len(batch), ticker)
            fail += 1

        if i < len(batch):
            time.sleep(sleep_s)

    logger.info("Done. success=%s failed=%s remaining=%s",
                success, fail, max(0, len(missing) - len(batch)))


if __name__ == "__main__":
    main()
