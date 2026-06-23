"""Coverage ingestion job — runnable from admin (thread) or Heroku Scheduler."""
import csv
import io
import json
import logging
import os
import time
from datetime import datetime, timedelta

import requests

from job_logging import finish_execution, is_job_running, start_execution
from news_collector import AlphaVantageNewsCollector, DatabaseManager

logger = logging.getLogger(__name__)

COVERAGE_TOPICS = [
    'blockchain', 'earnings', 'ipo', 'mergers_and_acquisitions',
    'financial_markets', 'economy_fiscal', 'economy_monetary',
    'economy_macro', 'energy_transportation', 'finance',
    'life_sciences', 'manufacturing', 'real_estate',
    'retail_wholesale', 'technology',
]

COVERAGE_PROGRESS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'coverage_progress.json'
)


def _db():
    dsn = os.getenv('DATABASE_URL')
    if dsn and dsn.startswith('postgres://'):
        dsn = dsn.replace('postgres://', 'postgresql://', 1)
    return DatabaseManager(dsn=dsn) if dsn else DatabaseManager()


def load_coverage_progress():
    try:
        if os.path.exists(COVERAGE_PROGRESS_FILE):
            with open(COVERAGE_PROGRESS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Could not load coverage progress: %s", e)
    return {}


def save_coverage_progress(data):
    try:
        with open(COVERAGE_PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning("Could not save coverage progress: %s", e)


def clear_coverage_progress():
    try:
        if os.path.exists(COVERAGE_PROGRESS_FILE):
            os.remove(COVERAGE_PROGRESS_FILE)
    except Exception as e:
        logger.warning("Could not clear coverage progress: %s", e)


def fetch_active_stocks(api_key):
    """Fetch active stock symbols from Alpha Vantage LISTING_STATUS."""
    listing_params = {"function": "LISTING_STATUS", "apikey": api_key, "datatype": "csv"}
    try:
        resp = requests.get(
            "https://www.alphavantage.co/query", params=listing_params, timeout=120
        )
        resp.raise_for_status()
        text = resp.text.strip()
        stocks = []
        if ',' in text and ('symbol' in text.lower() or text.count(',') > 5):
            try:
                reader = csv.DictReader(io.StringIO(text))
                for row in reader:
                    symbol = row.get('symbol', '').strip().upper()
                    status = row.get('status', '').strip().lower()
                    if symbol and status == 'active':
                        stocks.append(symbol)
            except Exception as csv_error:
                logger.warning("Failed to parse listing CSV: %s", csv_error)
        if not stocks:
            try:
                data = resp.json()
                if isinstance(data, list):
                    stocks = [
                        e.get('symbol', '').upper().strip() for e in data
                        if e.get('symbol') and e.get('status', '').lower() == 'active'
                    ]
                elif 'data' in data:
                    stocks = [
                        e.get('symbol', '').upper().strip() for e in data['data']
                        if e.get('symbol') and e.get('status', '').lower() == 'active'
                    ]
            except (ValueError, json.JSONDecodeError):
                logger.error("Could not parse LISTING_STATUS. Preview: %s", text[:300])
        return list(set(s for s in stocks if s))
    except Exception as e:
        logger.error("Error fetching stock list: %s", exc_info=True)
        return []


def run_coverage_ingestion(
    num_days=7,
    max_minutes=0,
    api_key=None,
    trigger_source='manual',
    status_dict=None,
    should_continue=None,
):
    """Run exhaustive coverage ingestion. Blocks until done/stopped/timeout.

    status_dict: optional mutable dict updated for live UI (admin dashboard).
    should_continue: callable returning False to stop (manual stop button).
    Returns result dict with counters and status.
    """
    api_key = (api_key or os.getenv('ALPHA_VANTAGE_API_KEY', '')).strip()
    if not api_key:
        raise ValueError('ALPHA_VANTAGE_API_KEY is required')

    if is_job_running('coverage_ingestion'):
        raise RuntimeError('Coverage ingestion already running (see Job Executions)')

    if status_dict is None:
        status_dict = {}
    if should_continue is None:
        should_continue = lambda: True

    exec_id = start_execution(
        'coverage_ingestion',
        trigger_source,
        num_days=num_days,
        max_minutes=max_minutes if max_minutes else None,
    )

    counters = {'found': 0, 'inserted': 0, 'skipped': 0}
    seen_urls = set()
    completed_topics = set()
    completed_tickers = set()
    final_status = 'error'
    summary = ''
    error_msg = None

    status_dict.update({
        'running': True,
        'started_at': datetime.now().isoformat(),
        'phase': None,
        'current_target': None,
        'targets_completed': 0,
        'targets_total': 0,
        'num_days': num_days,
        'total_found': 0,
        'total_inserted': 0,
        'total_skipped': 0,
        'per_date_found': {},
        'per_date_inserted': {},
        'message': 'Starting coverage ingestion...',
        'execution_id': exec_id,
    })

    progress = load_coverage_progress()
    if progress.get('num_days') == num_days:
        completed_topics = set(progress.get('completed_topics', []))
        completed_tickers = set(progress.get('completed_tickers', []))
    status_dict['resumed'] = bool(completed_topics or completed_tickers)
    status_dict['resumed_topics'] = len(completed_topics)
    status_dict['resumed_tickers'] = len(completed_tickers)

    def persist_progress():
        save_coverage_progress({
            'num_days': num_days,
            'completed_topics': sorted(completed_topics),
            'completed_tickers': sorted(completed_tickers),
            'updated_at': datetime.now().isoformat(),
        })

    deadline = None
    if max_minutes and max_minutes > 0:
        deadline = datetime.now() + timedelta(minutes=max_minutes)

    def time_left():
        return should_continue() and (deadline is None or datetime.now() < deadline)

    try:
        rate_limit = int(os.getenv('ALPHA_VANTAGE_RATE_LIMIT', '75'))
        collector = AlphaVantageNewsCollector(api_key, rate_limit_per_minute=rate_limit)
        db_manager = _db()

        window_end = datetime.now()
        window_start = window_end - timedelta(days=num_days)
        max_depth = 6
        saturation = 1000

        def record_and_save(new_articles):
            if not new_articles:
                return
            by_day = {}
            for art in new_articles:
                day_raw = (art.get('time_published') or '')[:8]
                if len(day_raw) == 8:
                    day = f"{day_raw[:4]}-{day_raw[4:6]}-{day_raw[6:8]}"
                else:
                    day = 'unknown'
                by_day.setdefault(day, []).append(art)

            for day, day_articles in by_day.items():
                status_dict.setdefault('per_date_found', {})
                status_dict.setdefault('per_date_inserted', {})
                status_dict['per_date_found'][day] = (
                    status_dict['per_date_found'].get(day, 0) + len(day_articles)
                )
                counters['found'] += len(day_articles)
                try:
                    with db_manager:
                        result = db_manager.save_articles(day_articles)
                    status_dict['per_date_inserted'][day] = (
                        status_dict['per_date_inserted'].get(day, 0) + result['inserted']
                    )
                    counters['inserted'] += result['inserted']
                    counters['skipped'] += result['skipped']
                except Exception as e:
                    logger.error("Error saving articles for %s: %s", day, e, exc_info=True)

            status_dict['total_found'] = counters['found']
            status_dict['total_inserted'] = counters['inserted']
            status_dict['total_skipped'] = counters['skipped']

        def fetch_window(tickers, topics, win_from, win_to, depth):
            if not time_left():
                return
            win_from_str = win_from.strftime('%Y%m%dT%H%M')
            win_to_str = win_to.strftime('%Y%m%dT%H%M')
            saturated = False

            for sort_order in ('LATEST', 'EARLIEST'):
                if not time_left():
                    return
                try:
                    ticker_to_use = tickers
                    try:
                        data = collector._single_request(
                            tickers=ticker_to_use, topics=topics,
                            time_from=win_from_str, time_to=win_to_str,
                            limit=saturation, sort=sort_order,
                        )
                    except ValueError as e:
                        if tickers and "Invalid ticker format" in str(e) and "-" in tickers:
                            ticker_to_use = tickers.replace("-", "_")
                            data = collector._single_request(
                                tickers=ticker_to_use, topics=topics,
                                time_from=win_from_str, time_to=win_to_str,
                                limit=saturation, sort=sort_order,
                            )
                        else:
                            raise

                    articles = data.get('feed', []) if data else []
                    if len(articles) >= saturation:
                        saturated = True

                    new_articles = []
                    for art in articles:
                        url = art.get('url')
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            new_articles.append(art)
                    record_and_save(new_articles)
                except Exception as e:
                    logger.error(
                        "Coverage window error %s-%s tickers=%s topics=%s: %s",
                        win_from_str, win_to_str, tickers, topics, e, exc_info=True,
                    )
                if time_left():
                    time.sleep(collector.request_delay)

            if (saturated and depth < max_depth
                    and (win_to - win_from) > timedelta(days=1) and time_left()):
                mid = win_from + (win_to - win_from) / 2
                fetch_window(tickers, topics, win_from, mid, depth + 1)
                fetch_window(tickers, topics, mid, win_to, depth + 1)

        # Phase 1: topics
        status_dict['phase'] = 'topics'
        status_dict['targets_total'] = len(COVERAGE_TOPICS)
        status_dict['targets_completed'] = len(
            [t for t in COVERAGE_TOPICS if t in completed_topics]
        )
        for i, topic in enumerate(COVERAGE_TOPICS):
            if not time_left():
                break
            if topic in completed_topics:
                continue
            status_dict['current_target'] = topic
            status_dict['message'] = f'Phase 1/2 (topics): {topic} ({i + 1}/{len(COVERAGE_TOPICS)})'
            fetch_window(None, topic, window_start, window_end, 0)
            if time_left():
                completed_topics.add(topic)
                persist_progress()
            status_dict['targets_completed'] = len(completed_topics)

        # Phase 2: tickers
        if time_left():
            status_dict['phase'] = 'tickers'
            status_dict['message'] = 'Fetching list of all stocks from Alpha Vantage...'
            stocks = fetch_active_stocks(api_key)
            if not stocks:
                status_dict['message'] = 'Topics done. Could not fetch ticker list; skipping ticker phase.'
                logger.warning("No tickers available, skipping phase 2")
            else:
                status_dict['targets_total'] = len(stocks)
                status_dict['targets_completed'] = len(
                    [s for s in stocks if s in completed_tickers]
                )
                for i, ticker in enumerate(stocks):
                    if not time_left():
                        break
                    if ticker in completed_tickers:
                        continue
                    status_dict['current_target'] = ticker
                    status_dict['message'] = (
                        f'Phase 2/2 (tickers): {ticker} ({i + 1}/{len(stocks)})'
                    )
                    fetch_window(ticker, None, window_start, window_end, 0)
                    if time_left():
                        completed_tickers.add(ticker)
                        if len(completed_tickers) % 10 == 0:
                            persist_progress()
                    status_dict['targets_completed'] = len(completed_tickers)
                persist_progress()

        stopped = not should_continue()
        timed_out = deadline is not None and datetime.now() >= deadline
        fully_complete = not stopped and not timed_out

        if fully_complete:
            clear_coverage_progress()
            resume_note = ''
            final_status = 'completed'
            prefix = 'Complete'
        elif stopped:
            persist_progress()
            resume_note = ' | Progress saved — restart to resume'
            final_status = 'stopped'
            prefix = 'Stopped'
        else:
            persist_progress()
            resume_note = ' | Time budget reached — restart to resume'
            final_status = 'timeout'
            prefix = 'Time budget reached'

        summary = (
            f'{prefix}! Coverage of last {num_days} day(s): '
            f'{counters["found"]} articles found, '
            f'{counters["inserted"]} new inserted, '
            f'{counters["skipped"]} duplicates skipped'
            f'{resume_note}'
        )
        status_dict['message'] = summary
        logger.info(
            "Coverage ingestion finished (%s): found=%s inserted=%s skipped=%s "
            "topics=%s tickers=%s",
            prefix, counters['found'], counters['inserted'], counters['skipped'],
            len(completed_topics), len(completed_tickers),
        )

    except Exception as e:
        error_msg = str(e)
        final_status = 'error'
        summary = f'Error: {e}'
        status_dict['message'] = summary
        logger.error("Coverage ingestion failed: %s", e, exc_info=True)
        persist_progress()
        raise
    finally:
        status_dict['running'] = False
        status_dict['current_target'] = None
        finish_execution(
            exec_id,
            final_status,
            articles_found=counters['found'],
            articles_inserted=counters['inserted'],
            articles_skipped=counters['skipped'],
            topics_completed=len(completed_topics),
            tickers_completed=len(completed_tickers),
            summary_message=summary,
            error_message=error_msg,
        )

    return {
        'status': final_status,
        'found': counters['found'],
        'inserted': counters['inserted'],
        'skipped': counters['skipped'],
        'topics_completed': len(completed_topics),
        'tickers_completed': len(completed_tickers),
        'execution_id': exec_id,
        'message': summary,
    }
