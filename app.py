#!/usr/bin/env python3
"""
Flask Web Dashboard for Financial News Collector
Provides a GUI to collect news and visualize collected articles.
"""

import os
import json
import logging
import math
import requests
import subprocess
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from news_collector import AlphaVantageNewsCollector, DatabaseManager
from werkzeug.security import generate_password_hash, check_password_hash
import threading
import time
from functools import wraps
from dotenv import load_dotenv

# OpenAI imports for embeddings
try:
    import openai
    import tiktoken
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    logger.warning("OpenAI or tiktoken not installed. Embedding features will be disabled.")

# Configure logging first
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    force=True  # Force reconfiguration
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
env_loaded = load_dotenv()
if env_loaded:
    logger.info("Successfully loaded .env file")
    # Log which API key is loaded (masked for security)
    api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    if api_key:
        logger.info(f"API key found in .env file (first 8 chars: {api_key[:8]}...)")
    else:
        logger.warning("ALPHA_VANTAGE_API_KEY not found in .env file")
else:
    logger.warning("No .env file found or failed to load")

# Configure Flask logging
logging.getLogger('werkzeug').setLevel(logging.WARNING)  # Reduce Flask request logs

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')

# ---------------------------------------------------------------------------
# Authentication / access control
# ---------------------------------------------------------------------------
# When ADMIN_PASSWORD is set (e.g. in production on Heroku), the app locks down:
# anonymous visitors can ONLY reach the public read-only dashboard + login page,
# while every other page/route requires an authenticated admin session.
# When ADMIN_PASSWORD is NOT set (local development), everything stays open as
# before, so the existing local workflow is unchanged.
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
AUTH_ENABLED = bool(ADMIN_PASSWORD)

# Endpoints reachable without an admin session (everything else is admin-only
# when AUTH_ENABLED). 'static' is needed for assets; the others are the public
# dashboard and the login/logout flow.
PUBLIC_ENDPOINTS = {
    'public_dashboard', 'api_dashboard_stocks',
    'view_articles', 'api_articles', 'view_article_detail',
    'public_companies', 'public_ticker_detail', 'api_ticker_sentiment_kpi', 'api_ticker_technicals', 'get_companies',
    'about',
    'login', 'logout', 'static',
    'account_login', 'account_logout',
}

# Endpoints reachable by any logged-in *user* (not just admin). Used for the
# personal holdings tracker: they need a user session but not admin rights.
USER_ENDPOINTS = {
    'holdings_page',
    'api_holdings_list', 'api_holdings_create',
    'api_holdings_update', 'api_holdings_delete',
}


# Shared defaults for the public sentiment KPI (daily dashboard + ticker chart).
DEFAULT_KPI_WINDOW_DAYS = 14
DEFAULT_KPI_HALF_LIFE = 7.0
DEFAULT_MIN_MENTIONS = 10


def is_admin():
    """True if the current session is an authenticated admin (or auth is off)."""
    return (not AUTH_ENABLED) or bool(session.get('is_admin'))


@app.context_processor
def inject_auth():
    """Expose auth state to all templates (used to hide admin-only nav links)."""
    user = None
    if session.get('user_id'):
        user = {
            'id': session.get('user_id'),
            'email': session.get('user_email'),
            'display_name': session.get('user_name') or session.get('user_email'),
        }
    elif not AUTH_ENABLED:
        # Local dev: a stable implicit user so the holdings tracker just works.
        user = {'id': None, 'email': 'dev@local', 'display_name': 'Dev'}
    return {'is_admin': is_admin(), 'auth_enabled': AUTH_ENABLED, 'current_user': user}


@app.before_request
def _enforce_access_control():
    """Gate non-public endpoints. Admin endpoints need an admin session; the
    personal holdings endpoints need any logged-in user; the rest is admin-only.
    """
    if not AUTH_ENABLED:
        return  # local/dev: no restrictions
    ep = request.endpoint
    if ep in PUBLIC_ENDPOINTS:
        return
    if session.get('is_admin'):
        return
    # User-level pages: a plain user session is enough.
    if ep in USER_ENDPOINTS and session.get('user_id'):
        return
    # Not authorized -> block (JSON 401 for APIs, redirect to the right login).
    if request.path.startswith('/api/'):
        return jsonify({'error': 'unauthorized', 'message': 'Login required.'}), 401
    if ep in USER_ENDPOINTS:
        return redirect(url_for('account_login', next=request.path))
    return redirect(url_for('login', next=request.path))


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Admin login. Only relevant when ADMIN_PASSWORD is configured."""
    if not AUTH_ENABLED:
        # No password configured -> nothing to log into.
        return redirect(url_for('public_dashboard'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        if password and password == ADMIN_PASSWORD:
            session['is_admin'] = True
            session.permanent = True
            next_url = request.args.get('next') or request.form.get('next') or url_for('index')
            # Avoid open-redirects: only allow same-site relative paths.
            if not next_url.startswith('/'):
                next_url = url_for('index')
            logger.info("Admin login successful")
            return redirect(next_url)
        logger.warning("Admin login failed (wrong password)")
        flash('Incorrect password.', 'error')

    return render_template('login.html', next=request.args.get('next', ''))


@app.route('/logout')
def logout():
    """Clear the admin session."""
    session.pop('is_admin', None)
    flash('Logged out.', 'info')
    return redirect(url_for('public_dashboard'))


# ---------------------------------------------------------------------------
# Multi-user accounts (invite-only) + per-user holdings
# ---------------------------------------------------------------------------
# Admins create accounts from /admin/users; users log in at /account/login and
# manage their own holdings (with buy price/date) at /holdings. Passwords are
# stored as salted hashes via werkzeug.
_USER_TABLES_READY = False
_DEV_USER_ID = None


def _ensure_user_tables():
    """Create users + user_holdings tables if missing (idempotent, run once)."""
    global _USER_TABLES_READY
    if _USER_TABLES_READY:
        return
    db = get_db_manager()
    with db:
        cur = db.conn.cursor()
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    display_name TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_holdings (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    ticker TEXT NOT NULL,
                    quantity NUMERIC NOT NULL,
                    buy_price NUMERIC NOT NULL,
                    buy_currency TEXT DEFAULT 'USD',
                    buy_date DATE,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_user_holdings_user ON user_holdings(user_id);")
            db.conn.commit()
            _USER_TABLES_READY = True
        finally:
            cur.close()


def _dev_user_id():
    """Resolve (creating once) a stable 'dev@local' user for local dev where
    ADMIN_PASSWORD is unset and there is no login flow."""
    global _DEV_USER_ID
    if _DEV_USER_ID is not None:
        return _DEV_USER_ID
    _ensure_user_tables()
    db = get_db_manager()
    with db:
        cur = db.conn.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE email = %s", ('dev@local',))
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO users (email, password_hash, display_name) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    ('dev@local', generate_password_hash('dev'), 'Dev User')
                )
                row = cur.fetchone()
                db.conn.commit()
            _DEV_USER_ID = row[0]
        finally:
            cur.close()
    return _DEV_USER_ID


def current_user_id():
    """Logged-in user's id, or the implicit dev user when auth is disabled."""
    uid = session.get('user_id')
    if uid:
        return uid
    if not AUTH_ENABLED:
        try:
            return _dev_user_id()
        except Exception as e:
            logger.warning(f"Could not resolve dev user: {e}")
    return None


@app.route('/account/login', methods=['GET', 'POST'])
def account_login():
    """User login (accounts are created by an admin)."""
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        _ensure_user_tables()
        db = get_db_manager(readonly=True)
        row = None
        with db:
            cur = db.conn.cursor()
            try:
                cur.execute(
                    "SELECT id, email, display_name, password_hash, is_active "
                    "FROM users WHERE email = %s", (email,)
                )
                row = cur.fetchone()
            finally:
                cur.close()
        if row and row[4] and check_password_hash(row[3], password):
            session['user_id'] = row[0]
            session['user_email'] = row[1]
            session['user_name'] = row[2] or row[1]
            session.permanent = True
            logger.info(f"User login: {email}")
            next_url = request.args.get('next') or request.form.get('next') or url_for('holdings_page')
            if not next_url.startswith('/'):
                next_url = url_for('holdings_page')
            return redirect(next_url)
        logger.warning(f"User login failed: {email}")
        flash('Email o password non validi.', 'error')
    return render_template('account_login.html', next=request.args.get('next', ''))


@app.route('/account/logout')
def account_logout():
    """Clear the user session."""
    for k in ('user_id', 'user_email', 'user_name'):
        session.pop(k, None)
    flash('Sei uscito dal tuo account.', 'info')
    return redirect(url_for('public_dashboard'))


@app.route('/admin/users', methods=['GET', 'POST'])
def admin_users():
    """Admin-only: list users and create new (invite-only) accounts."""
    _ensure_user_tables()
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        display_name = (request.form.get('display_name') or '').strip()
        password = request.form.get('password') or ''
        if not email or not password:
            flash('Email e password sono obbligatorie.', 'error')
        else:
            db = get_db_manager()
            try:
                with db:
                    cur = db.conn.cursor()
                    try:
                        cur.execute(
                            "INSERT INTO users (email, password_hash, display_name) "
                            "VALUES (%s, %s, %s)",
                            (email, generate_password_hash(password), display_name or None)
                        )
                        db.conn.commit()
                        flash(f'Utente {email} creato.', 'info')
                    finally:
                        cur.close()
            except Exception as e:
                logger.error(f"Error creating user {email}: {e}")
                flash(f'Errore nella creazione utente (email gia\' esistente?): {e}', 'error')
        return redirect(url_for('admin_users'))

    users = []
    db = get_db_manager(readonly=True)
    with db:
        cur = db.conn.cursor()
        try:
            cur.execute(
                "SELECT u.id, u.email, u.display_name, u.is_active, u.created_at, "
                "(SELECT COUNT(*) FROM user_holdings h WHERE h.user_id = u.id) "
                "FROM users u ORDER BY u.created_at DESC"
            )
            users = cur.fetchall()
        finally:
            cur.close()
    return render_template('admin_users.html', users=users)

# Global state for collection status
collection_status = {
    'running': False,
    'message': '',
    'last_run': None,
    'articles_collected': 0
}

# Global state for batch company fetch
batch_fetch_status = {
    'running': False,
    'message': '',
    'started_at': None,
    'total': 0,
    'processed': 0,
    'success': 0,
    'failed': 0,
    'current_ticker': None,
    'last_updated': None
}

# Flag to stop batch fetch process
batch_fetch_stop_requested = False

# Separate log for batch fetch process (visible in GUI)
batch_fetch_logs = []
MAX_BATCH_LOG_ENTRIES = 1000  # Keep last 1000 log entries

# Separate log for batch fetch process (visible in GUI)
batch_fetch_logs = []
MAX_BATCH_LOG_ENTRIES = 1000  # Keep last 1000 log entries

deep_ingestion_status = {
    'running': False,
    'message': 'Idle',
    'started_at': None,
    'total_articles': 0,
    'total_inserted': 0,
    'total_skipped': 0,
    'current_topic': None,
    'topics_completed': 0,
    'topics_total': 0,
    'current_day': None,
    'day_index': 0,
    'days_total': 0
}

stock_ingestion_status = {
    'running': False,
    'message': 'Idle',
    'started_at': None,
    'total_articles': 0,
    'total_inserted': 0,
    'total_skipped': 0,
    'current_ticker': None,
    'tickers_completed': 0,
    'tickers_total': 0
}

# Separate status for the multi-day variant of deep research, so it can run
# (and be tracked) independently from the classic deep research.
stock_ingestion_multiday_status = {
    'running': False,
    'message': 'Idle',
    'started_at': None,
    'total_articles': 0,
    'total_inserted': 0,
    'total_skipped': 0,
    'current_ticker': None,
    'tickers_completed': 0,
    'tickers_total': 0,
    'current_day': None,
    'day_index': 0,
    'days_total': 0
}

# Status for the exhaustive "Coverage Ingestion": maximizes article coverage of
# the last N days by querying every topic and every ticker with limit=1000,
# both LATEST and EARLIEST sort, recursively subdividing saturated windows.
# Tracks how many articles are found vs newly inserted, broken down by date.
coverage_ingestion_status = {
    'running': False,
    'message': 'Idle',
    'started_at': None,
    'phase': None,             # 'topics' | 'tickers'
    'current_target': None,    # current topic or ticker
    'targets_completed': 0,
    'targets_total': 0,
    'num_days': 0,
    'total_found': 0,
    'total_inserted': 0,
    'total_skipped': 0,
    'per_date_found': {},      # 'YYYY-MM-DD' -> articles seen from the API
    'per_date_inserted': {}    # 'YYYY-MM-DD' -> new articles actually saved
}


def _normalize_dsn(dsn):
    """Normalize a Postgres DSN (Heroku gives 'postgres://', psycopg2 prefers 'postgresql://')."""
    if dsn and dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql://", 1)
    return dsn


def get_db_manager(readonly=False):
    """Get database manager.

    Priority:
      1. DATABASE_URL / DATABASE_URL_RO (managed Postgres, e.g. Heroku) when present.
      2. Discrete DB_HOST/DB_PORT/... env vars (local development / docker-compose).

    Args:
        readonly: when True, prefer the read-only connection string
                  (DATABASE_URL_RO) so anonymous/public views cannot write.
    """
    dsn = None
    if readonly:
        dsn = os.getenv("DATABASE_URL_RO") or os.getenv("DATABASE_URL")
    else:
        dsn = os.getenv("DATABASE_URL")

    if dsn:
        return DatabaseManager(dsn=_normalize_dsn(dsn))

    return DatabaseManager(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "newsdb"),
        user=os.getenv("DB_USER", "newsuser"),
        password=os.getenv("DB_PASSWORD", "newspass")
    )


def add_batch_log(level, message):
    """Add a log entry to the batch fetch log (separate from main logging)."""
    global batch_fetch_logs
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = {
        'timestamp': timestamp,
        'level': level,
        'message': message
    }
    batch_fetch_logs.append(log_entry)
    # Keep only last MAX_BATCH_LOG_ENTRIES entries
    if len(batch_fetch_logs) > MAX_BATCH_LOG_ENTRIES:
        batch_fetch_logs = batch_fetch_logs[-MAX_BATCH_LOG_ENTRIES:]


@app.route('/dashboard')
def public_dashboard():
    """Public, read-only daily dashboard.

    Shows, over a recent window, the most-cited tickers and a relevance-weighted
    sentiment KPI with exponential time decay:
        KPI = SUM(sentiment * relevance * 0.5^(age_days / half_life))
            / SUM(relevance * 0.5^(age_days / half_life))
    Uses the read-only DB connection so it can never write.
    """
    # Parameters (with sane bounds).
    try:
        days = int(request.args.get('days', DEFAULT_KPI_WINDOW_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_KPI_WINDOW_DAYS
    days = max(1, min(days, 365))

    try:
        half_life = float(request.args.get('half_life', DEFAULT_KPI_HALF_LIFE))
    except (TypeError, ValueError):
        half_life = DEFAULT_KPI_HALF_LIFE
    half_life = max(0.5, min(half_life, 365.0))

    try:
        min_mentions = int(request.args.get('min_mentions', DEFAULT_MIN_MENTIONS))
    except (TypeError, ValueError):
        min_mentions = DEFAULT_MIN_MENTIONS
    min_mentions = max(1, min(min_mentions, 100000))

    ticker_filter = (request.args.get('ticker', '') or '').strip().upper()
    direction = (request.args.get('direction', 'all') or 'all').lower()
    if direction not in ('all', 'bullish', 'bearish'):
        direction = 'all'

    # "Last seen on/after" date filter (YYYY-MM-DD from the calendar picker).
    # When the param is absent (first visit, user hasn't touched it) we default
    # it later to the penultimate available date in the DB. An explicitly empty
    # value means the user cleared the filter and wants to see everything.
    last_seen_param = request.args.get('last_seen_from', None)
    last_seen_explicit = last_seen_param is not None
    last_seen_from = (last_seen_param or '').strip()
    try:
        if last_seen_from:
            datetime.strptime(last_seen_from, '%Y-%m-%d')
    except ValueError:
        last_seen_from = ''
        last_seen_explicit = True  # invalid value: don't override with default

    rows = []
    stats = {}
    error = None
    penultimate_last_seen = ''
    try:
        db_manager = get_db_manager(readonly=True)
        with db_manager:
            cursor = db_manager.conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT
                        ticker,
                        SUM(ticker_sentiment_score * relevance_score
                            * POWER(0.5, (CURRENT_DATE - article_time_published::date) / %(hl)s))
                          / NULLIF(SUM(relevance_score
                            * POWER(0.5, (CURRENT_DATE - article_time_published::date) / %(hl)s)), 0)
                            AS kpi,
                        MAX(article_time_published::date) AS last_seen,
                        COUNT(*) AS mentions
                    FROM article_ticker_sentiment_view
                    WHERE article_time_published::date > (CURRENT_DATE - make_interval(days => %(days)s))
                      AND (%(ticker)s = '' OR ticker ILIKE %(tickerlike)s)
                    GROUP BY ticker
                    HAVING COUNT(*) >= %(min_mentions)s
                    ORDER BY kpi DESC
                    """,
                    {
                        'hl': half_life,
                        'days': days,
                        'min_mentions': min_mentions,
                        'ticker': ticker_filter,
                        'tickerlike': f'%{ticker_filter}%',
                    }
                )
                for ticker, kpi, last_seen, mentions in cursor.fetchall():
                    rows.append({
                        'ticker': ticker,
                        'kpi': float(kpi) if kpi is not None else None,
                        'last_seen': last_seen.isoformat() if last_seen else None,
                        'mentions': int(mentions),
                    })

                # Overall + window statistics (same read-only cursor).
                stats = get_statistics_from_db(cursor)
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS articles_in_window,
                        COUNT(DISTINCT source) AS sources_in_window
                    FROM articles
                    WHERE time_published > (CURRENT_DATE - make_interval(days => %(days)s))
                    """,
                    {'days': days}
                )
                win = cursor.fetchone()
                stats['articles_in_window'] = int(win[0]) if win else 0
                stats['sources_in_window'] = int(win[1]) if win else 0

                # Two most recent distinct dates on which any ticker was seen,
                # to default the "last seen" filter to the penultimate one.
                cursor.execute(
                    """
                    SELECT DISTINCT article_time_published::date AS d
                    FROM article_ticker_sentiment_view
                    WHERE article_time_published::date
                          > (CURRENT_DATE - make_interval(days => %(days)s))
                    ORDER BY d DESC
                    LIMIT 2
                    """,
                    {'days': days}
                )
                date_rows = [r[0] for r in cursor.fetchall()]
                if len(date_rows) >= 2:
                    penultimate_last_seen = date_rows[1].isoformat()
                elif date_rows:
                    penultimate_last_seen = date_rows[0].isoformat()
            finally:
                cursor.close()
    except Exception as e:
        logger.error(f"Error building public dashboard: {str(e)}", exc_info=True)
        error = str(e)

    # Default the "last seen" filter to the penultimate available date when the
    # user hasn't explicitly chosen one.
    if not last_seen_explicit and penultimate_last_seen:
        last_seen_from = penultimate_last_seen

    # Apply sentiment-direction filter (kpi sign).
    if direction == 'bullish':
        rows = [r for r in rows if r['kpi'] is not None and r['kpi'] > 0]
    elif direction == 'bearish':
        rows = [r for r in rows if r['kpi'] is not None and r['kpi'] < 0]

    # Apply "last seen on/after" filter (ISO date strings compare lexicographically).
    if last_seen_from:
        rows = [r for r in rows if r['last_seen'] and r['last_seen'] >= last_seen_from]

    stats['tickers_in_window'] = len(rows)
    # The dashboard shows one chart per ticker; both orderings carry the same
    # per-ticker info (kpi, mentions, last_seen) so the front-end can switch
    # between them without refetching.
    # "Best sentiment" = ordered by KPI (already from SQL).
    by_sentiment = [r for r in rows if r['kpi'] is not None][:10]
    # "Most cited" = same data re-sorted by number of mentions.
    by_mentions = sorted(rows, key=lambda r: r['mentions'], reverse=True)[:10]

    return render_template(
        'dashboard.html',
        by_sentiment=by_sentiment,
        by_mentions=by_mentions,
        stats=stats,
        days=days,
        half_life=half_life,
        min_mentions=min_mentions,
        ticker_filter=ticker_filter,
        direction=direction,
        last_seen_from=last_seen_from,
        error=error,
        is_admin=is_admin(),
        auth_enabled=AUTH_ENABLED,
    )


# Daily prices (close + volume) are fetched lazily from Alpha Vantage and
# persisted in the stock_prices table. A per-ticker throttle avoids hitting the
# API more than once per refresh window (free tier is ~25 requests/day).
_price_fetch_attempts = {}
_price_fetch_lock = threading.Lock()
STOCK_PRICE_REFRESH_TTL = int(os.getenv('STOCK_PRICE_REFRESH_TTL', '3600'))  # seconds


def _ensure_price_table(cursor):
    """Create the stock_prices table if it doesn't exist (write cursor)."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_prices (
            ticker TEXT NOT NULL,
            price_date DATE NOT NULL,
            close NUMERIC,
            volume BIGINT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, price_date)
        )
        """
    )


def _load_prices_from_db(ticker, points):
    """Return {ticker, dates, closes, volumes} from stock_prices, or None."""
    try:
        db = get_db_manager(readonly=True)
        with db:
            cur = db.conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT price_date, close, volume
                    FROM stock_prices
                    WHERE ticker = %s
                    ORDER BY price_date DESC
                    LIMIT %s
                    """,
                    (ticker, points)
                )
                rows = cur.fetchall()
            finally:
                cur.close()
    except Exception as e:
        logger.warning(f"Could not read stored prices for {ticker}: {e}")
        return None
    if not rows:
        return None
    rows = list(reversed(rows))  # ascending by date for charting
    return {
        'ticker': ticker,
        'dates': [r[0].isoformat() for r in rows],
        'closes': [float(r[1]) if r[1] is not None else None for r in rows],
        'volumes': [int(r[2]) if r[2] is not None else 0 for r in rows],
    }


def _maybe_refresh_prices(ticker):
    """Fetch fresh daily prices from Alpha Vantage and upsert into stock_prices.

    Throttled per ticker (STOCK_PRICE_REFRESH_TTL) so repeated page loads don't
    burn the API quota. Safe to call on every request.
    """
    api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    if not api_key:
        return

    now = time.time()
    with _price_fetch_lock:
        last = _price_fetch_attempts.get(ticker)
        if last and (now - last) < STOCK_PRICE_REFRESH_TTL:
            return
        _price_fetch_attempts[ticker] = now

    try:
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": ticker,
            "apikey": api_key,
            "outputsize": "compact",  # last 100 days
        }
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if "Error Message" in data or "Note" in data or "Information" in data:
            logger.info(
                f"AV price refresh for {ticker} returned no data: "
                f"{data.get('Error Message') or data.get('Note') or data.get('Information')}"
            )
            return

        series = data.get("Time Series (Daily)")
        if not series:
            return

        rows = [
            (ticker, d, float(v['4. close']), int(float(v['5. volume'])))
            for d, v in series.items()
        ]
        db = get_db_manager()
        with db:
            if not db.conn:
                return
            cur = db.conn.cursor()
            try:
                _ensure_price_table(cur)
                cur.executemany(
                    """
                    INSERT INTO stock_prices (ticker, price_date, close, volume)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (ticker, price_date) DO UPDATE SET
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    rows
                )
                db.conn.commit()
                logger.info(f"Stored {len(rows)} price rows for {ticker}")
            finally:
                cur.close()
    except Exception as e:
        logger.warning(f"Price refresh failed for {ticker}: {e}")


def _fetch_av_daily(ticker, points=60):
    """Return daily close + volume for a ticker, persisted in the DB.

    Refreshes from Alpha Vantage when stale (throttled), then serves from the
    stock_prices table. Returns {ticker, dates, closes, volumes} or {ticker, error}.
    """
    ticker = ticker.upper()
    _maybe_refresh_prices(ticker)
    result = _load_prices_from_db(ticker, points)
    if result and result.get('dates'):
        return result
    if not os.getenv('ALPHA_VANTAGE_API_KEY', ''):
        return {'ticker': ticker, 'error': 'API key not configured'}
    return {'ticker': ticker, 'error': 'No daily data available'}


@app.route('/api/dashboard/stocks')
def api_dashboard_stocks():
    """Public endpoint: daily close + volume for the top-N tickers by sentiment KPI.

    Top tickers are ranked by the relevance-weighted, time-decayed sentiment KPI
    (same as the dashboard) from the read-only DB; prices come from Alpha Vantage
    (cached in memory). Used by the public dashboard chart section.
    """
    try:
        days = int(request.args.get('days', DEFAULT_KPI_WINDOW_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_KPI_WINDOW_DAYS
    days = max(1, min(days, 365))

    try:
        half_life = float(request.args.get('half_life', DEFAULT_KPI_HALF_LIFE))
    except (TypeError, ValueError):
        half_life = DEFAULT_KPI_HALF_LIFE
    half_life = max(0.5, min(half_life, 365.0))

    try:
        min_mentions = int(request.args.get('min_mentions', DEFAULT_MIN_MENTIONS))
    except (TypeError, ValueError):
        min_mentions = DEFAULT_MIN_MENTIONS
    min_mentions = max(1, min(min_mentions, 100000))

    try:
        top = int(request.args.get('top', 10))
    except (TypeError, ValueError):
        top = 10
    top = max(1, min(top, 10))

    # Explicit ticker list (from the filtered dashboard table) takes precedence.
    explicit = (request.args.get('tickers', '') or '').strip()
    if explicit:
        tickers = [t.strip().upper() for t in explicit.split(',') if t.strip()][:top]
        series = [_fetch_av_daily(t) for t in tickers]
        return jsonify({'tickers': tickers, 'series': series})

    tickers = []
    try:
        db_manager = get_db_manager(readonly=True)
        with db_manager:
            cursor = db_manager.conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT
                        ticker,
                        SUM(ticker_sentiment_score * relevance_score
                            * POWER(0.5, (CURRENT_DATE - article_time_published::date) / %(hl)s))
                          / NULLIF(SUM(relevance_score
                            * POWER(0.5, (CURRENT_DATE - article_time_published::date) / %(hl)s)), 0)
                            AS kpi
                    FROM article_ticker_sentiment_view
                    WHERE article_time_published::date > (CURRENT_DATE - make_interval(days => %(days)s))
                    GROUP BY ticker
                    HAVING COUNT(*) >= %(min_mentions)s
                    ORDER BY kpi DESC NULLS LAST
                    LIMIT %(top)s
                    """,
                    {'hl': half_life, 'days': days, 'min_mentions': min_mentions, 'top': top}
                )
                tickers = [r[0] for r in cursor.fetchall()]
            finally:
                cursor.close()
    except Exception as e:
        logger.error(f"Error fetching top tickers for stocks chart: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

    series = [_fetch_av_daily(t) for t in tickers]
    return jsonify({'tickers': tickers, 'series': series})


# Ticker-detail sentiment chart uses the same KPI defaults as the dashboard.
def _compute_ticker_kpi_series(ticker, window_days=DEFAULT_KPI_WINDOW_DAYS,
                               half_life=DEFAULT_KPI_HALF_LIFE,
                               chart_days=DEFAULT_KPI_WINDOW_DAYS):
    """Compute the dashboard KPI formula for one ticker on each calendar day.

    For each target date T the KPI uses articles with
        article_date > T - window_days  AND  article_date <= T
    with decay POWER(0.5, (T - article_date) / half_life) — identical to the
    public dashboard query when T = CURRENT_DATE.

    Returns list of dicts {date, kpi, mentions} ordered ascending by date.
    """
    ticker = ticker.upper()
    lookback = window_days + chart_days  # articles needed for the rolling window

    db = get_db_manager(readonly=True)
    with db:
        cur = db.conn.cursor()
        try:
            cur.execute(
                """
                WITH article_rows AS (
                    SELECT
                        article_time_published::date AS d,
                        ticker_sentiment_score AS s,
                        relevance_score AS rel
                    FROM article_ticker_sentiment_view
                    WHERE ticker = %(ticker)s
                      AND article_time_published::date
                          > (CURRENT_DATE - make_interval(days => %(lookback)s))
                ),
                days AS (
                    SELECT generate_series(
                        (CURRENT_DATE - make_interval(days => %(chart_days)s - 1))::date,
                        CURRENT_DATE,
                        INTERVAL '1 day'
                    )::date AS target_date
                ),
                daily_counts AS (
                    SELECT d, COUNT(*)::int AS cnt
                    FROM article_rows
                    GROUP BY d
                )
                SELECT
                    days.target_date,
                    k.kpi,
                    COALESCE(dc.cnt, 0) AS mentions
                FROM days
                LEFT JOIN LATERAL (
                    SELECT
                        SUM(ar.s * ar.rel
                            * POWER(0.5, (days.target_date - ar.d)::numeric / %(hl)s))
                          / NULLIF(SUM(ar.rel
                            * POWER(0.5, (days.target_date - ar.d)::numeric / %(hl)s)), 0)
                            AS kpi
                    FROM article_rows ar
                    WHERE ar.d > (days.target_date - make_interval(days => %(window)s))
                      AND ar.d <= days.target_date
                ) k ON TRUE
                LEFT JOIN daily_counts dc ON dc.d = days.target_date
                ORDER BY days.target_date
                """,
                {
                    'ticker': ticker,
                    'lookback': lookback,
                    'chart_days': chart_days,
                    'window': window_days,
                    'hl': half_life,
                }
            )
            rows = []
            for target_date, kpi, mentions in cur.fetchall():
                rows.append({
                    'date': target_date.isoformat(),
                    'kpi': float(kpi) if kpi is not None else None,
                    'mentions': int(mentions),
                })
            return rows
        finally:
            cur.close()


@app.route('/api/tickers/<ticker>/sentiment-kpi')
def api_ticker_sentiment_kpi(ticker):
    """Public, lazy endpoint: dashboard KPI time series for one ticker.

    Uses the same default window/half-life as the daily dashboard (14d / 7d).
    The last point matches the dashboard KPI at default filter settings.
    """
    ticker = (ticker or '').strip().upper()
    if not ticker:
        return jsonify({'error': 'Ticker required'}), 400
    try:
        series = _compute_ticker_kpi_series(ticker)
        dates = [p['date'] for p in series]
        kpis = [p['kpi'] for p in series]
        mentions = [p['mentions'] for p in series]
        current_kpi = kpis[-1] if kpis else None
        return jsonify({
            'ticker': ticker,
            'dates': dates,
            'kpis': kpis,
            'mentions': mentions,
            'window_days': DEFAULT_KPI_WINDOW_DAYS,
            'half_life': DEFAULT_KPI_HALF_LIFE,
            'current_kpi': current_kpi,
        })
    except Exception as e:
        logger.error(f"Error computing sentiment KPI series for {ticker}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# --- Technical analysis (computed from the stock_prices cache, no extra API calls) ---

def _sma(closes, window):
    """Simple moving average; None until the window is full."""
    out = [None] * len(closes)
    total = 0.0
    for i, c in enumerate(closes):
        total += c
        if i >= window:
            total -= closes[i - window]
        if i >= window - 1:
            out[i] = total / window
    return out


def _bollinger(closes, window=20, num_std=2.0):
    """Bollinger bands: SMA(window) +/- num_std * rolling std deviation."""
    mid = _sma(closes, window)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(window - 1, len(closes)):
        seg = closes[i - window + 1:i + 1]
        mean = mid[i]
        std = (sum((c - mean) ** 2 for c in seg) / window) ** 0.5
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
    return mid, upper, lower


def _rsi(closes, period=14):
    """RSI (Wilder smoothing); None until enough data."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _volatility_20d(closes):
    """Annualized volatility (%) of the last 20 daily log returns."""
    if len(closes) < 21:
        return None
    rets = []
    for i in range(len(closes) - 20, len(closes)):
        if closes[i - 1] and closes[i]:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5) * 100.0


def _technical_report(closes, sma20, sma50, bb_upper, bb_lower, rsi14, snapshot):
    """Rule-based narrative from the technical indicators.

    Returns a list of {text, tone} findings (tone: bullish/bearish/neutral/warning)
    plus a one-line headline. Purely deterministic, no LLM involved.
    """
    findings = []

    def add(text, tone='neutral'):
        findings.append({'text': text, 'tone': tone})

    last_close = closes[-1] if closes else None
    if not last_close:
        return [], 0

    # Price change over 5 and 20 sessions.
    if len(closes) > 20:
        chg5 = (last_close / closes[-6] - 1) * 100 if closes[-6] else None
        chg20 = (last_close / closes[-21] - 1) * 100 if closes[-21] else None
        if chg5 is not None and chg20 is not None:
            add(
                f"Price moved {chg5:+.1f}% over the last 5 sessions and {chg20:+.1f}% over the last 20.",
                'bullish' if chg20 > 0 else ('bearish' if chg20 < 0 else 'neutral'),
            )

    # Medium-term momentum vs SMA 50.
    dist = snapshot.get('dist_sma50_pct')
    if dist is not None:
        if dist > 2:
            add(f"Trading {dist:+.1f}% above its 50-day average: medium-term momentum is positive.", 'bullish')
        elif dist < -2:
            add(f"Trading {dist:+.1f}% below its 50-day average: medium-term momentum is negative.", 'bearish')
        else:
            add(f"Hovering around its 50-day average ({dist:+.1f}%): no clear medium-term trend.", 'neutral')

    # Trend structure: SMA 20 vs SMA 50, with recent cross detection.
    if sma20[-1] is not None and sma50[-1] is not None:
        above = sma20[-1] > sma50[-1]
        cross = None
        for i in range(max(1, len(closes) - 5), len(closes)):
            if sma20[i] is None or sma50[i] is None or sma20[i - 1] is None or sma50[i - 1] is None:
                continue
            if sma20[i - 1] <= sma50[i - 1] and sma20[i] > sma50[i]:
                cross = 'golden'
            elif sma20[i - 1] >= sma50[i - 1] and sma20[i] < sma50[i]:
                cross = 'death'
        if cross == 'golden':
            add("The 20-day average just crossed above the 50-day one (golden cross), a classic trend-reversal signal to the upside.", 'bullish')
        elif cross == 'death':
            add("The 20-day average just crossed below the 50-day one (death cross), a classic trend-reversal signal to the downside.", 'bearish')
        elif above:
            add("Short-term average above the medium-term one: the uptrend structure is intact.", 'bullish')
        else:
            add("Short-term average below the medium-term one: the downtrend structure is intact.", 'bearish')

    # RSI.
    rsi = snapshot.get('rsi14')
    if rsi is not None:
        if rsi >= 70:
            add(f"RSI at {rsi:.0f}: overbought territory - the recent run may be stretched.", 'warning')
        elif rsi <= 30:
            add(f"RSI at {rsi:.0f}: oversold territory - selling pressure may be exhausting.", 'warning')
        elif rsi >= 55:
            add(f"RSI at {rsi:.0f}: buyers are in control, with room before overbought levels.", 'bullish')
        elif rsi <= 45:
            add(f"RSI at {rsi:.0f}: sellers are in control, with room before oversold levels.", 'bearish')
        else:
            add(f"RSI at {rsi:.0f}: momentum is balanced.", 'neutral')

    # Bollinger position and squeeze.
    bb_pos = snapshot.get('bb_position')
    if bb_pos is not None:
        if bb_pos >= 0.95:
            add("Price is riding the upper Bollinger band: an unusually strong move relative to recent volatility.", 'warning')
        elif bb_pos <= 0.05:
            add("Price is pressing the lower Bollinger band: an unusually weak move relative to recent volatility.", 'warning')
    widths = [u - l for u, l in zip(bb_upper, bb_lower) if u is not None and l is not None]
    if len(widths) >= 20 and last_close:
        avg_width = sum(widths[:-1]) / (len(widths) - 1)
        if avg_width > 0 and widths[-1] < avg_width * 0.55:
            add("Bollinger bands are unusually tight (volatility squeeze): historically this precedes a sharp move, direction unknown.", 'warning')

    # Volatility bucket.
    vol = snapshot.get('volatility_20d_pct')
    if vol is not None:
        bucket = 'low' if vol < 20 else ('moderate' if vol < 40 else ('high' if vol < 60 else 'very high'))
        tone = 'neutral' if vol < 40 else 'warning'
        add(f"Realized volatility is {bucket} ({vol:.0f}% annualized).", tone)

    score = sum(1 for f in findings if f['tone'] == 'bullish') - sum(1 for f in findings if f['tone'] == 'bearish')
    return findings, score


def _cross_signal_finding(ticker, tech_score):
    """Compare the news sentiment KPI with the technical stance.

    The alignment/divergence between the two independent signals is the core
    research question of the project; surfacing it in the report makes it
    observable per ticker. Returns a finding dict or None.
    """
    try:
        series = _compute_ticker_kpi_series(ticker)
        kpi = next((p['kpi'] for p in reversed(series) if p['kpi'] is not None), None)
    except Exception:
        return None
    if kpi is None:
        return None
    sent_up, sent_down = kpi > 0.15, kpi < -0.15
    tech_up, tech_down = tech_score >= 1, tech_score <= -1
    if sent_up and tech_up:
        return {'text': f"News sentiment (KPI {kpi:+.2f}) and price action agree: both point up. Aligned signals.", 'tone': 'bullish'}
    if sent_down and tech_down:
        return {'text': f"News sentiment (KPI {kpi:+.2f}) and price action agree: both point down. Aligned signals.", 'tone': 'bearish'}
    if sent_up and tech_down:
        return {'text': f"Divergence: news sentiment is bullish (KPI {kpi:+.2f}) but the price action is weak. Either the market hasn't priced the news in, or the coverage is over-optimistic.", 'tone': 'warning'}
    if sent_down and tech_up:
        return {'text': f"Divergence: news sentiment is bearish (KPI {kpi:+.2f}) but the price action is strong. The rally is running against the news flow.", 'tone': 'warning'}
    return {'text': f"News sentiment is close to neutral (KPI {kpi:+.2f}): the technical picture is the dominant signal right now.", 'tone': 'neutral'}


def _finish_technical_report(findings, score):
    # Headline: net bullish vs bearish findings.
    if score >= 2:
        headline = 'Technically constructive: trend and momentum point up.'
    elif score == 1:
        headline = 'Mildly constructive, with mixed signals.'
    elif score == -1:
        headline = 'Mildly negative, with mixed signals.'
    elif score <= -2:
        headline = 'Technically weak: trend and momentum point down.'
    else:
        headline = 'No clear technical direction: signals are mixed.'
    return {'headline': headline, 'findings': findings}


@app.route('/api/tickers/<ticker>/technicals')
def api_ticker_technicals(ticker):
    """Public endpoint: daily prices plus technical indicators for one ticker.

    Everything is computed from the stock_prices cache (same data as the price
    chart), so this adds zero Alpha Vantage calls. Returns full series for
    charting (sma20/sma50, Bollinger 20+/-2sigma, rsi14) and a snapshot of the
    latest values for the KPI badges.
    """
    ticker = (ticker or '').strip().upper()
    if not ticker:
        return jsonify({'error': 'Ticker required'}), 400
    try:
        points = min(int(request.args.get('points', 120)), 365)
    except (TypeError, ValueError):
        points = 120
    try:
        data = _fetch_av_daily(ticker, points=points)
        if data.get('error'):
            return jsonify({'ticker': ticker, 'error': data['error']})

        dates = data['dates']
        closes = [c if c is not None else 0.0 for c in data['closes']]
        sma20 = _sma(closes, 20)
        _, bb_upper, bb_lower = _bollinger(closes, 20, 2.0)
        sma50 = _sma(closes, 50)
        rsi14 = _rsi(closes, 14)

        last_close = closes[-1] if closes else None
        last_sma50 = sma50[-1]
        last_rsi = rsi14[-1]
        snapshot = {
            'close': last_close,
            'rsi14': round(last_rsi, 1) if last_rsi is not None else None,
            'sma20': round(sma20[-1], 2) if sma20[-1] is not None else None,
            'sma50': round(last_sma50, 2) if last_sma50 is not None else None,
            'dist_sma50_pct': (
                round((last_close / last_sma50 - 1.0) * 100.0, 2)
                if last_close and last_sma50 else None
            ),
            'volatility_20d_pct': None,
            'bb_position': None,  # 0 = lower band, 1 = upper band
        }
        vol = _volatility_20d(closes)
        if vol is not None:
            snapshot['volatility_20d_pct'] = round(vol, 1)
        if last_close and bb_upper[-1] is not None and bb_lower[-1] is not None:
            band_width = bb_upper[-1] - bb_lower[-1]
            if band_width > 0:
                snapshot['bb_position'] = round((last_close - bb_lower[-1]) / band_width, 2)

        findings, tech_score = _technical_report(closes, sma20, sma50, bb_upper, bb_lower, rsi14, snapshot)
        cross = _cross_signal_finding(ticker, tech_score)
        if cross:
            findings.insert(0, cross)
        report = _finish_technical_report(findings, tech_score)

        return jsonify({
            'ticker': ticker,
            'dates': dates,
            'closes': data['closes'],
            'volumes': data['volumes'],
            'sma20': sma20,
            'sma50': sma50,
            'bb_upper': bb_upper,
            'bb_lower': bb_lower,
            'rsi14': rsi14,
            'snapshot': snapshot,
            'report': report,
        })
    except Exception as e:
        logger.error(f"Error computing technicals for {ticker}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/about')
def about():
    """Public page explaining what DollarPunk shows and how to read it."""
    return render_template(
        'about.html',
        default_kpi_window=DEFAULT_KPI_WINDOW_DAYS,
        default_kpi_half_life=DEFAULT_KPI_HALF_LIFE,
    )


@app.route('/tickers')
def public_companies():
    """Public, read-only page to browse company/ticker descriptions."""
    logger.info("Public companies page accessed")
    return render_template('companies_public.html')


# Throttle on-demand (lazy) company-info fetches triggered by viewer page
# loads, so repeated views of a ticker without a description don't burn through
# the Alpha Vantage rate limit (free tier is ~25 requests/day).
_company_fetch_attempts = {}
_company_fetch_lock = threading.Lock()
COMPANY_FETCH_RETRY_TTL = int(os.getenv('COMPANY_FETCH_RETRY_TTL', '21600'))  # 6h


def _load_company(cursor, ticker):
    """Load a single company row (or None) into a plain dict using the given cursor."""
    cursor.execute(
        """
        SELECT ticker, name, business_description, sector, industry,
               exchange, market_cap, website, ceo, employees,
               city, state, country
        FROM companies
        WHERE ticker = %s
        """,
        (ticker,)
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        'ticker': row[0], 'name': row[1], 'business_description': row[2],
        'sector': row[3], 'industry': row[4], 'exchange': row[5],
        'market_cap': float(row[6]) if row[6] else None,
        'website': row[7], 'ceo': row[8], 'employees': row[9],
        'city': row[10], 'state': row[11], 'country': row[12],
    }


def _maybe_fetch_company(ticker):
    """Lazily fetch + save a company profile when it's missing.

    Tries Alpha Vantage first, then FMP, and upserts via save_company_to_db.
    Throttled per ticker (COMPANY_FETCH_RETRY_TTL) to respect API rate limits.
    Returns True if a profile was fetched and saved.
    """
    av_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    fmp_key = os.getenv('FMP_API_KEY', '')
    if not av_key and not fmp_key:
        return False

    now = time.time()
    with _company_fetch_lock:
        last = _company_fetch_attempts.get(ticker)
        if last and (now - last) < COMPANY_FETCH_RETRY_TTL:
            return False
        _company_fetch_attempts[ticker] = now

    try:
        data = None
        if av_key:
            data = get_alpha_vantage_company_overview(ticker, av_key)
        if not data and fmp_key:
            data = get_fmp_company_profile(ticker, fmp_key)
        if data and save_company_to_db(ticker, data):
            logger.info(f"On-demand company fetch succeeded for {ticker}")
            return True
        logger.info(f"On-demand company fetch found no profile for {ticker}")
    except Exception as e:
        logger.warning(f"On-demand company fetch failed for {ticker}: {e}")
    return False


@app.route('/tickers/<ticker>')
def public_ticker_detail(ticker):
    """Public, read-only detail page for a single ticker: company info,
    price/volume chart (via the public stocks API) and latest related news.

    If no usable company description is on file, a small on-demand fetch is
    triggered (throttled) to try to fill it in from the APIs.
    """
    ticker = (ticker or '').strip().upper()
    logger.info(f"Public ticker detail accessed: {ticker}")

    company = None
    news = []
    summary = {'mentions': 0, 'avg_sentiment': None, 'last_seen': None, 'first_seen': None}
    error = None
    try:
        db_manager = get_db_manager(readonly=True)
        with db_manager:
            cursor = db_manager.conn.cursor()
            try:
                company = _load_company(cursor, ticker)

                # Lazy fill: if a viewer opens a ticker without a usable
                # description, try to fetch it once (throttled) and reload.
                if company is None or not (company.get('business_description') or '').strip():
                    if _maybe_fetch_company(ticker):
                        company = _load_company(cursor, ticker) or company

                # Latest news mentioning this ticker.
                cursor.execute(
                    """
                    SELECT article_id, article_title, article_url, article_source,
                           article_time_published, ticker_sentiment_score,
                           ticker_sentiment_label, relevance_score
                    FROM article_ticker_sentiment_view
                    WHERE ticker = %s
                    ORDER BY article_time_published DESC
                    LIMIT 30
                    """,
                    (ticker,)
                )
                for n in cursor.fetchall():
                    news.append({
                        'id': n[0],
                        'title': n[1],
                        'url': n[2],
                        'source': n[3],
                        'time_published': n[4].isoformat(sep=' ', timespec='minutes') if n[4] else None,
                        'sentiment_score': float(n[5]) if n[5] is not None else None,
                        'sentiment_label': n[6],
                        'relevance': float(n[7]) if n[7] is not None else None,
                    })

                # Quick aggregate summary over all stored news for the ticker.
                cursor.execute(
                    """
                    SELECT COUNT(*) AS mentions,
                           AVG(ticker_sentiment_score) AS avg_sentiment,
                           MAX(article_time_published::date) AS last_seen,
                           MIN(article_time_published::date) AS first_seen
                    FROM article_ticker_sentiment_view
                    WHERE ticker = %s
                    """,
                    (ticker,)
                )
                agg = cursor.fetchone()
                if agg:
                    summary = {
                        'mentions': int(agg[0] or 0),
                        'avg_sentiment': float(agg[1]) if agg[1] is not None else None,
                        'last_seen': agg[2].isoformat() if agg[2] else None,
                        'first_seen': agg[3].isoformat() if agg[3] else None,
                    }
            finally:
                cursor.close()
    except Exception as e:
        logger.error(f"Error building ticker detail for {ticker}: {str(e)}", exc_info=True)
        error = str(e)

    return render_template(
        'ticker_detail.html',
        ticker=ticker,
        company=company,
        news=news,
        summary=summary,
        error=error,
        default_kpi_window=DEFAULT_KPI_WINDOW_DAYS,
        default_kpi_half_life=DEFAULT_KPI_HALF_LIFE,
    )


@app.route('/')
def index():
    """Main dashboard page."""
    logger.info("Dashboard page accessed")
    # Get statistics
    stats = get_statistics()
    logger.debug(f"Statistics retrieved: {stats.get('total_articles', 0)} articles")
    retention_days = int(os.getenv('RETENTION_DAYS', '30'))
    return render_template('index.html', stats=stats, retention_days=retention_days)


@app.route('/admin/prune-old-news', methods=['POST'])
def prune_old_news_route():
    """Admin-only: delete articles older than RETENTION_DAYS (manual trigger)."""
    from job_logging import finish_execution, start_execution

    days = int(os.getenv('RETENTION_DAYS', '30'))
    exec_id = start_execution(
        'prune_old_news', 'manual', extra_metrics={'retention_days': days}
    )
    deleted = 0
    try:
        db_manager = get_db_manager()
        with db_manager:
            cursor = db_manager.conn.cursor()
            try:
                cursor.execute(
                    "DELETE FROM articles WHERE time_published < (now() - make_interval(days => %s))",
                    (days,),
                )
                deleted = cursor.rowcount
                db_manager.conn.commit()
            finally:
                cursor.close()
        finish_execution(
            exec_id,
            'completed',
            summary_message=f'Deleted {deleted} articles older than {days} days',
            extra_metrics={'retention_days': days, 'articles_deleted': deleted},
        )
        logger.info("Manual prune: deleted %s articles older than %s days", deleted, days)
        flash(f'Deleted {deleted} articles older than {days} days.', 'success')
    except Exception as e:
        finish_execution(
            exec_id,
            'error',
            error_message=str(e),
            summary_message=f'Prune failed: {e}',
            extra_metrics={'retention_days': days, 'articles_deleted': deleted},
        )
        logger.error("Manual prune failed: %s", e, exc_info=True)
        flash(f'Prune failed: {e}', 'error')
    return redirect(url_for('index'))


@app.route('/collect', methods=['POST'])
def collect_news():
    """Collect news based on form parameters."""
    global collection_status
    
    logger.info("News collection requested")
    
    if collection_status['running']:
        logger.warning("Collection already in progress, request rejected")
        flash('Collection already in progress. Please wait.', 'warning')
        return redirect(url_for('index'))
    
    # Get form parameters
    form_api_key = request.form.get('api_key', '').strip()
    env_api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    
    # Debug: log all sources
    logger.info(f"API key sources - Form: {'provided' if form_api_key else 'empty'}, "
                f"Environment: {'found' if env_api_key else 'not found'}")
    if env_api_key:
        logger.info(f"Environment API key value (first 8 chars): {env_api_key[:8]}...")
    
    # Use form API key if provided, otherwise use environment variable
    api_key = form_api_key if form_api_key else env_api_key
    
    # Log which source is being used (without exposing the full key)
    if form_api_key:
        logger.info(f"Using API key from form (first 8 chars: {form_api_key[:8]}...)")
    elif env_api_key:
        logger.info(f"Using API key from environment variable (first 8 chars: {env_api_key[:8]}...)")
    else:
        logger.error("No API key provided in form or environment")
        flash('API key is required. Please provide it in the form or set ALPHA_VANTAGE_API_KEY environment variable.', 'error')
        return redirect(url_for('index'))
    
    if not api_key:
        logger.error("API key is empty")
        flash('API key is required. Please provide it in the form or set ALPHA_VANTAGE_API_KEY environment variable.', 'error')
        return redirect(url_for('index'))
    
    tickers = request.form.get('tickers', '').strip()
    topics = request.form.get('topics', '').strip()
    time_from = request.form.get('time_from', '').strip()
    time_to = request.form.get('time_to', '').strip()
    
    # If no time range is specified, default to last 365 days
    if not time_from or not time_to:
        now = datetime.now()
        one_year_ago = now - timedelta(days=365)
        time_from = one_year_ago.strftime('%Y%m%dT0000')
        time_to = now.strftime('%Y%m%dT2359')
        logger.info(f"No time range specified, using default: last 365 days ({time_from} to {time_to})")
    
    # Date inputs are already converted to YYYYMMDDTHHMM format by JavaScript
    # But handle legacy format conversion if needed
    if time_from and 'T' in time_from and len(time_from) > 10 and '-' in time_from:
        # Format: YYYY-MM-DDTHH:MM -> YYYYMMDDTHHMM
        try:
            dt = datetime.fromisoformat(time_from.replace('Z', '+00:00'))
            time_from = dt.strftime('%Y%m%dT%H%M')
        except:
            pass  # Keep original if conversion fails
    
    if time_to and 'T' in time_to and len(time_to) > 10 and '-' in time_to:
        try:
            dt = datetime.fromisoformat(time_to.replace('Z', '+00:00'))
            time_to = dt.strftime('%Y%m%dT%H%M')
        except:
            pass
    
    limit = int(request.form.get('limit', 50))
    sort = request.form.get('sort', 'LATEST')
    save_to_db = request.form.get('save_to_db') == 'on'
    
    logger.info(f"Collection parameters: tickers={tickers}, topics={topics}, limit={limit}, sort={sort}, "
                f"time_from={time_from}, time_to={time_to}, save_to_db={save_to_db}")
    
    # Start collection in background thread
    def collect_thread():
        global collection_status
        collection_status['running'] = True
        collection_status['message'] = 'Starting collection...'
        logger.info("Starting news collection in background thread")
        
        try:
            # Get rate limit from environment (default: 75 for premium, 5 for free)
            rate_limit = int(os.getenv('ALPHA_VANTAGE_RATE_LIMIT', '75'))
            # Log API key being used (first 8 chars for security)
            api_key_preview = api_key[:8] + "..." if len(api_key) > 8 else api_key
            logger.info(f"Using API key: {api_key_preview} (rate limit: {rate_limit} calls/minute)")
            collector = AlphaVantageNewsCollector(api_key, rate_limit_per_minute=rate_limit)
            
            collection_status['message'] = 'Fetching news from Alpha Vantage...'
            logger.info("Fetching news from Alpha Vantage API")
            data = collector.get_news_sentiment(
                tickers=tickers if tickers else None,
                topics=topics if topics else None,
                time_from=time_from,  # Already set to default if empty
                time_to=time_to,  # Already set to default if empty
                limit=limit,
                sort=sort
            )
            
            articles = data.get('feed', [])
            logger.info(f"Retrieved {len(articles)} articles from API")
            
            collection_status['articles_collected'] = len(articles)
            
            if save_to_db:
                collection_status['message'] = f'Saving {len(articles)} articles to database...'
                logger.info(f"Saving {len(articles)} articles to database")
                db_manager = get_db_manager()
                with db_manager:
                    result = db_manager.save_articles(articles)
                    logger.info(f"Database save complete: {result['inserted']} inserted, {result['skipped']} skipped")
                    collection_status['message'] = (
                        f'✓ Collection complete! '
                        f'Inserted: {result["inserted"]} new articles, '
                        f'Skipped: {result["skipped"]} duplicates'
                    )
                    collection_status['articles_collected'] = result['inserted']
            else:
                logger.info(f"Collection complete: {len(articles)} articles retrieved (not saved to DB)")
                collection_status['message'] = f'✓ Collection complete! Retrieved {len(articles)} articles.'
            
            collection_status['last_run'] = datetime.now().isoformat()
            logger.info("Collection thread completed successfully")
            
        except Exception as e:
            logger.error(f"Error in collection thread: {str(e)}", exc_info=True)
            collection_status['message'] = f'✗ Error: {str(e)}'
        finally:
            collection_status['running'] = False
            logger.info("Collection thread finished")
    
    thread = threading.Thread(target=collect_thread)
    thread.daemon = True
    thread.start()
    
    flash('News collection started in background. Check status below.', 'info')
    return redirect(url_for('index'))


@app.route('/api/status')
def get_status():
    """Get collection status (AJAX endpoint)."""
    # Log only occasionally to avoid spam (every 10th call)
    if not hasattr(get_status, 'call_count'):
        get_status.call_count = 0
    get_status.call_count += 1
    if get_status.call_count % 10 == 0:
        logger.debug(f"Status check (call #{get_status.call_count})")
    return jsonify(collection_status)


@app.route('/api/stats')
def get_stats():
    """Get statistics (AJAX endpoint)."""
    stats = get_statistics()
    return jsonify(stats)


@app.route('/deep-ingestion', methods=['POST'])
def deep_ingestion():
    """Start deep ingestion - collect articles from all topics for a specified duration."""
    global deep_ingestion_status
    
    if deep_ingestion_status['running']:
        logger.warning("Deep ingestion already in progress")
        flash('Deep ingestion already in progress. Please wait.', 'warning')
        return redirect(url_for('index'))
    
    # Get duration parameter (in minutes)
    try:
        duration_minutes = int(request.form.get('duration_minutes', 60))
        if duration_minutes < 1:
            duration_minutes = 1
        elif duration_minutes > 1440:  # Max 24 hours
            duration_minutes = 1440
    except (ValueError, TypeError):
        duration_minutes = 60

    # Number of previous days to loop over. The deep ingestion will be
    # repeated for each of the last `num_days` days (D-1 ... D-num_days).
    try:
        num_days = int(request.form.get('num_days', 1))
        if num_days < 1:
            num_days = 1
        elif num_days > 30:
            num_days = 30
    except (ValueError, TypeError):
        num_days = 1

    logger.info(
        f"Deep ingestion requested: {duration_minutes} minutes/day x {num_days} day(s) "
        f"(total ~{duration_minutes * num_days} minutes)"
    )
    
    # Get API key
    form_api_key = request.form.get('api_key', '').strip()
    env_api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    api_key = form_api_key if form_api_key else env_api_key
    
    if not api_key:
        logger.error("API key not provided for deep ingestion")
        flash('API key is required for deep ingestion.', 'error')
        return redirect(url_for('index'))
    
    # All available topics supported by Alpha Vantage NEWS_SENTIMENT API
    # Complete list as per official documentation: https://www.alphavantage.co/documentation/#news-sentiment
    all_topics = [
        'blockchain',                    # Blockchain and cryptocurrency
        'earnings',                      # Earnings reports and announcements
        'ipo',                          # Initial Public Offerings
        'mergers_and_acquisitions',     # Mergers and acquisitions
        'financial_markets',           # Financial markets
        'economy_fiscal',               # Fiscal policy and economy
        'economy_monetary',             # Monetary policy and economy
        'economy_macro',                # Macroeconomics
        'energy_transportation',        # Energy and transportation
        'finance',                      # Finance
        'life_sciences',                # Life sciences and healthcare
        'manufacturing',               # Manufacturing
        'real_estate',                 # Real estate
        'retail_wholesale',            # Retail and wholesale
        'technology'                    # Technology
    ]
    
    # Start deep ingestion in background thread
    def deep_ingestion_thread():
        global deep_ingestion_status
        deep_ingestion_status['running'] = True
        deep_ingestion_status['started_at'] = datetime.now().isoformat()
        deep_ingestion_status['total_articles'] = 0
        deep_ingestion_status['total_inserted'] = 0
        deep_ingestion_status['total_skipped'] = 0
        deep_ingestion_status['topics_completed'] = 0
        deep_ingestion_status['topics_total'] = len(all_topics)
        deep_ingestion_status['current_topic'] = None
        deep_ingestion_status['current_day'] = None
        deep_ingestion_status['day_index'] = 0
        deep_ingestion_status['days_total'] = num_days

        logger.info(
            f"Starting deep ingestion: {duration_minutes} minutes/day x {num_days} day(s), "
            f"{len(all_topics)} topics"
        )

        total_inserted = 0
        total_skipped = 0
        # Deduplication is shared across the whole multi-day run so that the
        # same article is not re-inserted if it appears in multiple topics/days.
        seen_urls = set()

        try:
            rate_limit = int(os.getenv('ALPHA_VANTAGE_RATE_LIMIT', '75'))
            collector = AlphaVantageNewsCollector(api_key, rate_limit_per_minute=rate_limit)
            db_manager = get_db_manager()

            today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

            # Outer loop: one full deep ingestion per previous day (D-1 ... D-num_days)
            for day_offset in range(1, num_days + 1):
                if not deep_ingestion_status['running']:
                    logger.info("Deep ingestion stopped by user before starting next day")
                    break

                day_start = today_midnight - timedelta(days=day_offset)
                day_end = day_start + timedelta(days=1) - timedelta(minutes=1)
                day_label = day_start.strftime('%Y-%m-%d')

                day_from = day_start.strftime('%Y%m%dT%H%M')
                day_to = day_end.strftime('%Y%m%dT%H%M')

                # Each day gets its own duration budget, like a standalone deep ingestion
                day_deadline = datetime.now() + timedelta(minutes=duration_minutes)

                deep_ingestion_status['current_day'] = day_label
                deep_ingestion_status['day_index'] = day_offset
                deep_ingestion_status['topics_completed'] = 0
                logger.info(
                    f"Deep ingestion: Day {day_offset}/{num_days} ({day_label}) "
                    f"running for {duration_minutes} minutes, range {day_from} -> {day_to}"
                )

                topic_index = 0
                round_num = 0
                chunk_num = 0

                # Inner loop: cycle through topics for this day until its budget is over
                while datetime.now() < day_deadline and deep_ingestion_status['running']:
                    topic = all_topics[topic_index % len(all_topics)]

                    # Track full rounds through all topics within this day
                    if topic_index > 0 and (topic_index % len(all_topics)) == 0:
                        round_num += 1
                        logger.info(
                            f"Deep ingestion: Day {day_offset}/{num_days} ({day_label}) "
                            f"completed round {round_num}"
                        )

                    topic_index += 1
                    chunk_num += 1

                    deep_ingestion_status['current_topic'] = topic
                    deep_ingestion_status['topics_completed'] = round_num

                    remaining_seconds = (day_deadline - datetime.now()).total_seconds()
                    remaining_minutes = int(remaining_seconds / 60)
                    remaining_secs = int(remaining_seconds % 60)
                    deep_ingestion_status['message'] = (
                        f'Day {day_offset}/{num_days} ({day_label}) | '
                        f'Topic: {topic} (Round {round_num + 1}) | '
                        f'Time left on day: {remaining_minutes}m {remaining_secs}s'
                    )

                    logger.info(
                        f"Deep ingestion: Day {day_offset}/{num_days} ({day_label}), "
                        f"topic {topic}, round {round_num + 1}, chunk {chunk_num}: "
                        f"{day_from} to {day_to}"
                    )

                    try:
                        chunk_data = collector._single_request(
                            tickers=None,
                            topics=topic,
                            time_from=day_from,
                            time_to=day_to,
                            limit=50,
                            sort="LATEST"
                        )

                        chunk_articles = chunk_data.get('feed', [])

                        # Deduplicate globally across all days/topics
                        new_articles = []
                        for article in chunk_articles:
                            url = article.get('url')
                            if url and url not in seen_urls:
                                seen_urls.add(url)
                                new_articles.append(article)

                        logger.info(
                            f"Deep ingestion: Day {day_offset}/{num_days} ({day_label}), "
                            f"topic {topic}, round {round_num + 1}, chunk {chunk_num}: "
                            f"{len(new_articles)} new articles"
                        )

                        if new_articles:
                            with db_manager:
                                result = db_manager.save_articles(new_articles)
                                total_inserted += result['inserted']
                                total_skipped += result['skipped']
                                deep_ingestion_status['total_inserted'] = total_inserted
                                deep_ingestion_status['total_skipped'] = total_skipped
                                deep_ingestion_status['total_articles'] = total_inserted + total_skipped
                                logger.info(
                                    f"Deep ingestion: Saved chunk - "
                                    f"{result['inserted']} inserted, {result['skipped']} skipped"
                                )

                    except Exception as e:
                        logger.error(
                            f"Error in deep ingestion chunk for topic {topic} "
                            f"on day {day_label}: {str(e)}",
                            exc_info=True
                        )
                        # Continue with next request

                    # Rate limit delay (only if we still have time on this day)
                    if datetime.now() < day_deadline:
                        time.sleep(collector.request_delay)

            # Final status
            deep_ingestion_status['message'] = (
                f'✓ Deep ingestion complete! '
                f'Processed {num_days} day(s) x {len(all_topics)} topics, '
                f'{total_inserted} new articles inserted, '
                f'{total_skipped} duplicates skipped'
            )
            logger.info(
                f"Deep ingestion completed: {num_days} day(s), "
                f"{total_inserted} inserted, {total_skipped} skipped"
            )

        except Exception as e:
            logger.error(f"Error in deep ingestion thread: {str(e)}", exc_info=True)
            deep_ingestion_status['message'] = f'Error: {str(e)}'
        finally:
            deep_ingestion_status['running'] = False
            deep_ingestion_status['current_topic'] = None
            deep_ingestion_status['current_day'] = None
            logger.info("Deep ingestion thread finished")

    thread = threading.Thread(target=deep_ingestion_thread)
    thread.daemon = True
    thread.start()

    total_minutes = duration_minutes * num_days
    flash(
        f'Deep ingestion started: {duration_minutes} min/day x {num_days} day(s) '
        f'(~{total_minutes} min totali), collecting from all topics.',
        'info'
    )
    return redirect(url_for('index'))


@app.route('/api/deep-ingestion/status')
def get_deep_ingestion_status():
    """Get deep ingestion status (AJAX endpoint)."""
    return jsonify(deep_ingestion_status)


@app.route('/api/deep-ingestion/stop', methods=['POST'])
def stop_deep_ingestion():
    """Stop deep ingestion."""
    global deep_ingestion_status
    if deep_ingestion_status['running']:
        deep_ingestion_status['running'] = False
        logger.info("Deep ingestion stop requested")
        return jsonify({'status': 'stopping'})
    return jsonify({'status': 'not_running'})


@app.route('/deep-research', methods=['POST'])
def deep_research():
    """Start deep research - collect articles from all available stocks for a specified duration."""
    global stock_ingestion_status
    
    if stock_ingestion_status['running']:
        logger.warning("Deep research already in progress")
        flash('Deep research already in progress. Please wait.', 'warning')
        return redirect(url_for('index'))
    
    # Get duration parameter (in minutes)
    try:
        duration_minutes = int(request.form.get('duration_minutes', 120))
        if duration_minutes < 1:
            duration_minutes = 1
        elif duration_minutes > 1440:  # Max 24 hours
            duration_minutes = 1440
    except (ValueError, TypeError):
        duration_minutes = 120

    logger.info(f"Deep research requested: {duration_minutes} minutes duration")
    
    # Get API key
    form_api_key = request.form.get('api_key', '').strip()
    env_api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    api_key = form_api_key if form_api_key else env_api_key
    
    if not api_key:
        logger.error("API key not provided for deep research")
        flash('API key is required for deep research.', 'error')
        return redirect(url_for('index'))
    
    # Start deep research in background thread
    def deep_research_thread():
        global stock_ingestion_status
        stock_ingestion_status['running'] = True
        stock_ingestion_status['started_at'] = datetime.now().isoformat()
        stock_ingestion_status['total_articles'] = 0
        stock_ingestion_status['total_inserted'] = 0
        stock_ingestion_status['total_skipped'] = 0
        stock_ingestion_status['tickers_completed'] = 0
        stock_ingestion_status['tickers_total'] = 0
        stock_ingestion_status['current_ticker'] = None
        
        logger.info(f"Starting deep research: {duration_minutes} minutes duration")
        
        try:
            import requests
            import csv
            import io
            rate_limit = int(os.getenv('ALPHA_VANTAGE_RATE_LIMIT', '75'))
            collector = AlphaVantageNewsCollector(api_key, rate_limit_per_minute=rate_limit)
            db_manager = get_db_manager()
            
            # Calculate end time
            start_time = datetime.now()
            end_time = start_time + timedelta(minutes=duration_minutes)
            
            logger.info(f"Deep research: Running for {duration_minutes} minutes (until {end_time.strftime('%Y-%m-%d %H:%M:%S')})")
            stock_ingestion_status['message'] = f'Fetching list of all stocks from Alpha Vantage...'
            
            # Get list of all available stocks from Alpha Vantage LISTING_STATUS
            logger.info("Fetching list of all stocks from Alpha Vantage LISTING_STATUS endpoint")
            listing_url = "https://www.alphavantage.co/query"
            listing_params = {
                "function": "LISTING_STATUS",
                "apikey": api_key,
                "datatype": "csv"  # Request CSV format for easier parsing
            }
            
            try:
                listing_response = requests.get(listing_url, params=listing_params, timeout=120)
                listing_response.raise_for_status()
                
                # Parse response - LISTING_STATUS typically returns CSV
                stocks = []
                response_text = listing_response.text.strip()
                
                # Check if response is CSV (usually starts with header or contains commas)
                if ',' in response_text and ('symbol' in response_text.lower() or response_text.count(',') > 5):
                    # CSV format - try to parse as CSV
                    try:
                        csv_reader = csv.DictReader(io.StringIO(response_text))
                        for row in csv_reader:
                            symbol = row.get('symbol', '').strip().upper()
                            status = row.get('status', '').strip().lower()
                            # Only include active stocks
                            if symbol and status == 'active':
                                stocks.append(symbol)
                        logger.info(f"Parsed {len(stocks)} stocks from CSV format")
                    except Exception as csv_error:
                        logger.warning(f"Failed to parse as CSV: {str(csv_error)}, trying JSON...")
                        # Fall through to JSON parsing
                
                # If no stocks found from CSV, try JSON format
                if not stocks:
                    try:
                        listing_data = listing_response.json()
                        if "Error Message" in listing_data:
                            raise ValueError(f"API Error: {listing_data['Error Message']}")
                        if "Note" in listing_data:
                            raise ValueError(f"API Note: {listing_data['Note']}")
                        
                        # Try to extract from JSON response
                        if isinstance(listing_data, list):
                            stocks = [entry.get('symbol', '').upper().strip() for entry in listing_data 
                                     if entry.get('symbol') and entry.get('status', '').lower() == 'active']
                        elif 'data' in listing_data:
                            stocks = [entry.get('symbol', '').upper().strip() for entry in listing_data['data'] 
                                     if entry.get('symbol') and entry.get('status', '').lower() == 'active']
                        logger.info(f"Parsed {len(stocks)} stocks from JSON format")
                    except (ValueError, json.JSONDecodeError) as json_error:
                        # If both CSV and JSON fail, log the response for debugging
                        logger.error(f"Failed to parse response as CSV or JSON. Response preview: {response_text[:500]}")
                        raise ValueError(f"Could not parse LISTING_STATUS response: {str(json_error)}")
                
                # Filter out empty symbols and remove duplicates
                stocks = list(set([s for s in stocks if s and len(s) > 0]))
                
                if not stocks:
                    raise ValueError("No active stocks found in LISTING_STATUS response")
                
                logger.info(f"Retrieved {len(stocks)} active stocks from Alpha Vantage")
                stock_ingestion_status['tickers_total'] = len(stocks)
                stock_ingestion_status['message'] = f'Found {len(stocks)} active stocks. Starting ingestion...'
                
            except Exception as e:
                logger.error(f"Error fetching stock list: {str(e)}", exc_info=True)
                stock_ingestion_status['message'] = f'Error fetching stock list: {str(e)}'
                return
            
            total_inserted = 0
            total_skipped = 0
            seen_urls = set()  # Global deduplication across all stocks
            
            # Start from NOW and go backwards in time
            current_end = datetime.now()
            initial_start = current_end - timedelta(days=365)
            current_start = initial_start
            
            ticker_index = 0
            chunk_num = 0
            round_num = 0  # Track how many complete rounds through all stocks
            
            # Keep running until time expires
            while datetime.now() < end_time and stock_ingestion_status['running'] and ticker_index < len(stocks):
                # Cycle through stocks - priority to new stocks over older news
                ticker = stocks[ticker_index]
                
                # Check if we've completed a full round through all stocks
                if ticker_index > 0 and (ticker_index % len(stocks)) == 0:
                    round_num += 1
                    # After completing a round, go backwards in time for next round
                    current_end = current_end - timedelta(days=30)
                    # If we've gone too far back, reset to now and start fresh
                    if current_end < current_start:
                        current_end = datetime.now()
                        current_start = current_end - timedelta(days=365)
                        round_num = 0
                    logger.info(f"Deep research: Completed round {round_num}, moving back in time. Next range: {current_end.strftime('%Y%m%dT%H%M')}")
                
                ticker_index += 1
                
                stock_ingestion_status['current_ticker'] = ticker
                stock_ingestion_status['tickers_completed'] = round_num
                
                # Calculate remaining time
                remaining_seconds = (end_time - datetime.now()).total_seconds()
                remaining_minutes = int(remaining_seconds / 60)
                remaining_secs = int(remaining_seconds % 60)
                stock_ingestion_status['message'] = (
                    f'Ticker: {ticker} ({ticker_index}/{len(stocks)}, Round {round_num + 1}) | '
                    f'Time remaining: {remaining_minutes}m {remaining_secs}s'
                )
                
                chunk_num += 1
                chunk_days = 30
                
                # Go backwards: chunk_end is more recent, chunk_start is older
                chunk_start = max(current_end - timedelta(days=chunk_days), current_start)
                chunk_from = chunk_start.strftime('%Y%m%dT%H%M')
                chunk_to = current_end.strftime('%Y%m%dT%H%M')
                
                logger.info(f"Deep research: Ticker {ticker}, round {round_num + 1}, chunk {chunk_num}: {chunk_from} to {chunk_to}")
                
                try:
                    # Fetch articles for this ticker and chunk
                    # Try original ticker first
                    ticker_to_use = ticker
                    chunk_data = None
                    
                    try:
                        chunk_data = collector._single_request(
                            tickers=ticker_to_use,
                            topics=None,
                            time_from=chunk_from,
                            time_to=chunk_to,
                            limit=50,
                            sort="LATEST"
                        )
                    except ValueError as e:
                        error_msg = str(e)
                        # Check if it's an invalid ticker format error
                        if "Invalid ticker format" in error_msg:
                            # Try converting dashes to underscores (as per NEWS_SENTIMENT API requirements)
                            if "-" in ticker:
                                ticker_to_use = ticker.replace("-", "_")
                                logger.info(f"Ticker {ticker} has invalid format for NEWS_SENTIMENT API, trying variant: {ticker_to_use}")
                                try:
                                    chunk_data = collector._single_request(
                                        tickers=ticker_to_use,
                                        topics=None,
                                        time_from=chunk_from,
                                        time_to=chunk_to,
                                        limit=50,
                                        sort="LATEST"
                                    )
                                    logger.info(f"Successfully used variant {ticker_to_use} for ticker {ticker}")
                                except ValueError as e2:
                                    # If variant also fails, log and continue (don't skip the ticker)
                                    logger.warning(f"Ticker {ticker} and variant {ticker_to_use} both failed for NEWS_SENTIMENT API: {str(e2)}. No news available for this ticker format.")
                                    # Continue to next ticker without processing this chunk
                                    current_end = datetime.now()
                                    current_start = current_end - timedelta(days=365)
                                    continue
                            else:
                                # Other invalid format, log and continue
                                logger.warning(f"Ticker {ticker} has invalid format for NEWS_SENTIMENT API: {error_msg}. Skipping this ticker.")
                                current_end = datetime.now()
                                current_start = current_end - timedelta(days=365)
                                continue
                        else:
                            # Re-raise if it's a different error (rate limit, etc.)
                            raise
                    
                    if not chunk_data:
                        # No data available, continue to next ticker
                        current_end = datetime.now()
                        current_start = current_end - timedelta(days=365)
                        continue
                    
                    chunk_articles = chunk_data.get('feed', [])
                    
                    # Deduplicate globally
                    new_articles = []
                    for article in chunk_articles:
                        url = article.get('url')
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            new_articles.append(article)
                    
                    logger.info(f"Deep research: Ticker {ticker}, round {round_num + 1}, chunk {chunk_num}: {len(new_articles)} new articles")
                    
                    # Save to DB after each chunk
                    if new_articles:
                        with db_manager:
                            result = db_manager.save_articles(new_articles)
                            total_inserted += result['inserted']
                            total_skipped += result['skipped']
                            stock_ingestion_status['total_inserted'] = total_inserted
                            stock_ingestion_status['total_skipped'] = total_skipped
                            stock_ingestion_status['total_articles'] = total_inserted + total_skipped
                            logger.info(f"Deep research: Saved chunk - {result['inserted']} inserted, {result['skipped']} skipped")
                    
                except Exception as e:
                    logger.error(f"Error in deep research chunk for ticker {ticker}: {str(e)}", exc_info=True)
                    # Continue with next request - don't skip the ticker, just move on
                    current_end = datetime.now()
                    current_start = current_end - timedelta(days=365)
                
                # Rate limit delay (only if we have time left)
                if datetime.now() < end_time:
                    time.sleep(collector.request_delay)
            
            # Final status
            elapsed_minutes = int((datetime.now() - start_time).total_seconds() / 60)
            elapsed_seconds = int((datetime.now() - start_time).total_seconds() % 60)
            stock_ingestion_status['message'] = (
                f'✓ Deep research complete! '
                f'Ran for {elapsed_minutes}m {elapsed_seconds}s, '
                f'Processed {ticker_index} stocks, '
                f'{chunk_num} chunks, '
                f'{total_inserted} new articles inserted, '
                f'{total_skipped} duplicates skipped'
            )
            logger.info(f"Deep research completed: {elapsed_minutes}m {elapsed_seconds}s, {ticker_index} stocks, {chunk_num} chunks, {total_inserted} inserted, {total_skipped} skipped")
            
        except Exception as e:
            logger.error(f"Error in deep research thread: {str(e)}", exc_info=True)
            stock_ingestion_status['message'] = f'Error: {str(e)}'
        finally:
            stock_ingestion_status['running'] = False
            stock_ingestion_status['current_ticker'] = None
            logger.info("Deep research thread finished")
    
    thread = threading.Thread(target=deep_research_thread)
    thread.daemon = True
    thread.start()
    
    flash(f'Deep research started: running for {duration_minutes} minutes, collecting from all available stocks.', 'info')
    return redirect(url_for('index'))


@app.route('/api/deep-research/status')
def get_deep_research_status():
    """Get deep research status (AJAX endpoint)."""
    return jsonify(stock_ingestion_status)


@app.route('/api/deep-research/stop', methods=['POST'])
def stop_deep_research():
    """Stop deep research."""
    global stock_ingestion_status
    if stock_ingestion_status['running']:
        stock_ingestion_status['running'] = False
        logger.info("Deep research stop requested")
        return jsonify({'status': 'stopping'})
    return jsonify({'status': 'not_running'})


@app.route('/deep-research-multiday', methods=['POST'])
def deep_research_multiday():
    """
    Start a multi-day deep research: same logic as the classic deep research
    (chunks of 30 days going backwards in time, cycling through all stocks)
    but executed N times, one per previous day. For each of the last `num_days`
    days the full deep research is repeated for `duration_minutes` minutes,
    so the total runtime is approximately `duration_minutes * num_days`.
    """
    global stock_ingestion_multiday_status

    if stock_ingestion_multiday_status['running']:
        logger.warning("Multi-day deep research already in progress")
        flash('Multi-day deep research already in progress. Please wait.', 'warning')
        return redirect(url_for('index'))

    # Duration per day (in minutes)
    try:
        duration_minutes = int(request.form.get('duration_minutes', 120))
        if duration_minutes < 1:
            duration_minutes = 1
        elif duration_minutes > 1440:
            duration_minutes = 1440
    except (ValueError, TypeError):
        duration_minutes = 120

    # Number of previous days to iterate over (D-1 ... D-num_days)
    try:
        num_days = int(request.form.get('num_days', 7))
        if num_days < 1:
            num_days = 1
        elif num_days > 30:
            num_days = 30
    except (ValueError, TypeError):
        num_days = 7

    logger.info(
        f"Multi-day deep research requested: {duration_minutes} min/day x {num_days} day(s) "
        f"(total ~{duration_minutes * num_days} minutes)"
    )

    form_api_key = request.form.get('api_key', '').strip()
    env_api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    api_key = form_api_key if form_api_key else env_api_key

    if not api_key:
        logger.error("API key not provided for multi-day deep research")
        flash('API key is required for multi-day deep research.', 'error')
        return redirect(url_for('index'))

    def deep_research_multiday_thread():
        global stock_ingestion_multiday_status
        stock_ingestion_multiday_status['running'] = True
        stock_ingestion_multiday_status['started_at'] = datetime.now().isoformat()
        stock_ingestion_multiday_status['total_articles'] = 0
        stock_ingestion_multiday_status['total_inserted'] = 0
        stock_ingestion_multiday_status['total_skipped'] = 0
        stock_ingestion_multiday_status['tickers_completed'] = 0
        stock_ingestion_multiday_status['tickers_total'] = 0
        stock_ingestion_multiday_status['current_ticker'] = None
        stock_ingestion_multiday_status['current_day'] = None
        stock_ingestion_multiday_status['day_index'] = 0
        stock_ingestion_multiday_status['days_total'] = num_days

        logger.info(
            f"Starting multi-day deep research: {duration_minutes} min/day x {num_days} day(s)"
        )

        try:
            import requests
            import csv
            import io
            rate_limit = int(os.getenv('ALPHA_VANTAGE_RATE_LIMIT', '75'))
            collector = AlphaVantageNewsCollector(api_key, rate_limit_per_minute=rate_limit)
            db_manager = get_db_manager()

            overall_start = datetime.now()

            stock_ingestion_multiday_status['message'] = 'Fetching list of all stocks from Alpha Vantage...'

            # Fetch the list of active stocks once (shared across all days)
            listing_url = "https://www.alphavantage.co/query"
            listing_params = {
                "function": "LISTING_STATUS",
                "apikey": api_key,
                "datatype": "csv"
            }

            try:
                listing_response = requests.get(listing_url, params=listing_params, timeout=120)
                listing_response.raise_for_status()
                stocks = []
                response_text = listing_response.text.strip()

                if ',' in response_text and ('symbol' in response_text.lower() or response_text.count(',') > 5):
                    try:
                        csv_reader = csv.DictReader(io.StringIO(response_text))
                        for row in csv_reader:
                            symbol = row.get('symbol', '').strip().upper()
                            status = row.get('status', '').strip().lower()
                            if symbol and status == 'active':
                                stocks.append(symbol)
                    except Exception as csv_error:
                        logger.warning(f"Failed to parse listing as CSV: {csv_error}, trying JSON...")

                if not stocks:
                    try:
                        listing_data = listing_response.json()
                        if "Error Message" in listing_data:
                            raise ValueError(f"API Error: {listing_data['Error Message']}")
                        if "Note" in listing_data:
                            raise ValueError(f"API Note: {listing_data['Note']}")
                        if isinstance(listing_data, list):
                            stocks = [entry.get('symbol', '').upper().strip() for entry in listing_data
                                      if entry.get('symbol') and entry.get('status', '').lower() == 'active']
                        elif 'data' in listing_data:
                            stocks = [entry.get('symbol', '').upper().strip() for entry in listing_data['data']
                                      if entry.get('symbol') and entry.get('status', '').lower() == 'active']
                    except (ValueError, json.JSONDecodeError) as json_error:
                        logger.error(f"Failed to parse LISTING_STATUS. Preview: {response_text[:500]}")
                        raise ValueError(f"Could not parse LISTING_STATUS response: {json_error}")

                stocks = list(set([s for s in stocks if s and len(s) > 0]))
                if not stocks:
                    raise ValueError("No active stocks found in LISTING_STATUS response")

                logger.info(f"Multi-day deep research: retrieved {len(stocks)} active stocks")
                stock_ingestion_multiday_status['tickers_total'] = len(stocks)
                stock_ingestion_multiday_status['message'] = (
                    f'Found {len(stocks)} active stocks. Starting multi-day ingestion '
                    f'({num_days} days x {duration_minutes} min)...'
                )

            except Exception as e:
                logger.error(f"Error fetching stock list (multi-day): {e}", exc_info=True)
                stock_ingestion_multiday_status['message'] = f'Error fetching stock list: {e}'
                return

            total_inserted = 0
            total_skipped = 0
            # Deduplication is shared across the whole multi-day run so that
            # the same article is not re-inserted if it appears multiple times.
            seen_urls = set()
            total_chunks = 0

            today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

            # Outer loop: one full deep research per previous day
            for day_offset in range(1, num_days + 1):
                if not stock_ingestion_multiday_status['running']:
                    logger.info("Multi-day deep research stopped by user before next day")
                    break

                # Anchor of the day we are processing (D-day_offset)
                day_anchor = today_midnight - timedelta(days=day_offset - 1)
                day_label = (today_midnight - timedelta(days=day_offset)).strftime('%Y-%m-%d')

                # Each day gets its own time budget
                day_deadline = datetime.now() + timedelta(minutes=duration_minutes)

                stock_ingestion_multiday_status['current_day'] = day_label
                stock_ingestion_multiday_status['day_index'] = day_offset
                stock_ingestion_multiday_status['tickers_completed'] = 0
                stock_ingestion_multiday_status['current_ticker'] = None
                logger.info(
                    f"Multi-day deep research: Day {day_offset}/{num_days} (anchor {day_label}) "
                    f"running for {duration_minutes} minutes"
                )

                # Same chunking strategy as classic deep research, but starting
                # from the day anchor instead of "now". Chunks of 30 days going
                # backwards from `day_anchor` to up to ~1 year before it.
                current_end = day_anchor
                initial_start = current_end - timedelta(days=365)
                current_start = initial_start

                ticker_index = 0
                chunk_num = 0
                round_num = 0

                while (datetime.now() < day_deadline
                       and stock_ingestion_multiday_status['running']
                       and ticker_index < len(stocks)):
                    ticker = stocks[ticker_index]

                    if ticker_index > 0 and (ticker_index % len(stocks)) == 0:
                        round_num += 1
                        current_end = current_end - timedelta(days=30)
                        if current_end < current_start:
                            current_end = day_anchor
                            current_start = current_end - timedelta(days=365)
                            round_num = 0
                        logger.info(
                            f"Multi-day deep research: Day {day_offset}/{num_days} "
                            f"completed round {round_num}, next range ends "
                            f"{current_end.strftime('%Y%m%dT%H%M')}"
                        )

                    ticker_index += 1

                    stock_ingestion_multiday_status['current_ticker'] = ticker
                    stock_ingestion_multiday_status['tickers_completed'] = round_num

                    remaining_seconds = (day_deadline - datetime.now()).total_seconds()
                    remaining_minutes = int(remaining_seconds / 60)
                    remaining_secs = int(remaining_seconds % 60)
                    stock_ingestion_multiday_status['message'] = (
                        f'Day {day_offset}/{num_days} ({day_label}) | '
                        f'Ticker: {ticker} ({ticker_index}/{len(stocks)}, '
                        f'Round {round_num + 1}) | '
                        f'Time left on day: {remaining_minutes}m {remaining_secs}s'
                    )

                    chunk_num += 1
                    chunk_days = 30
                    chunk_start = max(current_end - timedelta(days=chunk_days), current_start)
                    chunk_from = chunk_start.strftime('%Y%m%dT%H%M')
                    chunk_to = current_end.strftime('%Y%m%dT%H%M')

                    logger.info(
                        f"Multi-day deep research: Day {day_offset}/{num_days}, "
                        f"ticker {ticker}, round {round_num + 1}, chunk {chunk_num}: "
                        f"{chunk_from} to {chunk_to}"
                    )

                    try:
                        ticker_to_use = ticker
                        chunk_data = None

                        try:
                            chunk_data = collector._single_request(
                                tickers=ticker_to_use,
                                topics=None,
                                time_from=chunk_from,
                                time_to=chunk_to,
                                limit=50,
                                sort="LATEST"
                            )
                        except ValueError as e:
                            error_msg = str(e)
                            if "Invalid ticker format" in error_msg:
                                if "-" in ticker:
                                    ticker_to_use = ticker.replace("-", "_")
                                    logger.info(
                                        f"Ticker {ticker} invalid format, trying variant: {ticker_to_use}"
                                    )
                                    try:
                                        chunk_data = collector._single_request(
                                            tickers=ticker_to_use,
                                            topics=None,
                                            time_from=chunk_from,
                                            time_to=chunk_to,
                                            limit=50,
                                            sort="LATEST"
                                        )
                                    except ValueError as e2:
                                        logger.warning(
                                            f"Ticker {ticker} and variant {ticker_to_use} both failed: {e2}"
                                        )
                                        current_end = day_anchor
                                        current_start = current_end - timedelta(days=365)
                                        continue
                                else:
                                    logger.warning(
                                        f"Ticker {ticker} invalid format: {error_msg}"
                                    )
                                    current_end = day_anchor
                                    current_start = current_end - timedelta(days=365)
                                    continue
                            else:
                                raise

                        if not chunk_data:
                            current_end = day_anchor
                            current_start = current_end - timedelta(days=365)
                            continue

                        chunk_articles = chunk_data.get('feed', [])

                        new_articles = []
                        for article in chunk_articles:
                            url = article.get('url')
                            if url and url not in seen_urls:
                                seen_urls.add(url)
                                new_articles.append(article)

                        logger.info(
                            f"Multi-day deep research: Day {day_offset}/{num_days}, "
                            f"ticker {ticker}, round {round_num + 1}, chunk {chunk_num}: "
                            f"{len(new_articles)} new articles"
                        )

                        if new_articles:
                            with db_manager:
                                result = db_manager.save_articles(new_articles)
                                total_inserted += result['inserted']
                                total_skipped += result['skipped']
                                stock_ingestion_multiday_status['total_inserted'] = total_inserted
                                stock_ingestion_multiday_status['total_skipped'] = total_skipped
                                stock_ingestion_multiday_status['total_articles'] = total_inserted + total_skipped
                                logger.info(
                                    f"Multi-day deep research: Saved chunk - "
                                    f"{result['inserted']} inserted, {result['skipped']} skipped"
                                )

                    except Exception as e:
                        logger.error(
                            f"Error in multi-day deep research chunk for ticker {ticker} "
                            f"(day {day_offset}/{num_days}): {e}",
                            exc_info=True
                        )
                        current_end = day_anchor
                        current_start = current_end - timedelta(days=365)

                    if datetime.now() < day_deadline:
                        time.sleep(collector.request_delay)

                total_chunks += chunk_num

            elapsed_minutes = int((datetime.now() - overall_start).total_seconds() / 60)
            elapsed_seconds = int((datetime.now() - overall_start).total_seconds() % 60)
            stock_ingestion_multiday_status['message'] = (
                f'✓ Multi-day deep research complete! '
                f'Ran for {elapsed_minutes}m {elapsed_seconds}s, '
                f'Processed {num_days} day(s) x up to {len(stocks)} stocks, '
                f'{total_chunks} chunks, '
                f'{total_inserted} new articles inserted, '
                f'{total_skipped} duplicates skipped'
            )
            logger.info(
                f"Multi-day deep research completed: {elapsed_minutes}m {elapsed_seconds}s, "
                f"{num_days} day(s), {total_chunks} chunks, "
                f"{total_inserted} inserted, {total_skipped} skipped"
            )

        except Exception as e:
            logger.error(f"Error in multi-day deep research thread: {e}", exc_info=True)
            stock_ingestion_multiday_status['message'] = f'Error: {e}'
        finally:
            stock_ingestion_multiday_status['running'] = False
            stock_ingestion_multiday_status['current_ticker'] = None
            stock_ingestion_multiday_status['current_day'] = None
            logger.info("Multi-day deep research thread finished")

    thread = threading.Thread(target=deep_research_multiday_thread)
    thread.daemon = True
    thread.start()

    total_minutes = duration_minutes * num_days
    flash(
        f'Multi-day deep research started: {duration_minutes} min/day x {num_days} day(s) '
        f'(~{total_minutes} min totali), collecting from all available stocks.',
        'info'
    )
    return redirect(url_for('index'))


@app.route('/api/deep-research-multiday/status')
def get_deep_research_multiday_status():
    """Get multi-day deep research status (AJAX endpoint)."""
    return jsonify(stock_ingestion_multiday_status)


@app.route('/api/deep-research-multiday/stop', methods=['POST'])
def stop_deep_research_multiday():
    """Stop multi-day deep research."""
    global stock_ingestion_multiday_status
    if stock_ingestion_multiday_status['running']:
        stock_ingestion_multiday_status['running'] = False
        logger.info("Multi-day deep research stop requested")
        return jsonify({'status': 'stopping'})
    return jsonify({'status': 'not_running'})


@app.route('/coverage-ingestion', methods=['POST'])
def coverage_ingestion():
    """Start exhaustive coverage ingestion in a background thread (admin manual)."""
    global coverage_ingestion_status

    from coverage_ingestion_job import run_coverage_ingestion
    from job_logging import is_job_running

    if coverage_ingestion_status.get('running') or is_job_running('coverage_ingestion'):
        logger.warning("Coverage ingestion already in progress")
        flash('Coverage ingestion already in progress. See Job Executions.', 'warning')
        return redirect(url_for('index'))

    try:
        num_days = int(request.form.get('num_days', 7))
        num_days = max(1, min(num_days, 30))
    except (ValueError, TypeError):
        num_days = 7

    try:
        max_minutes = int(request.form.get('max_minutes', 0))
        max_minutes = max(0, min(max_minutes, 1440))
    except (ValueError, TypeError):
        max_minutes = 0

    form_api_key = request.form.get('api_key', '').strip()
    env_api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    api_key = form_api_key if form_api_key else env_api_key

    if not api_key:
        flash('API key is required for coverage ingestion.', 'error')
        return redirect(url_for('index'))

    logger.info(
        "Coverage ingestion requested (manual): last %s day(s), max_minutes=%s",
        num_days, max_minutes or 'unlimited',
    )

    coverage_ingestion_status['running'] = True

    def coverage_ingestion_thread():
        global coverage_ingestion_status
        try:
            run_coverage_ingestion(
                num_days=num_days,
                max_minutes=max_minutes,
                api_key=api_key,
                trigger_source='manual',
                status_dict=coverage_ingestion_status,
                should_continue=lambda: coverage_ingestion_status.get('running', False),
            )
        except Exception as e:
            logger.error("Coverage ingestion thread error: %s", e, exc_info=True)
            coverage_ingestion_status['message'] = f'Error: {e}'
            coverage_ingestion_status['running'] = False

    thread = threading.Thread(target=coverage_ingestion_thread)
    thread.daemon = True
    thread.start()

    flash(
        f'Coverage ingestion started (manual): last {num_days} day(s). '
        f'Progress logged under Job Executions.',
        'info',
    )
    return redirect(url_for('index'))


@app.route('/api/coverage-ingestion/status')
def get_coverage_ingestion_status():
    """Get coverage ingestion status (AJAX endpoint), including saved progress."""
    from coverage_ingestion_job import load_coverage_progress

    payload = dict(coverage_ingestion_status)
    progress = load_coverage_progress()
    if progress:
        payload['saved_progress'] = {
            'num_days': progress.get('num_days'),
            'completed_topics': len(progress.get('completed_topics', [])),
            'completed_tickers': len(progress.get('completed_tickers', [])),
            'updated_at': progress.get('updated_at'),
        }
    else:
        payload['saved_progress'] = None
    return jsonify(payload)


@app.route('/api/coverage-ingestion/stop', methods=['POST'])
def stop_coverage_ingestion():
    """Stop coverage ingestion."""
    global coverage_ingestion_status
    if coverage_ingestion_status['running']:
        coverage_ingestion_status['running'] = False
        logger.info("Coverage ingestion stop requested")
        return jsonify({'status': 'stopping'})
    return jsonify({'status': 'not_running'})


@app.route('/api/coverage-ingestion/reset', methods=['POST'])
def reset_coverage_ingestion():
    """Clear saved coverage progress so the next run starts fresh."""
    from coverage_ingestion_job import clear_coverage_progress

    if coverage_ingestion_status['running']:
        return jsonify({'status': 'running', 'error': 'Stop the run before resetting progress.'}), 409
    clear_coverage_progress()
    coverage_ingestion_status['resumed'] = False
    coverage_ingestion_status['resumed_topics'] = 0
    coverage_ingestion_status['resumed_tickers'] = 0
    logger.info("Coverage ingestion progress reset")
    return jsonify({'status': 'reset'})


@app.route('/jobs')
def job_executions_page():
    """Admin page: history of scheduled and manual job runs."""
    from job_logging import list_executions

    job_filter = request.args.get('job', '').strip() or None
    executions = list_executions(job_name=job_filter, limit=200)
    return render_template(
        'job_executions.html',
        executions=executions,
        job_filter=job_filter or '',
    )


@app.route('/portfolio')
def portfolio():
    """Portfolio monitoring page."""
    logger.info("Portfolio page accessed")
    return render_template('portfolio.html')


@app.route('/api/portfolio/quote/<ticker>')
def get_stock_quote(ticker):
    """Get stock quote from Alpha Vantage API."""
    api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'API key not configured'}), 500
    
    try:
        import requests
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "GLOBAL_QUOTE",
            "symbol": ticker.upper(),
            "apikey": api_key
        }
        
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if "Error Message" in data:
            logger.error(f"Alpha Vantage API Error: {data['Error Message']}")
            return jsonify({'error': data['Error Message']}), 400
        
        if "Note" in data:
            logger.warning(f"Alpha Vantage API Note: {data['Note']}")
            return jsonify({'error': data['Note']}), 400
        
        if "Global Quote" in data:
            quote = data["Global Quote"]
            return jsonify({
                'symbol': quote.get('01. symbol', ticker),
                'open': quote.get('02. open', 'N/A'),
                'high': quote.get('03. high', 'N/A'),
                'low': quote.get('04. low', 'N/A'),
                'price': quote.get('05. price', 'N/A'),
                'volume': quote.get('06. volume', 'N/A'),
                'latest_trading_day': quote.get('07. latest trading day', 'N/A'),
                'previous_close': quote.get('08. previous close', 'N/A'),
                'change': quote.get('09. change', 'N/A'),
                'change_percent': quote.get('10. change percent', 'N/A')
            })
        else:
            return jsonify({'error': 'No quote data available'}), 404
            
    except Exception as e:
        logger.error(f"Error fetching stock quote for {ticker}: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/history/<ticker>')
def get_stock_history(ticker):
    """Get stock price history (intraday or daily) from Alpha Vantage API."""
    api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'API key not configured'}), 500
    
    # Get granularity from query parameter (default: 'intraday' for maximum granularity)
    granularity = request.args.get('granularity', 'intraday').lower()
    interval = request.args.get('interval', '15min')  # Default to 15min for intraday
    
    try:
        import requests
        url = "https://www.alphavantage.co/query"
        
        if granularity == 'intraday':
            # Use intraday data with configurable intervals
            params = {
                "function": "TIME_SERIES_INTRADAY",
                "symbol": ticker.upper(),
                "interval": interval,  # 1min, 5min, 15min, 30min, 60min
                "apikey": api_key,
                "outputsize": "full"  # Returns up to 2 months of data
            }
        else:
            # Daily data
            params = {
                "function": "TIME_SERIES_DAILY_ADJUSTED",
                "symbol": ticker.upper(),
                "apikey": api_key,
                "outputsize": "full"  # Returns all available data (up to 20 years)
            }
        
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if "Error Message" in data:
            logger.error(f"Alpha Vantage API Error for {ticker}: {data['Error Message']}")
            return jsonify({'error': data['Error Message']}), 400
        
        if "Note" in data:
            logger.warning(f"Alpha Vantage API Note for {ticker}: {data['Note']}")
            return jsonify({'error': data['Note']}), 400
        
        # Log available keys for debugging
        logger.debug(f"Alpha Vantage response keys for {ticker}: {list(data.keys())}")
        
        # Handle intraday data - check for various interval formats
        # Alpha Vantage returns keys like "Time Series (15min)", "Time Series (5min)", etc.
        time_series_key = None
        for key in data.keys():
            if "Time Series" in key:
                # Check if it's intraday format (contains interval in parentheses)
                if "(" in key and ")" in key:
                    time_series_key = key
                    logger.debug(f"Found intraday time series key: {time_series_key}")
                    break
        
        if time_series_key:
            time_series = data[time_series_key]
            # Get last 500 data points (or all available) for intraday
            # Limit based on interval to avoid too many points
            max_points = {
                '1min': 200,
                '5min': 300,
                '15min': 400,
                '30min': 500,
                '60min': 500
            }.get(interval, 500)
            
            # Get all timestamps sorted (most recent first)
            all_timestamps = sorted(time_series.keys(), reverse=True)
            logger.debug(f"Available timestamps for {ticker}: first={all_timestamps[0] if all_timestamps else 'none'}, last={all_timestamps[-1] if all_timestamps else 'none'}, total={len(all_timestamps)}")
            
            # Take the most recent data points
            timestamps = all_timestamps[:max_points]
            timestamps.reverse()  # Oldest first for chart
            
            if not timestamps:
                logger.warning(f"No intraday timestamps found for {ticker}")
                return jsonify({'error': 'No intraday data available for this ticker'}), 404
            
            # Log the date range
            logger.info(f"Returning {len(timestamps)} intraday data points for {ticker} with interval {interval}, from {timestamps[0]} to {timestamps[-1]}")
            
            history = {
                'dates': timestamps,
                'closes': [float(time_series[ts]['4. close']) for ts in timestamps],
                'granularity': 'intraday',
                'interval': interval
            }
            return jsonify(history)
        
        # Handle daily data
        if "Time Series (Daily)" in data:
            time_series = data["Time Series (Daily)"]
            # Get all dates sorted (most recent first)
            all_dates = sorted(time_series.keys(), reverse=True)
            logger.debug(f"Available daily dates for {ticker}: first={all_dates[0] if all_dates else 'none'}, last={all_dates[-1] if all_dates else 'none'}, total={len(all_dates)}")
            
            # Take the most recent 90 days
            dates = all_dates[:90]
            dates.reverse()  # Oldest first for chart
            
            # Log the date range
            logger.info(f"Returning {len(dates)} daily data points for {ticker}, from {dates[0] if dates else 'none'} to {dates[-1] if dates else 'none'}")
            
            history = {
                'dates': dates,
                'closes': [float(time_series[date]['5. adjusted close']) for date in dates],
                'granularity': 'daily'
            }
            return jsonify(history)
        else:
            return jsonify({'error': 'No historical data available'}), 404
            
    except Exception as e:
        logger.error(f"Error fetching stock history for {ticker}: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/sentiment/<ticker>')
def get_portfolio_sentiment(ticker):
    """Get weighted average sentiment for a ticker, grouped by time intervals."""
    granularity = request.args.get('granularity', 'intraday').lower()
    interval = request.args.get('interval', '15min')
    
    try:
        db_manager = get_db_manager()
        with db_manager:
            cursor = db_manager.conn.cursor()
            
            # Determine time interval for grouping
            if granularity == 'daily':
                time_trunc = "DATE(time_published)"
                time_format = "YYYY-MM-DD"
            else:
                # Intraday: group by minutes
                interval_minutes = {
                    '1min': 1,
                    '5min': 5,
                    '15min': 15,
                    '30min': 30,
                    '60min': 60
                }.get(interval, 15)
                
                # PostgreSQL date_trunc for minute intervals
                time_trunc = f"DATE_TRUNC('hour', time_published) + INTERVAL '{interval_minutes} minutes' * FLOOR(EXTRACT(MINUTE FROM time_published) / {interval_minutes})"
                time_format = "YYYY-MM-DD HH24:MI:SS"
            
            # Query to calculate weighted average sentiment with temporal decay
            # For each time bucket, consider data from that bucket + previous buckets within 14 days
            # with exponential decay: weight = 0.5^(days_ago / 7) where half-life is 7 days
            # This fills gaps by using data from previous intervals with decreasing weight
            query = f"""
                WITH ticker_data AS (
                    SELECT 
                        {time_trunc} as time_bucket,
                        jsonb_array_elements(ticker_sentiment) as ticker_info,
                        time_published
                    FROM articles
                    WHERE time_published IS NOT NULL
                        AND ticker_sentiment IS NOT NULL
                        AND jsonb_array_length(ticker_sentiment) > 0
                        AND time_published >= NOW() - INTERVAL '90 days'
                ),
                ticker_scores AS (
                    SELECT 
                        time_bucket,
                        (ticker_info->>'ticker_sentiment_score')::numeric as sentiment_score,
                        (ticker_info->>'relevance_score')::numeric as relevance_score,
                        time_published
                    FROM ticker_data
                    WHERE ticker_info->>'ticker' = %s
                        AND ticker_info->>'ticker_sentiment_score' IS NOT NULL
                        AND ticker_info->>'relevance_score' IS NOT NULL
                        AND (ticker_info->>'ticker_sentiment_score')::numeric IS NOT NULL
                        AND (ticker_info->>'relevance_score')::numeric IS NOT NULL
                ),
                -- Get all unique time buckets that have data
                buckets_with_data AS (
                    SELECT DISTINCT time_bucket
                    FROM ticker_scores
                ),
                -- For each bucket, calculate decayed sentiment from current + previous buckets (within 14 days)
                -- This includes:
                -- 1. Data from the same bucket
                -- 2. Data from previous buckets (all previous intervals within 14 days)
                -- 3. For intraday: also same time-of-day from previous days
                decayed_sentiment AS (
                    SELECT 
                        b1.time_bucket as target_bucket,
                        -- Calculate weighted average with temporal decay
                        -- Half-life of 7 days: weight = 0.5^(days_ago / 7)
                        -- days_ago is calculated in days (for daily) or fractional days (for intraday)
                        SUM(
                            s2.sentiment_score * s2.relevance_score * 
                            POWER(0.5, GREATEST(0, EXTRACT(EPOCH FROM (b1.time_bucket - s2.time_bucket)) / 86400.0) / 7.0)
                        ) / NULLIF(
                            SUM(
                                s2.relevance_score * 
                                POWER(0.5, GREATEST(0, EXTRACT(EPOCH FROM (b1.time_bucket - s2.time_bucket)) / 86400.0) / 7.0)
                            ),
                            0
                        ) as weighted_avg_sentiment_decayed,
                        SUM(
                            s2.relevance_score * 
                            POWER(0.5, GREATEST(0, EXTRACT(EPOCH FROM (b1.time_bucket - s2.time_bucket)) / 86400.0) / 7.0)
                        ) as total_relevance_decayed,
                        COUNT(*) as article_count
                    FROM buckets_with_data b1
                    LEFT JOIN ticker_scores s2 ON (
                        s2.time_bucket <= b1.time_bucket 
                        AND s2.time_bucket >= b1.time_bucket - INTERVAL '14 days'
                    )
                    GROUP BY b1.time_bucket
                    HAVING SUM(
                        s2.relevance_score * 
                        POWER(0.5, GREATEST(0, EXTRACT(EPOCH FROM (b1.time_bucket - s2.time_bucket)) / 86400.0) / 7.0)
                    ) > 0
                )
                SELECT 
                    TO_CHAR(target_bucket, %s) as time_bucket_str,
                    weighted_avg_sentiment_decayed,
                    total_relevance_decayed,
                    article_count
                FROM decayed_sentiment
                ORDER BY target_bucket ASC
            """
            
            cursor.execute(query, (ticker.upper(), time_format))
            results = cursor.fetchall()
            cursor.close()
            
            if not results:
                logger.warning(f"No sentiment data found for {ticker} with {granularity} granularity (interval: {interval})")
                return jsonify({
                    'dates': [],
                    'sentiments': [],
                    'granularity': granularity,
                    'interval': interval if granularity == 'intraday' else None
                })
            
            dates = [row[0] for row in results]
            sentiments = [float(row[1]) if row[1] is not None else 0.0 for row in results]
            article_counts = [int(row[3]) if row[3] is not None else 0 for row in results]
            
            logger.info(f"Returning {len(dates)} sentiment data points for {ticker} with {granularity} granularity (interval: {interval}), from {dates[0] if dates else 'none'} to {dates[-1] if dates else 'none'}")
            
            return jsonify({
                'dates': dates,
                'sentiments': sentiments,
                'article_counts': article_counts,
                'granularity': granularity,
                'interval': interval if granularity == 'intraday' else None
            })
            
    except Exception as e:
        logger.error(f"Error fetching sentiment for {ticker}: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/news/<tickers>')
def get_portfolio_news(tickers):
    """Get latest news for portfolio tickers and save to database."""
    api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'API key not configured'}), 500
    
    try:
        # Limit to 20 articles for portfolio view
        rate_limit = int(os.getenv('ALPHA_VANTAGE_RATE_LIMIT', '75'))
        collector = AlphaVantageNewsCollector(api_key, rate_limit_per_minute=rate_limit)
        
        # Get news for the tickers (comma-separated)
        logger.info(f"Fetching portfolio news for tickers: {tickers}")
        data = collector.get_news_sentiment(
            tickers=tickers,
            limit=20,
            sort="LATEST"
        )
        
        articles = data.get('feed', [])
        
        # Save articles to database (idempotent)
        save_result = {'inserted': 0, 'skipped': 0}
        if articles:
            logger.info(f"Saving {len(articles)} portfolio news articles to database")
            try:
                db_manager = get_db_manager()
                with db_manager:
                    save_result = db_manager.save_articles(articles)
                    logger.info(f"Portfolio news saved: {save_result['inserted']} inserted, {save_result['skipped']} skipped")
            except Exception as db_error:
                logger.error(f"Error saving portfolio news to database: {str(db_error)}", exc_info=True)
                # Continue even if database save fails
        
        return jsonify({
            'articles': articles,
            'count': len(articles),
            'saved_to_db': True,
            'inserted': save_result['inserted'],
            'skipped': save_result['skipped']
        })
        
    except Exception as e:
        logger.error(f"Error fetching news for {tickers}: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/article/<int:article_id>')
def view_article_detail(article_id):
    """View full details of a single article from database."""
    logger.info(f"Article detail page accessed for ID: {article_id}")
    
    try:
        db_manager = get_db_manager(readonly=True)
        with db_manager:
            if not db_manager.conn:
                raise ConnectionError("Database connection not established")
            
            cursor = db_manager.conn.cursor()
            query = """
                SELECT id, url, title, source, time_published, summary,
                       overall_sentiment_score, overall_sentiment_label,
                       ticker_sentiment, topics, banner_image, source_domain,
                       created_at, updated_at
                FROM articles
                WHERE id = %s
            """
            cursor.execute(query, (article_id,))
            
            columns = [desc[0] for desc in cursor.description]
            row = cursor.fetchone()
            cursor.close()
            
            if not row:
                flash(f'Article with ID {article_id} not found', 'error')
                return redirect(url_for('view_articles'))
            
            article = dict(zip(columns, row))
            
            # Convert overall_sentiment_score to float if needed
            if article.get('overall_sentiment_score') is not None:
                try:
                    if isinstance(article['overall_sentiment_score'], str):
                        article['overall_sentiment_score'] = float(article['overall_sentiment_score'])
                except (ValueError, TypeError):
                    article['overall_sentiment_score'] = None
            
            # Parse JSONB fields
            if article.get('ticker_sentiment'):
                if isinstance(article['ticker_sentiment'], str):
                    try:
                        article['ticker_sentiment'] = json.loads(article['ticker_sentiment'])
                    except json.JSONDecodeError:
                        article['ticker_sentiment'] = []
                # Convert ticker sentiment scores to float
                if isinstance(article['ticker_sentiment'], list):
                    for ticker_data in article['ticker_sentiment']:
                        if isinstance(ticker_data, dict):
                            if 'ticker_sentiment_score' in ticker_data:
                                try:
                                    if isinstance(ticker_data['ticker_sentiment_score'], str):
                                        ticker_data['ticker_sentiment_score'] = float(ticker_data['ticker_sentiment_score'])
                                    else:
                                        ticker_data['ticker_sentiment_score'] = float(ticker_data['ticker_sentiment_score'])
                                except (ValueError, TypeError):
                                    ticker_data['ticker_sentiment_score'] = None
                            if 'relevance_score' in ticker_data:
                                try:
                                    if isinstance(ticker_data['relevance_score'], str):
                                        ticker_data['relevance_score'] = float(ticker_data['relevance_score'])
                                    else:
                                        ticker_data['relevance_score'] = float(ticker_data['relevance_score'])
                                except (ValueError, TypeError):
                                    ticker_data['relevance_score'] = None
            
            if article.get('topics'):
                if isinstance(article['topics'], str):
                    try:
                        article['topics'] = json.loads(article['topics'])
                    except json.JSONDecodeError:
                        article['topics'] = []
            
            logger.info(f"Loaded article detail: {article.get('title', 'Unknown')[:50]}")
            return render_template('article_detail.html', article=article)
            
    except Exception as e:
        logger.error(f"Error loading article {article_id}: {str(e)}", exc_info=True)
        flash(f'Error loading article: {str(e)}', 'error')
        return redirect(url_for('view_articles'))


@app.route('/articles')
def view_articles():
    """View collected articles from database."""
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    sentiment_filter = request.args.get('sentiment', '')
    ticker_filter = request.args.get('ticker', '')
    search_query = request.args.get('search', '')
    provider_filter = request.args.get('provider', '')
    scope_filter = request.args.get('scope', '')

    logger.info(f"Articles page accessed: page={page}, per_page={per_page}, "
                f"sentiment={sentiment_filter}, ticker={ticker_filter}, search={search_query}, "
                f"provider={provider_filter}, scope={scope_filter}")

    try:
        db_manager = get_db_manager(readonly=True)
        with db_manager:
            articles, total, stats = get_articles_paginated(
                db_manager, page, per_page, sentiment_filter, ticker_filter,
                search_query, provider_filter, scope_filter
            )
        logger.info(f"Loaded {len(articles)} articles (total: {total})")
        
        return render_template('articles.html', 
                             articles=articles,
                             page=page,
                             per_page=per_page,
                             total=total,
                             total_pages=(total + per_page - 1) // per_page,
                             sentiment_filter=sentiment_filter,
                             ticker_filter=ticker_filter,
                             search_query=search_query,
                             provider_filter=provider_filter,
                             scope_filter=scope_filter,
                             stats=stats)
    except Exception as e:
        logger.error(f"Error loading articles: {str(e)}", exc_info=True)
        flash(f'Error loading articles: {str(e)}', 'error')
        return redirect(url_for('index'))


def get_articles_paginated(db_manager, page, per_page, sentiment_filter='',
                           ticker_filter='', search_query='', provider_filter='',
                           scope_filter=''):
    """Get paginated articles from database."""
    if not db_manager.conn:
        raise ConnectionError("Database connection not established")

    cursor = db_manager.conn.cursor()

    try:
        # Build WHERE clause
        conditions = []
        params = []

        if sentiment_filter:
            conditions.append("overall_sentiment_label = %s")
            params.append(sentiment_filter)

        if provider_filter:
            # Legacy rows may predate the provider column: treat NULL as alpha_vantage.
            conditions.append("COALESCE(provider, 'alpha_vantage') = %s")
            params.append(provider_filter)

        if scope_filter:
            conditions.append("topics @> %s::jsonb")
            params.append(json.dumps([{"topic": scope_filter}]))
        
        if ticker_filter:
            conditions.append("ticker_sentiment::text LIKE %s")
            params.append(f'%{ticker_filter}%')
        
        if search_query:
            conditions.append("(title ILIKE %s OR summary ILIKE %s OR source ILIKE %s)")
            params.extend([f'%{search_query}%', f'%{search_query}%', f'%{search_query}%'])
        
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        
        # Get total count
        count_query = f"SELECT COUNT(*) FROM articles {where_clause}"
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]
        
        # Get paginated articles
        offset = (page - 1) * per_page
        query = f"""
            SELECT id, url, title, source, time_published, summary,
                   overall_sentiment_score, overall_sentiment_label,
                   ticker_sentiment, topics, banner_image, source_domain,
                   COALESCE(provider, 'alpha_vantage') AS provider,
                   created_at, updated_at
            FROM articles
            {where_clause}
            ORDER BY time_published DESC NULLS LAST, created_at DESC
            LIMIT %s OFFSET %s
        """
        cursor.execute(query, params + [per_page, offset])
        
        columns = [desc[0] for desc in cursor.description]
        articles = []
        for row in cursor.fetchall():
            article = dict(zip(columns, row))
            
            # Convert overall_sentiment_score to float if it's a string
            if article.get('overall_sentiment_score') is not None:
                try:
                    if isinstance(article['overall_sentiment_score'], str):
                        article['overall_sentiment_score'] = float(article['overall_sentiment_score'])
                    elif not isinstance(article['overall_sentiment_score'], (int, float)):
                        article['overall_sentiment_score'] = None
                except (ValueError, TypeError):
                    logger.warning(f"Could not convert overall_sentiment_score to float: {article.get('overall_sentiment_score')}")
                    article['overall_sentiment_score'] = None
            
            # Parse JSONB fields (psycopg2 may already convert JSONB to dict/list)
            if article.get('ticker_sentiment'):
                if isinstance(article['ticker_sentiment'], str):
                    try:
                        article['ticker_sentiment'] = json.loads(article['ticker_sentiment'])
                    except json.JSONDecodeError:
                        logger.warning(f"Could not parse ticker_sentiment JSON: {article.get('ticker_sentiment')}")
                        article['ticker_sentiment'] = []
                
                # Convert ticker_sentiment_score to float if it's a string
                if isinstance(article['ticker_sentiment'], list):
                    for ticker_data in article['ticker_sentiment']:
                        if isinstance(ticker_data, dict) and 'ticker_sentiment_score' in ticker_data:
                            try:
                                if isinstance(ticker_data['ticker_sentiment_score'], str):
                                    ticker_data['ticker_sentiment_score'] = float(ticker_data['ticker_sentiment_score'])
                                elif ticker_data['ticker_sentiment_score'] is not None:
                                    ticker_data['ticker_sentiment_score'] = float(ticker_data['ticker_sentiment_score'])
                            except (ValueError, TypeError):
                                logger.warning(f"Could not convert ticker_sentiment_score to float: {ticker_data.get('ticker_sentiment_score')}")
                                ticker_data['ticker_sentiment_score'] = None
                        if isinstance(ticker_data, dict) and 'relevance_score' in ticker_data:
                            try:
                                if isinstance(ticker_data['relevance_score'], str):
                                    ticker_data['relevance_score'] = float(ticker_data['relevance_score'])
                                elif ticker_data['relevance_score'] is not None:
                                    ticker_data['relevance_score'] = float(ticker_data['relevance_score'])
                            except (ValueError, TypeError):
                                ticker_data['relevance_score'] = None
                # If already dict/list, keep as is
            if article.get('topics'):
                if isinstance(article['topics'], str):
                    try:
                        article['topics'] = json.loads(article['topics'])
                    except json.JSONDecodeError:
                        logger.warning(f"Could not parse topics JSON: {article.get('topics')}")
                        article['topics'] = []
                # If already dict/list, keep as is
            articles.append(article)
        
        # Get statistics
        stats = get_statistics_from_db(cursor)
        
        return articles, total, stats
        
    finally:
        cursor.close()


def get_statistics():
    """Get database statistics."""
    try:
        db_manager = get_db_manager()
        with db_manager:
            cursor = db_manager.conn.cursor()
            try:
                stats = get_statistics_from_db(cursor)
                logger.debug(f"Statistics retrieved: {stats.get('total_articles', 0)} articles")
                return stats
            finally:
                cursor.close()
    except Exception as e:
        logger.error(f"Error getting statistics: {str(e)}", exc_info=True)
        return {
            'total_articles': 0,
            'by_sentiment': {},
            'latest_article': None,
            'error': str(e)
        }


def get_statistics_from_db(cursor):
    """Get statistics from database cursor."""
    stats = {}
    
    # Total articles
    cursor.execute("SELECT COUNT(*) FROM articles")
    stats['total_articles'] = cursor.fetchone()[0]
    
    # Articles by sentiment
    cursor.execute("""
        SELECT overall_sentiment_label, COUNT(*) as count
        FROM articles
        WHERE overall_sentiment_label IS NOT NULL
        GROUP BY overall_sentiment_label
        ORDER BY count DESC
    """)
    stats['by_sentiment'] = {row[0]: row[1] for row in cursor.fetchall()}
    
    # Latest article date
    cursor.execute("""
        SELECT MAX(time_published) FROM articles
    """)
    latest = cursor.fetchone()[0]
    stats['latest_article'] = latest.isoformat() if latest else None
    
    # Articles by date (last 7 days)
    cursor.execute("""
        SELECT DATE(time_published) as date, COUNT(*) as count
        FROM articles
        WHERE time_published >= NOW() - INTERVAL '7 days'
        GROUP BY DATE(time_published)
        ORDER BY date DESC
    """)
    stats['by_date'] = {str(row[0]): row[1] for row in cursor.fetchall()}
    
    return stats


@app.route('/api/articles')
def api_articles():
    """API endpoint for articles (JSON)."""
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    sentiment_filter = request.args.get('sentiment', '')
    ticker_filter = request.args.get('ticker', '')
    provider_filter = request.args.get('provider', '')
    scope_filter = request.args.get('scope', '')

    try:
        db_manager = get_db_manager(readonly=True)
        with db_manager:
            articles, total, _ = get_articles_paginated(
                db_manager, page, per_page, sentiment_filter, ticker_filter,
                '', provider_filter, scope_filter
            )
        
        return jsonify({
            'articles': articles,
            'page': page,
            'per_page': per_page,
            'total': total,
            'total_pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def get_alpha_vantage_company_overview(ticker, api_key):
    """
    Fetch company overview from Alpha Vantage API.
    
    Args:
        ticker: Stock ticker symbol (e.g., 'AAPL')
        api_key: Alpha Vantage API key
    
    Returns:
        dict with company data or None if error
    """
    # Log moved to batch log system
    
    if not api_key:
        logger.error(f"No Alpha Vantage API key provided for ticker {ticker}")
        return None
    
    try:
        url = "https://www.alphavantage.co/query"
        params = {
            'function': 'OVERVIEW',
            'symbol': ticker,
            'apikey': api_key
        }
        
        logger.debug(f"Alpha Vantage API Request - URL: {url}, Function: OVERVIEW, Symbol: {ticker}")
        
        # Use shorter timeout to make stop more responsive
        response = requests.get(url, params=params, timeout=5)
        
        logger.debug(f"Alpha Vantage API Response - Status: {response.status_code}")
        
        response.raise_for_status()
        
        data = response.json()
        
        # Check for API errors
        if 'Error Message' in data:
            logger.error(f"Alpha Vantage API Error for {ticker}: {data['Error Message']}")
            return None
        
        if 'Note' in data:
            logger.warning(f"Alpha Vantage API Note for {ticker}: {data['Note']}")
            return None
        
        # Check if we got valid data
        if not data or 'Symbol' not in data:
            # Don't spam main log - this is expected for many tickers
            logger.debug(f"Alpha Vantage returned empty or invalid data for {ticker}")
            return None
        
        logger.debug(f"Alpha Vantage returned data for {ticker}")
        logger.debug(f"Alpha Vantage data keys: {list(data.keys())}")
        
        # Map Alpha Vantage fields to our database schema
        # Alpha Vantage uses different field names than FMP
        mapped_data = {
            'companyName': data.get('Name', ticker),
            'description': data.get('Description', ''),
            'sector': data.get('Sector', ''),
            'industry': data.get('Industry', ''),
            'exchangeShortName': data.get('Exchange', ''),
            'mktCap': None,  # Alpha Vantage doesn't provide market cap directly
            'website': '',  # Alpha Vantage doesn't provide website
            'ceo': '',  # Alpha Vantage doesn't provide CEO
            'fullTimeEmployees': None,  # Alpha Vantage doesn't provide employees
            'address': '',  # Alpha Vantage doesn't provide address
            'city': '',  # Alpha Vantage doesn't provide city
            'state': '',  # Alpha Vantage doesn't provide state
            'country': data.get('Country', ''),
            'phone': '',  # Alpha Vantage doesn't provide phone
            # Additional Alpha Vantage fields we can use
            'symbol': data.get('Symbol', ticker),
            'assetType': data.get('AssetType', ''),
            'currency': data.get('Currency', ''),
            'fiscalYearEnd': data.get('FiscalYearEnd', ''),
            'latestQuarter': data.get('LatestQuarter', ''),
        }
        
        # Try to calculate market cap from shares outstanding and price
        shares_outstanding = data.get('SharesOutstanding')
        price = data.get('52WeekHigh') or data.get('52WeekLow')
        if shares_outstanding and price:
            try:
                # Shares outstanding might be in millions or actual number
                shares = float(shares_outstanding)
                price_val = float(price)
                # If shares < 1000, assume it's in millions
                if shares < 1000:
                    shares = shares * 1000000
                mapped_data['mktCap'] = shares * price_val
            except (ValueError, TypeError):
                pass
        
        return mapped_data
        
    except requests.exceptions.Timeout as e:
        logger.error(f"Timeout fetching Alpha Vantage OVERVIEW for {ticker}: {str(e)}")
        return None
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error fetching Alpha Vantage OVERVIEW for {ticker}: Status {response.status_code}, Response: {response.text[:500]}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request exception fetching Alpha Vantage OVERVIEW for {ticker}: {str(e)}", exc_info=True)
        return None
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error for {ticker}: {str(e)}, Response text: {response.text[:500]}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching Alpha Vantage OVERVIEW for {ticker}: {str(e)}", exc_info=True)
        return None


def get_fmp_company_profile(ticker, api_key):
    """
    Fetch company profile from Financial Modeling Prep API.
    
    Args:
        ticker: Stock ticker symbol (e.g., 'AAPL')
        api_key: Financial Modeling Prep API key
    
    Returns:
        dict with company data or None if error
    """
    logger.debug(f"Fetching FMP profile for ticker: {ticker}")
    
    if not api_key:
        logger.error(f"No FMP API key provided for ticker {ticker}")
        return None
    
    try:
        # Try multiple endpoint formats - FMP has deprecated v3 and may use different formats
        endpoints_to_try = [
            f"https://financialmodelingprep.com/stable/profile/{ticker}",  # Stable endpoint (recommended)
            f"https://financialmodelingprep.com/api/v4/company/profile/{ticker}",
            f"https://financialmodelingprep.com/api/v4/profile/{ticker}",
            f"https://financialmodelingprep.com/api/v3/profile/{ticker}",  # Legacy (may not work)
        ]
        
        for url in endpoints_to_try:
            params = {'apikey': api_key}
            
            logger.debug(f"FMP API Request - URL: {url}, Params: apikey={'*' * (len(api_key) - 4) + api_key[-4:]}")
            
            try:
                # Use shorter timeout to make stop more responsive
                response = requests.get(url, params=params, timeout=5)
                
                logger.debug(f"FMP API Response - Status: {response.status_code}, Headers: {dict(response.headers)}")
                
                # If 403, try next endpoint
                if response.status_code == 403:
                    error_data = response.json() if response.text else {}
                    error_msg = error_data.get('Error Message', '')
                    if 'Legacy Endpoint' in error_msg or 'no longer supported' in error_msg:
                        logger.warning(f"Endpoint {url} is deprecated (403), trying next endpoint...")
                        continue
                
                response.raise_for_status()
                
                data = response.json()
                logger.debug(f"FMP API Response data type: {type(data)}, length: {len(data) if isinstance(data, (list, dict)) else 'N/A'}")
                
                if isinstance(data, list):
                    if len(data) > 0:
                        logger.info(f"FMP API returned list with {len(data)} items for {ticker}, using first item")
                        logger.debug(f"First item keys: {list(data[0].keys()) if isinstance(data[0], dict) else 'N/A'}")
                        return data[0]  # API returns array, get first element
                    else:
                        logger.warning(f"FMP API returned empty list for {ticker}")
                        return None
                elif isinstance(data, dict):
                    logger.info(f"FMP API returned dict for {ticker}")
                    logger.debug(f"Dict keys: {list(data.keys())}")
                    return data
                else:
                    logger.warning(f"Unexpected response format for {ticker}: type={type(data)}, value={str(data)[:200]}")
                    return None
                    
            except requests.exceptions.HTTPError as e:
                if response.status_code == 403:
                    # Continue to next endpoint
                    continue
                else:
                    # For other HTTP errors, raise to be caught by outer handler
                    raise
        
        # If all endpoints failed with 403
        logger.error(f"All FMP endpoints returned 403 (deprecated/legacy) for {ticker}. "
                    f"FMP API v3 endpoints are no longer available for new subscriptions. "
                    f"Please check FMP documentation for the correct v4 endpoint or upgrade your subscription.")
        return None
            
    except requests.exceptions.Timeout as e:
        logger.error(f"Timeout fetching FMP profile for {ticker}: {str(e)}")
        return None
    except requests.exceptions.HTTPError as e:
        logger.debug(f"HTTP error fetching FMP profile for {ticker}: Status {response.status_code}, Response: {response.text[:500]}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request exception fetching FMP profile for {ticker}: {str(e)}", exc_info=True)
        return None
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error for {ticker}: {str(e)}, Response text: {response.text[:500]}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching FMP profile for {ticker}: {str(e)}", exc_info=True)
        return None


def company_exists_in_db(ticker):
    """
    Check if a company already exists in the database and was updated today,
    OR if it had an error today (should skip in both cases).
    Returns True if company exists AND was updated today OR had error today.
    Returns False if company doesn't exist OR was last updated before today AND no error today.
    """
    db_manager = get_db_manager()
    try:
        with db_manager:
            if not db_manager.conn:
                return False
            cursor = db_manager.conn.cursor()
            # Check if exists and (last_updated is today OR had error today)
            cursor.execute("""
                SELECT 1 FROM companies 
                WHERE ticker = %s 
                AND (
                    DATE(last_updated) = CURRENT_DATE
                    OR DATE(last_error_date) = CURRENT_DATE
                )
                LIMIT 1
            """, (ticker,))
            exists_today = cursor.fetchone() is not None
            cursor.close()
            return exists_today
    except Exception as e:
        logger.error(f"Error checking if company {ticker} exists: {str(e)}")
        return False


def save_company_error(ticker, error_message):
    """
    Save error information for a ticker.
    Creates a record if ticker doesn't exist, or updates error info if it does.
    """
    db_manager = get_db_manager()
    try:
        with db_manager:
            if not db_manager.conn:
                logger.error(f"Database connection not established for error logging {ticker}")
                return False
            
            cursor = db_manager.conn.cursor()
            
            # Check if ticker exists
            cursor.execute("SELECT 1 FROM companies WHERE ticker = %s", (ticker,))
            exists = cursor.fetchone() is not None
            
            if exists:
                # Update existing record with error info
                cursor.execute("""
                    UPDATE companies 
                    SET last_error_date = CURRENT_DATE,
                        last_error_message = %s
                    WHERE ticker = %s
                """, (error_message[:500], ticker))  # Limit error message length
            else:
                # Insert new record with just error info (no company data)
                cursor.execute("""
                    INSERT INTO companies (ticker, name, last_error_date, last_error_message)
                    VALUES (%s, %s, CURRENT_DATE, %s)
                    ON CONFLICT (ticker) DO UPDATE SET
                        last_error_date = CURRENT_DATE,
                        last_error_message = EXCLUDED.last_error_message
                """, (ticker, ticker, error_message[:500]))
            
            db_manager.conn.commit()
            logger.debug(f"Saved error for ticker {ticker}: {error_message[:100]}")
            cursor.close()
            return True
            
    except Exception as e:
        logger.error(f"Error saving error info for {ticker}: {str(e)}", exc_info=True)
        return False


def save_company_to_db(ticker, company_data):
    """
    Save or update company data to database.
    
    Args:
        ticker: Stock ticker symbol
        company_data: dict with company information from FMP API
    
    Returns:
        bool: True if successful, False otherwise
    """
    # Log moved to batch log system
    logger.debug(f"Company data keys: {list(company_data.keys()) if isinstance(company_data, dict) else 'N/A'}")
    
    db_manager = get_db_manager()
    
    try:
        with db_manager:
            if not db_manager.conn:
                logger.error(f"Database connection not established for {ticker}")
                return False
            
            cursor = db_manager.conn.cursor()
            
            # Extract fields - handle both Alpha Vantage and FMP formats
            # Alpha Vantage uses: name, business_description, etc.
            # FMP uses: companyName, description, etc.
            name = company_data.get('name') or company_data.get('companyName', ticker)
            description = company_data.get('business_description') or company_data.get('description', '')
            sector = company_data.get('sector', '')
            industry = company_data.get('industry', '')
            exchange = company_data.get('exchange') or company_data.get('exchangeShortName', '')
            market_cap = company_data.get('market_cap') or company_data.get('mktCap')
            website = company_data.get('website', '')
            ceo = company_data.get('ceo', '')
            employees = company_data.get('employees') or company_data.get('fullTimeEmployees')
            address = company_data.get('address', '')
            city = company_data.get('city', '')
            state = company_data.get('state', '')
            country = company_data.get('country', '')
            phone = company_data.get('phone', '')
            
            logger.debug(f"Extracted data for {ticker}: name={name}, sector={sector}, industry={industry}, "
                        f"description_length={len(description) if description else 0}")
            
            # Insert or update
            cursor.execute("""
                INSERT INTO companies (
                    ticker, name, business_description, sector, industry,
                    exchange, market_cap, website, ceo, employees,
                    address, city, state, country, phone
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker) DO UPDATE SET
                    name = EXCLUDED.name,
                    business_description = EXCLUDED.business_description,
                    sector = EXCLUDED.sector,
                    industry = EXCLUDED.industry,
                    exchange = EXCLUDED.exchange,
                    market_cap = EXCLUDED.market_cap,
                    website = EXCLUDED.website,
                    ceo = EXCLUDED.ceo,
                    employees = EXCLUDED.employees,
                    address = EXCLUDED.address,
                    city = EXCLUDED.city,
                    state = EXCLUDED.state,
                    country = EXCLUDED.country,
                    phone = EXCLUDED.phone,
                    last_updated = CURRENT_TIMESTAMP
            """, (
                ticker, name, description, sector, industry,
                exchange, market_cap, website, ceo, employees,
                address, city, state, country, phone
            ))
            
            db_manager.conn.commit()
            # Log moved to batch log system
            cursor.close()
            return True
            
    except Exception as e:
        logger.error(f"Error saving company {ticker} to database: {str(e)}", exc_info=True)
        if db_manager.conn:
            db_manager.conn.rollback()
        return False


@app.route('/companies')
def companies():
    """Companies management page for viewing and populating company descriptions."""
    logger.info("Companies page accessed")
    return render_template('companies.html')


@app.route('/api/companies')
def get_companies():
    """API endpoint to fetch companies from database."""
    logger.info("API /api/companies called")
    
    try:
        db_manager = get_db_manager(readonly=True)
        with db_manager:
            if not db_manager.conn:
                raise ConnectionError("Database connection not established")
            
            cursor = db_manager.conn.cursor()
            
            # Get query parameters
            search = request.args.get('search', '').strip()
            sector = request.args.get('sector', '').strip()
            limit = int(request.args.get('limit', 100))
            offset = int(request.args.get('offset', 0))
            
            logger.debug(f"Companies query params: search='{search}', sector='{sector}', limit={limit}, offset={offset}")
            
            # Build query
            where_clauses = []
            params = []
            
            if search:
                where_clauses.append("(ticker ILIKE %s OR name ILIKE %s)")
                search_pattern = f"%{search}%"
                params.extend([search_pattern, search_pattern])
                logger.debug(f"Added search filter: {search_pattern}")
            
            if sector:
                where_clauses.append("sector = %s")
                params.append(sector)
                logger.debug(f"Added sector filter: {sector}")
            
            where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"
            logger.debug(f"Final WHERE clause: {where_clause}, params: {params}")
            
            # Get total count
            start_time = time.time()
            cursor.execute(f"SELECT COUNT(*) FROM companies WHERE {where_clause}", params)
            total_count = cursor.fetchone()[0]
            count_time = time.time() - start_time
            logger.info(f"Companies count query: {total_count} companies found (took {count_time:.3f}s)")
            
            # Get companies
            query = f"""
                SELECT 
                    ticker, name, business_description, sector, industry,
                    exchange, market_cap, website, ceo, employees,
                    address, city, state, country, phone,
                    last_updated, created_at
                FROM companies
                WHERE {where_clause}
                ORDER BY ticker
                LIMIT %s OFFSET %s
            """
            params.extend([limit, offset])
            
            logger.debug(f"Executing companies query with limit={limit}, offset={offset}")
            start_time = time.time()
            cursor.execute(query, params)
            companies = []
            
            for row in cursor.fetchall():
                companies.append({
                    'ticker': row[0],
                    'name': row[1],
                    'business_description': row[2],
                    'sector': row[3],
                    'industry': row[4],
                    'exchange': row[5],
                    'market_cap': float(row[6]) if row[6] else None,
                    'website': row[7],
                    'ceo': row[8],
                    'employees': row[9],
                    'address': row[10],
                    'city': row[11],
                    'state': row[12],
                    'country': row[13],
                    'phone': row[14],
                    'last_updated': row[15].isoformat() if row[15] else None,
                    'created_at': row[16].isoformat() if row[16] else None
                })
            
            query_time = time.time() - start_time
            logger.info(f"Companies query returned {len(companies)} companies (took {query_time:.3f}s)")
            
            # Get unique sectors for filter
            start_time = time.time()
            cursor.execute("SELECT DISTINCT sector FROM companies WHERE sector IS NOT NULL ORDER BY sector")
            sectors = [row[0] for row in cursor.fetchall()]
            sectors_time = time.time() - start_time
            logger.debug(f"Found {len(sectors)} unique sectors (query took {sectors_time:.3f}s)")
            
            cursor.close()
            
            logger.info(f"API /api/companies returning {len(companies)} companies (total: {total_count})")
            
            return jsonify({
                'companies': companies,
                'total': total_count,
                'limit': limit,
                'offset': offset,
                'sectors': sectors
            })
            
    except Exception as e:
        logger.error(f"Error fetching companies: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/companies/fetch', methods=['POST'])
def fetch_company_from_fmp():
    """API endpoint to fetch company data from Alpha Vantage (first) or Financial Modeling Prep (fallback) and save to database."""
    logger.info("API /api/companies/fetch called")
    
    data = request.json if request.is_json else {}
    ticker = data.get('ticker', '').strip().upper()
    
    logger.debug(f"Fetch request - ticker: '{ticker}', request data: {data}")
    
    if not ticker:
        logger.warning("Fetch request rejected: ticker is empty")
        return jsonify({'error': 'Ticker is required'}), 400
    
    # Try Alpha Vantage first (already have API key)
    av_api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    fmp_api_key = os.getenv('FMP_API_KEY', '')
    
    company_data = None
    source = None
    
    try:
        # Try Alpha Vantage first
        if av_api_key:
            logger.info(f"Fetching company profile for {ticker} from Alpha Vantage...")
            start_time = time.time()
            company_data = get_alpha_vantage_company_overview(ticker, av_api_key)
            fetch_time = time.time() - start_time
            logger.info(f"Alpha Vantage API fetch for {ticker} took {fetch_time:.2f}s")
            
            if company_data:
                source = 'Alpha Vantage'
        
        # Fallback to FMP if Alpha Vantage didn't work
        if not company_data and fmp_api_key:
            logger.info(f"Alpha Vantage failed, trying Financial Modeling Prep for {ticker}...")
            start_time = time.time()
            company_data = get_fmp_company_profile(ticker, fmp_api_key)
            fetch_time = time.time() - start_time
            logger.info(f"FMP API fetch for {ticker} took {fetch_time:.2f}s")
            
            if company_data:
                source = 'Financial Modeling Prep'
        
        if not company_data:
            error_msg = f'Company profile not found for {ticker}'
            if not av_api_key and not fmp_api_key:
                error_msg += '. Please add ALPHA_VANTAGE_API_KEY or FMP_API_KEY to your .env file.'
            logger.warning(error_msg)
            return jsonify({'error': error_msg}), 404
        
        logger.debug(f"Company data received for {ticker}, saving to database...")
        
        # Save to database
        save_start = time.time()
        success = save_company_to_db(ticker, company_data)
        save_time = time.time() - save_start
        logger.info(f"Database save for {ticker} took {save_time:.2f}s")
        
        if success:
            logger.info(f"Successfully fetched and saved company {ticker} from {source}")
            return jsonify({
                'success': True,
                'message': f'Company {ticker} saved successfully (source: {source})',
                'source': source,
                'data': company_data
            })
        else:
            logger.error(f"Failed to save company {ticker} to database")
            return jsonify({'error': 'Failed to save company to database'}), 500
            
    except Exception as e:
        logger.error(f"Error fetching company {ticker}: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


def process_batch_fetch_thread(tickers, av_api_key, fmp_api_key, total_tickers=None):
    """Process batch fetch in background thread."""
    global batch_fetch_status, batch_fetch_logs, batch_fetch_stop_requested
    
    # Reset stop flag
    batch_fetch_stop_requested = False
    
    # Clear previous logs and initialize
    batch_fetch_logs = []
    add_batch_log('INFO', f"Starting batch fetch for {len(tickers)} tickers (Alpha Vantage: {'Yes' if av_api_key else 'No'}, FMP: {'Yes' if fmp_api_key else 'No'})")
    
    # Initialize status
    batch_fetch_status['running'] = True
    batch_fetch_status['started_at'] = datetime.now().isoformat()
    # Use total_tickers if provided, otherwise keep existing total or use len(tickers)
    is_new_session = False
    if total_tickers is not None:
        # If total changed or was reset, it's a new session
        if batch_fetch_status.get('total', 0) != total_tickers:
            is_new_session = True
        batch_fetch_status['total'] = total_tickers
    elif batch_fetch_status.get('total', 0) == 0:
        batch_fetch_status['total'] = len(tickers)
        is_new_session = True
    
    # Only reset counters if this is a new session (total was just set/changed)
    if is_new_session:
        batch_fetch_status['processed'] = 0
        batch_fetch_status['success'] = 0
        batch_fetch_status['failed'] = 0
    # Otherwise, keep existing cumulative values
    batch_fetch_status['current_ticker'] = None
    batch_fetch_status['message'] = f'Starting batch fetch for {len(tickers)} tickers (total: {batch_fetch_status["total"]})...'
    
    logger.info(f"Starting batch fetch for {len(tickers)} tickers (Alpha Vantage: {'Yes' if av_api_key else 'No'}, FMP: {'Yes' if fmp_api_key else 'No'})")
    # Log progress every N tickers to reduce spam
    progress_log_interval = max(10, len(tickers) // 20)  # Log every 5% or every 10 tickers, whichever is larger
    
    start_time = time.time()
    
    try:
        for idx, ticker in enumerate(tickers, 1):
            # Check if stop was requested
            if batch_fetch_stop_requested:
                add_batch_log('WARNING', 'Stop requested by user. Stopping batch fetch...')
                batch_fetch_status['message'] = 'Stopped by user'
                break
            
            ticker = ticker.strip().upper()
            if not ticker:
                logger.debug(f"Skipping empty ticker at index {idx}")
                continue
            
            # Update status - processed is cumulative across batches
            batch_fetch_status['current_ticker'] = ticker
            # Processed is updated based on success + failed counts, not idx
            batch_fetch_status['message'] = f'Processing {idx}/{len(tickers)}: {ticker}'
            batch_fetch_status['last_updated'] = datetime.now().isoformat()
            
            # Log progress periodically to batch log
            if idx % progress_log_interval == 0 or idx == 1 or idx == len(tickers):
                add_batch_log('INFO', f"Batch progress: {idx}/{len(tickers)} tickers processed ({batch_fetch_status['success']} success, {batch_fetch_status['failed']} failed)")
            
            add_batch_log('DEBUG', f"Processing ticker {idx}/{len(tickers)}: {ticker}")
            
            try:
                # Always reprocess to update the date
                ticker_start = time.time()
                company_data = None
                source = None
                error_message = None
                
                # Check stop flag before API calls
                if batch_fetch_stop_requested:
                    add_batch_log('WARNING', 'Stop requested by user. Stopping batch fetch...')
                    batch_fetch_status['message'] = 'Stopped by user'
                    break
                
                # Try Alpha Vantage first
                if av_api_key:
                    try:
                        company_data = get_alpha_vantage_company_overview(ticker, av_api_key)
                        if company_data:
                            source = 'Alpha Vantage'
                    except Exception as e:
                        error_message = f"Alpha Vantage error: {str(e)}"
                        add_batch_log('WARNING', f"{error_message}")
                
                # Check stop flag after Alpha Vantage call
                if batch_fetch_stop_requested:
                    add_batch_log('WARNING', 'Stop requested by user. Stopping batch fetch...')
                    batch_fetch_status['message'] = 'Stopped by user'
                    break
                
                # Fallback to FMP
                if not company_data and fmp_api_key:
                    try:
                        company_data = get_fmp_company_profile(ticker, fmp_api_key)
                        if company_data:
                            source = 'Financial Modeling Prep'
                    except Exception as e:
                        if not error_message:
                            error_message = f"FMP error: {str(e)}"
                        else:
                            error_message += f"; FMP error: {str(e)}"
                        add_batch_log('WARNING', f"FMP error for {ticker}: {str(e)}")
                
                # Check stop flag after FMP call
                if batch_fetch_stop_requested:
                    add_batch_log('WARNING', 'Stop requested by user. Stopping batch fetch...')
                    batch_fetch_status['message'] = 'Stopped by user'
                    break
                
                fetch_time = time.time() - ticker_start
                
                if company_data:
                    add_batch_log('DEBUG', f"API returned data for {ticker} in {fetch_time:.2f}s")
                    save_start = time.time()
                    success = save_company_to_db(ticker, company_data)
                    save_time = time.time() - save_start
                    
                    if success:
                        add_batch_log('DEBUG', f"Successfully processed {ticker} from {source} (fetch: {fetch_time:.2f}s, save: {save_time:.2f}s)")
                        batch_fetch_status['success'] += 1
                    else:
                        error_msg = f"Database save failed for {ticker}"
                        add_batch_log('ERROR', error_msg)
                        save_company_error(ticker, error_msg)
                        batch_fetch_status['failed'] += 1
                else:
                    # No data from either API - save error
                    if not error_message:
                        error_message = f"No data returned from Alpha Vantage or FMP for {ticker}"
                    add_batch_log('WARNING', error_message)
                    save_company_error(ticker, error_message)
                    batch_fetch_status['failed'] += 1
                
                # Update processed as sum of success + failed (cumulative)
                batch_fetch_status['processed'] = batch_fetch_status['success'] + batch_fetch_status['failed']
                    
                # Rate limiting - wait a bit between requests
                if idx < len(tickers):  # Don't wait after last ticker
                    time.sleep(0.3)  # ~3 requests per second
                    logger.debug(f"Rate limit delay: 0.3s")
                
            except Exception as e:
                error_msg = f"Unexpected error processing {ticker}: {str(e)}"
                add_batch_log('ERROR', error_msg)
                save_company_error(ticker, error_msg)
                batch_fetch_status['failed'] += 1
        
        total_time = time.time() - start_time
        if batch_fetch_stop_requested:
            batch_fetch_status['message'] = f'Stopped: {batch_fetch_status["success"]} success, {batch_fetch_status["failed"]} failed'
            add_batch_log('WARNING', f"Batch fetch stopped by user: {batch_fetch_status['success']} success, {batch_fetch_status['failed']} failed, "
                       f"total time: {total_time:.2f}s")
        else:
            batch_fetch_status['message'] = f'Completed: {batch_fetch_status["success"]} success, {batch_fetch_status["failed"]} failed'
            add_batch_log('INFO', f"Batch fetch completed: {batch_fetch_status['success']} success, {batch_fetch_status['failed']} failed, "
                       f"total time: {total_time:.2f}s, avg: {total_time/len(tickers):.2f}s per ticker")
    
    finally:
        batch_fetch_status['running'] = False
        batch_fetch_status['last_updated'] = datetime.now().isoformat()
        batch_fetch_stop_requested = False
        # Don't reset processed, success, failed - they should be cumulative across batches


def worker_thread_process_tickers(worker_id, tickers, av_api_key, fmp_api_key, results_dict):
    """Worker thread to process a batch of tickers."""
    global batch_fetch_stop_requested
    add_batch_log('INFO', f"Worker {worker_id} starting: processing {len(tickers)} tickers")
    
    success_count = 0
    fail_count = 0
    
    for idx, ticker in enumerate(tickers, 1):
        # Check if stop was requested
        if batch_fetch_stop_requested:
            add_batch_log('WARNING', f'Worker {worker_id}: Stop requested by user. Stopping...')
            break
        
        ticker = ticker.strip().upper()
        if not ticker:
            continue
        
        # Update global status with current ticker
        batch_fetch_status['current_ticker'] = ticker
        batch_fetch_status['last_updated'] = datetime.now().isoformat()
        
        add_batch_log('DEBUG', f"Worker {worker_id}: Processing ticker {idx}/{len(tickers)}: {ticker}")
        
        try:
            # Always reprocess to update the date
            ticker_start = time.time()
            company_data = None
            source = None
            error_message = None
            
            # Check stop flag before API calls
            if batch_fetch_stop_requested:
                add_batch_log('WARNING', f'Worker {worker_id}: Stop requested by user. Stopping...')
                break
            
            # Try Alpha Vantage first
            if av_api_key:
                try:
                    company_data = get_alpha_vantage_company_overview(ticker, av_api_key)
                    if company_data:
                        source = 'Alpha Vantage'
                except Exception as e:
                    error_message = f"Alpha Vantage error: {str(e)}"
                    add_batch_log('WARNING', f"Worker {worker_id}: {error_message}")
            
            # Check stop flag after Alpha Vantage call
            if batch_fetch_stop_requested:
                add_batch_log('WARNING', f'Worker {worker_id}: Stop requested by user. Stopping...')
                break
            
            # Fallback to FMP
            if not company_data and fmp_api_key:
                try:
                    company_data = get_fmp_company_profile(ticker, fmp_api_key)
                    if company_data:
                        source = 'Financial Modeling Prep'
                except Exception as e:
                    if not error_message:
                        error_message = f"FMP error: {str(e)}"
                    else:
                        error_message += f"; FMP error: {str(e)}"
                    add_batch_log('WARNING', f"Worker {worker_id}: FMP error for {ticker}: {str(e)}")
            
            # Check stop flag after FMP call
            if batch_fetch_stop_requested:
                add_batch_log('WARNING', f'Worker {worker_id}: Stop requested by user. Stopping...')
                break
            
            fetch_time = time.time() - ticker_start
            
            if company_data:
                save_start = time.time()
                success = save_company_to_db(ticker, company_data)
                save_time = time.time() - save_start
                
                if success:
                    add_batch_log('DEBUG', f"Worker {worker_id}: Successfully processed {ticker} from {source} (fetch: {fetch_time:.2f}s, save: {save_time:.2f}s)")
                    success_count += 1
                else:
                    error_msg = f"Database save failed for {ticker}"
                    add_batch_log('ERROR', f"Worker {worker_id}: {error_msg}")
                    save_company_error(ticker, error_msg)
                    fail_count += 1
            else:
                # No data from either API - save error
                if not error_message:
                    error_message = f"No data returned from Alpha Vantage or FMP for {ticker}"
                add_batch_log('WARNING', f"Worker {worker_id}: {error_message}")
                save_company_error(ticker, error_message)
                fail_count += 1
            
            # Rate limiting
            if idx < len(tickers):
                time.sleep(0.3)  # ~3 requests per second
        
        except Exception as e:
            error_msg = f"Unexpected error processing {ticker}: {str(e)}"
            add_batch_log('ERROR', f"Worker {worker_id}: {error_msg}")
            save_company_error(ticker, error_msg)
            fail_count += 1
    
    # Store results
    results_dict[worker_id] = {'success': success_count, 'failed': fail_count}
    add_batch_log('INFO', f"Worker {worker_id} completed: {success_count} success, {fail_count} failed")


def start_distributed_company_fetch(tickers, total_tickers=None):
    """Start distributed company fetch across multiple Python threads."""
    global batch_fetch_status, batch_fetch_logs
    
    # Clear previous logs and initialize
    batch_fetch_logs = []
    add_batch_log('INFO', f'Starting distributed fetch for {len(tickers)} tickers (total: {total_tickers if total_tickers is not None else len(tickers)})...')
    
    # Initialize status
    batch_fetch_status['running'] = True
    batch_fetch_status['started_at'] = datetime.now().isoformat()
    # Use total_tickers if provided (for cumulative progress), otherwise keep existing total or use len(tickers)
    is_new_session = False
    if total_tickers is not None:
        # If total changed or was reset, it's a new session
        if batch_fetch_status.get('total', 0) != total_tickers:
            is_new_session = True
        batch_fetch_status['total'] = total_tickers
    elif batch_fetch_status.get('total', 0) == 0:
        batch_fetch_status['total'] = len(tickers)
        is_new_session = True
    
    # Only reset counters if this is a new session (total was just set/changed)
    if is_new_session:
        batch_fetch_status['processed'] = 0
        batch_fetch_status['success'] = 0
        batch_fetch_status['failed'] = 0
    # Otherwise, keep existing cumulative values
    batch_fetch_status['current_ticker'] = None
    batch_fetch_status['message'] = f'Starting distributed fetch for {len(tickers)} tickers (total: {batch_fetch_status["total"]})...'
    
    # Get API keys
    av_api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    fmp_api_key = os.getenv('FMP_API_KEY', '')
    
    # Split tickers across 40 worker threads
    num_workers = 40
    tickers_per_worker = len(tickers) // num_workers
    remainder = len(tickers) % num_workers
    
    worker_tasks = []
    start_idx = 0
    
    for worker_num in range(1, num_workers + 1):
        # Distribute remainder across first workers
        size = tickers_per_worker + (1 if worker_num <= remainder else 0)
        worker_tickers = tickers[start_idx:start_idx + size]
        start_idx += size
        
        if worker_tickers:
            worker_tasks.append((worker_num, worker_tickers))
    
    add_batch_log('INFO', f"Distributing {len(tickers)} tickers across {len(worker_tasks)} worker threads")
    
    # Shared results dictionary
    results_dict = {}
    
    # Start worker threads
    threads = []
    start_time = time.time()
    for worker_num, worker_tickers in worker_tasks:
        add_batch_log('DEBUG', f"Starting worker thread {worker_num} with {len(worker_tickers)} tickers")
        thread = threading.Thread(
            target=worker_thread_process_tickers,
            args=(worker_num, worker_tickers, av_api_key, fmp_api_key, results_dict),
            daemon=True
        )
        thread.start()
        threads.append((worker_num, thread, len(worker_tickers)))
    
    # Monitor workers and update status
    def monitor_workers():
        global batch_fetch_status, batch_fetch_stop_requested
        
        last_logged_progress = 0
        progress_log_interval = max(10, batch_fetch_status['total'] // 20)  # Log every 5% or every 10, whichever is larger
        processed_workers = set()  # Track which workers we've already counted
        
        while True:
            # Check if stop was requested - break immediately
            if batch_fetch_stop_requested:
                add_batch_log('WARNING', 'Stop requested by user. Stopping monitoring and waiting for workers to finish current tickers...')
                batch_fetch_status['running'] = False
                batch_fetch_status['message'] = 'Stopped by user'
                # Wait a bit for workers to see the flag, then break
                time.sleep(1)
                break
            
            all_done = True
            total_success = 0
            total_failed = 0
            total_processed_from_completed = 0
            
            for worker_num, thread, worker_total in threads:
                if thread.is_alive():
                    # Still running
                    all_done = False
                else:
                    # Worker finished - get actual results (only count once)
                    if worker_num in results_dict and worker_num not in processed_workers:
                        result = results_dict[worker_num]
                        worker_success = result.get('success', 0)
                        worker_failed = result.get('failed', 0)
                        worker_processed = worker_success + worker_failed
                        total_success += worker_success
                        total_failed += worker_failed
                        total_processed_from_completed += worker_processed
                        processed_workers.add(worker_num)
            
            # Update status with actual counts from completed workers
            # For running workers, we estimate based on time elapsed (rough estimate)
            if not all_done:
                # Estimate: assume workers process at ~3 tickers per second
                # This is just for progress display, actual counts come from completed workers
                elapsed_time = time.time() - start_time
                estimated_from_running = sum(
                    min(worker_total, int(elapsed_time * 3))
                    for worker_num, thread, worker_total in threads
                    if thread.is_alive()
                )
                total_processed = total_processed_from_completed + estimated_from_running
            else:
                total_processed = total_processed_from_completed
            
            # Use actual success/failed counts
            batch_fetch_status['success'] = total_success
            batch_fetch_status['failed'] = total_failed
            # Processed is the sum of success and failed (actual counts)
            batch_fetch_status['processed'] = min(total_success + total_failed, batch_fetch_status['total'])
            batch_fetch_status['last_updated'] = datetime.now().isoformat()
            batch_fetch_status['message'] = f'Processing: {batch_fetch_status["success"]} success, {batch_fetch_status["failed"]} failed'
            
            # Log progress periodically
            current_progress = batch_fetch_status['processed']
            if current_progress - last_logged_progress >= progress_log_interval or all_done:
                add_batch_log('INFO', f"Batch fetch progress: {current_progress}/{batch_fetch_status['total']} processed ({batch_fetch_status['success']} success, {batch_fetch_status['failed']} failed)")
                last_logged_progress = current_progress
            
            if all_done:
                break
            
            time.sleep(2)  # Check every 2 seconds
        
        # Final status
        batch_fetch_status['running'] = False
        if batch_fetch_stop_requested:
            batch_fetch_status['message'] = f'Stopped: {batch_fetch_status["success"]} success, {batch_fetch_status["failed"]} failed'
            add_batch_log('WARNING', f"Distributed fetch stopped by user: {batch_fetch_status['success']} success, {batch_fetch_status['failed']} failed")
        else:
            batch_fetch_status['message'] = f'Completed: {batch_fetch_status["success"]} success, {batch_fetch_status["failed"]} failed'
            add_batch_log('INFO', f"Distributed fetch completed: {batch_fetch_status['success']} success, {batch_fetch_status['failed']} failed")
        batch_fetch_status['current_ticker'] = None
        batch_fetch_status['last_updated'] = datetime.now().isoformat()
        batch_fetch_stop_requested = False
    
    # Start monitoring in background
    monitor_thread = threading.Thread(target=monitor_workers, daemon=True)
    monitor_thread.start()


@app.route('/api/companies/fetch-batch', methods=['POST'])
def fetch_companies_batch():
    """API endpoint to fetch multiple companies from Alpha Vantage (first) or Financial Modeling Prep (fallback)."""
    global batch_fetch_status
    
    logger.info("API /api/companies/fetch-batch called")
    
    data = request.json if request.is_json else {}
    tickers = data.get('tickers', [])
    use_distributed = data.get('distributed', True)  # Default to distributed
    total_tickers = data.get('total_tickers', None)  # Optional: total across all batches
    
    logger.debug(f"Batch fetch request - tickers count: {len(tickers) if isinstance(tickers, list) else 0}, distributed: {use_distributed}, total_tickers: {total_tickers}")
    
    if not tickers or not isinstance(tickers, list):
        logger.warning(f"Batch fetch rejected: invalid tickers parameter - {type(tickers)}")
        return jsonify({'error': 'tickers must be a non-empty list'}), 400
    
    # Check if already running
    if batch_fetch_status['running']:
        return jsonify({'error': 'Batch fetch is already running'}), 409
    
    # If total_tickers is provided, always use it as the total (for cumulative progress across batches)
    # This allows subsequent batches to update the total if needed
    if total_tickers is not None:
        batch_fetch_status['total'] = total_tickers
        logger.info(f"Setting total tickers to {total_tickers} for cumulative progress tracking")
    # If no total_tickers provided but we have an existing total, keep it (for subsequent batches)
    elif batch_fetch_status.get('total', 0) > 0:
        # Keep existing total for cumulative tracking
        logger.debug(f"Keeping existing total: {batch_fetch_status['total']}")
    
    # Try Alpha Vantage first, then FMP
    av_api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    fmp_api_key = os.getenv('FMP_API_KEY', '')
    
    if not av_api_key and not fmp_api_key:
        error_msg = 'Neither ALPHA_VANTAGE_API_KEY nor FMP_API_KEY found in environment variables. Please add at least one to your .env file.'
        logger.error(error_msg)
        return jsonify({
            'error': error_msg,
            'hint': 'Add ALPHA_VANTAGE_API_KEY=your_api_key_here (or FMP_API_KEY) to your .env file and restart the application'
        }), 500
    
    # Use distributed threads if requested and available
    if use_distributed and len(tickers) > 10:
        try:
            start_distributed_company_fetch(tickers, total_tickers=total_tickers)
            return jsonify({
                'success': True,
                'message': f'Distributed batch fetch started for {len(tickers)} tickers (total: {total_tickers or len(tickers)}) across 20 worker threads. Use /api/companies/batch-status to check progress.',
                'total': total_tickers if total_tickers is not None else len(tickers),
                'distributed': True
            })
        except Exception as e:
            logger.warning(f"Distributed fetch failed, falling back to local: {str(e)}", exc_info=True)
            # Fall through to local processing
    
    # Fallback to local processing
    thread = threading.Thread(
        target=process_batch_fetch_thread,
        args=(tickers, av_api_key, fmp_api_key),
        daemon=True
    )
    thread.start()
    
    return jsonify({
        'success': True,
        'message': f'Batch fetch started for {len(tickers)} tickers. Use /api/companies/batch-status to check progress.',
        'total': len(tickers),
        'distributed': False
    })


@app.route('/api/companies/batch-status')
def get_batch_fetch_status():
    """API endpoint to get current batch fetch status."""
    global batch_fetch_status
    
    # Also get current count of companies in database
    try:
        db_manager = get_db_manager()
        with db_manager:
            if db_manager.conn:
                cursor = db_manager.conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM companies")
                total_companies = cursor.fetchone()[0]
                cursor.close()
                batch_fetch_status['total_companies_in_db'] = total_companies
    except Exception as e:
        logger.error(f"Error getting company count: {str(e)}")
        batch_fetch_status['total_companies_in_db'] = None
    
    return jsonify(batch_fetch_status)


@app.route('/api/companies/batch-logs')
def get_batch_fetch_logs():
    """API endpoint to get batch fetch process logs."""
    global batch_fetch_logs
    return jsonify({'logs': batch_fetch_logs})


@app.route('/api/companies/batch-stop', methods=['POST'])
def stop_batch_fetch():
    """API endpoint to stop the running batch fetch process."""
    global batch_fetch_status, batch_fetch_stop_requested
    
    if not batch_fetch_status['running']:
        return jsonify({'error': 'No batch fetch is currently running'}), 400
    
    batch_fetch_stop_requested = True
    batch_fetch_status['running'] = False  # Immediately mark as not running
    batch_fetch_status['message'] = 'Stopping...'
    add_batch_log('WARNING', 'Stop requested by user - workers will stop after current ticker')
    logger.info("Batch fetch stop requested by user")
    
    return jsonify({
        'success': True,
        'message': 'Stop request sent. Process will stop after completing current ticker(s).'
    })


@app.route('/api/companies/get-tickers-from-articles')
def get_tickers_from_articles():
    """API endpoint to get list of unique tickers from articles table."""
    logger.debug("API /api/companies/get-tickers-from-articles called")
    
    try:
        db_manager = get_db_manager()
        with db_manager:
            if not db_manager.conn:
                raise ConnectionError("Database connection not established")
            
            cursor = db_manager.conn.cursor()
            
            logger.debug("Fetching unique tickers from articles table...")
            start_time = time.time()
            
            # Get all unique tickers from articles
            cursor.execute("""
                SELECT DISTINCT jsonb_array_elements(ticker_sentiment)->>'ticker' as ticker
                FROM articles
                WHERE ticker_sentiment IS NOT NULL
                    AND jsonb_array_length(ticker_sentiment) > 0
                ORDER BY ticker
            """)
            
            all_tickers = [row[0] for row in cursor.fetchall() if row[0]]
            query_time = time.time() - start_time
            logger.debug(f"Found {len(all_tickers)} unique tickers from articles (query took {query_time:.2f}s)")
            
            logger.debug("Fetching existing companies from database...")
            start_time = time.time()
            
            # Get which ones already have descriptions (with actual non-empty descriptions)
            # Exclude placeholder values like "No description", "None", etc.
            cursor.execute("""
                SELECT ticker 
                FROM companies 
                WHERE business_description IS NOT NULL 
                AND TRIM(business_description) != ''
                AND UPPER(TRIM(business_description)) NOT IN ('NO DESCRIPTION', 'NONE', 'N/A', 'NA', '-')
                AND LENGTH(TRIM(business_description)) > 5
            """)
            existing_tickers = {row[0] for row in cursor.fetchall()}
            existing_time = time.time() - start_time
            logger.debug(f"Found {len(existing_tickers)} companies with descriptions (query took {existing_time:.2f}s)")
            
            missing_tickers = [t for t in all_tickers if t not in existing_tickers]
            logger.debug(f"Missing descriptions: {len(missing_tickers)} tickers")
            
            cursor.close()
            
            return jsonify({
                'tickers': all_tickers,
                'total': len(all_tickers),
                'with_descriptions': len(existing_tickers),
                'missing_descriptions': missing_tickers
            })
            
    except Exception as e:
        logger.error(f"Error getting tickers from articles: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/sql-query', methods=['GET', 'POST'])
def sql_query():
    """SQL query interface for direct database queries."""
    # Get database catalog (tables and views)
    db_manager = get_db_manager()
    tables = []
    views = []
    
    try:
        with db_manager:
            if db_manager.conn:
                cursor = db_manager.conn.cursor()
                
                # Get all tables
                cursor.execute("""
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                        AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                """)
                tables = [row[0] for row in cursor.fetchall()]
                
                # Get all views
                cursor.execute("""
                    SELECT table_name 
                    FROM information_schema.views 
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """)
                views = [row[0] for row in cursor.fetchall()]
                
                cursor.close()
    except Exception as e:
        logger.error(f"Error fetching database catalog: {str(e)}", exc_info=True)
    
    if request.method == 'GET':
        return render_template('sql_query.html', tables=tables, views=views)
    
    # POST: Execute query
    query = request.form.get('sql_query', '').strip()
    
    if not query:
        flash('Please enter a SQL query', 'error')
        return render_template('sql_query.html', query=query, tables=tables, views=views)
    
    # Security: Allow SELECT, CREATE, DROP, ALTER, INSERT, UPDATE, DELETE
    # Block potentially dangerous operations like DROP DATABASE, etc.
    query_upper = query.upper().strip()
    blocked_keywords = ['DROP DATABASE', 'DROP SCHEMA', 'TRUNCATE', 'TRUNCATE TABLE']
    
    for blocked in blocked_keywords:
        if blocked in query_upper:
            error_msg = f"Operation '{blocked}' is not allowed for security reasons."
            logger.warning(f"Blocked dangerous query: {query[:100]}")
            return render_template('sql_query.html', query=query, error=error_msg, tables=tables, views=views)
    
    try:
        import time
        db_manager = get_db_manager()
        with db_manager:
            if not db_manager.conn:
                raise ConnectionError("Database connection not established")
            
            cursor = db_manager.conn.cursor()
            
            # Execute query with timeout protection
            start_time = time.time()
            cursor.execute(query)
            execution_time = time.time() - start_time
            
            # Check if query returns results (SELECT) or is DDL/DML
            results = None
            columns = None
            
            if cursor.description:
                # SELECT query - fetch results
                columns = [desc[0] for desc in cursor.description]
                results = cursor.fetchall()  # Fetch all results (no limit)
            else:
                # DDL/DML query (CREATE, INSERT, UPDATE, DELETE, etc.)
                db_manager.conn.commit()
                rows_affected = cursor.rowcount if cursor.rowcount >= 0 else 0
                flash(f'Query executed successfully. Rows affected: {rows_affected}', 'success')
            
            cursor.close()
            
            logger.info(f"SQL query executed successfully in {execution_time:.3f}s")
            
            # Refresh catalog after DDL operations
            if query_upper.startswith(('CREATE', 'DROP', 'ALTER')):
                with db_manager:
                    if db_manager.conn:
                        cursor = db_manager.conn.cursor()
                        cursor.execute("""
                            SELECT table_name 
                            FROM information_schema.tables 
                            WHERE table_schema = 'public' 
                                AND table_type = 'BASE TABLE'
                            ORDER BY table_name
                        """)
                        tables = [row[0] for row in cursor.fetchall()]
                        cursor.execute("""
                            SELECT table_name 
                            FROM information_schema.views 
                            WHERE table_schema = 'public'
                            ORDER BY table_name
                        """)
                        views = [row[0] for row in cursor.fetchall()]
                        cursor.close()
            
            return render_template('sql_query.html', 
                                 query=query, 
                                 results=results, 
                                 columns=columns,
                                 execution_time=execution_time,
                                 tables=tables,
                                 views=views)
            
    except Exception as e:
        error_msg = str(e)
        logger.error(f"SQL query error: {error_msg}", exc_info=True)
        return render_template('sql_query.html', query=query, error=error_msg, tables=tables, views=views)


@app.route('/api/sql-query/view-definition/<view_name>')
def get_view_definition(view_name):
    """Return the SQL definition of a view for the SQL Query GUI."""
    import re
    if not view_name or not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', view_name):
        return jsonify({'error': 'Invalid view name'}), 400
    try:
        db_manager = get_db_manager()
        with db_manager:
            if not db_manager.conn:
                return jsonify({'error': 'Database connection not established'}), 500
            cursor = db_manager.conn.cursor()
            # pg_views.definition contains the full view definition in PostgreSQL
            cursor.execute("""
                SELECT definition 
                FROM pg_views 
                WHERE schemaname = 'public' AND viewname = %s
            """, (view_name,))
            row = cursor.fetchone()
            cursor.close()
            if not row:
                return jsonify({'error': f'View "{view_name}" not found'}), 404
            return jsonify({'view_name': view_name, 'definition': row[0]})
    except Exception as e:
        logger.error(f"Error fetching view definition for {view_name}: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


def ensure_companies_table_exists():
    """Ensure the companies table exists in the database."""
    logger.info("Ensuring companies table exists")
    db_manager = get_db_manager()
    
    try:
        with db_manager:
            if not db_manager.conn:
                logger.error("Database connection not established")
                return
            
            cursor = db_manager.conn.cursor()
            
            # Check if table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_name = 'companies'
                )
            """)
            table_exists = cursor.fetchone()[0]
            
            if not table_exists:
                logger.info("Creating companies table...")
                cursor.execute("""
                    CREATE TABLE companies (
                        ticker TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        business_description TEXT,
                        pipeline_description TEXT,
                        sector TEXT,
                        industry TEXT,
                        exchange TEXT,
                        market_cap NUMERIC,
                        website TEXT,
                        ceo TEXT,
                        employees INTEGER,
                        address TEXT,
                        city TEXT,
                        state TEXT,
                        country TEXT,
                        phone TEXT,
                        embedding_vector FLOAT[],
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_error_date DATE,
                        last_error_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                cursor.execute("""
                    CREATE INDEX idx_companies_ticker ON companies(ticker)
                """)
                cursor.execute("""
                    CREATE INDEX idx_companies_sector ON companies(sector)
                """)
                cursor.execute("""
                    CREATE INDEX idx_companies_industry ON companies(industry)
                """)
                cursor.execute("""
                    CREATE INDEX idx_companies_name ON companies(name)
                """)
                cursor.execute("""
                    CREATE INDEX idx_companies_last_error_date ON companies(last_error_date)
                """)
                
                db_manager.conn.commit()
                logger.info("companies table created successfully")
            else:
                logger.info("companies table already exists, checking for error columns...")
                # Add error columns if they don't exist (for existing databases)
                try:
                    cursor.execute("""
                        SELECT column_name FROM information_schema.columns 
                        WHERE table_name = 'companies' AND column_name = 'last_error_date'
                    """)
                    if not cursor.fetchone():
                        logger.info("Adding last_error_date and last_error_message columns...")
                        cursor.execute("ALTER TABLE companies ADD COLUMN last_error_date DATE")
                        cursor.execute("ALTER TABLE companies ADD COLUMN last_error_message TEXT")
                        cursor.execute("CREATE INDEX IF NOT EXISTS idx_companies_last_error_date ON companies(last_error_date)")
                        db_manager.conn.commit()
                        logger.info("Error columns added successfully")
                    else:
                        logger.info("Error columns already exist")
                except Exception as e:
                    logger.warning(f"Could not add error columns (may already exist): {str(e)}")
                    db_manager.conn.rollback()
            
            cursor.close()
            
    except Exception as e:
        logger.error(f"Error ensuring companies table exists: {str(e)}", exc_info=True)
        if db_manager.conn:
            db_manager.conn.rollback()


# Global state for vectorization status
vectorization_status = {
    'running': False,
    'message': '',
    'started_at': None,
    'total': 0,
    'processed': 0,
    'success': 0,
    'failed': 0,
    'current_ticker': None,
    'last_updated': None,
    'tokens_used': 0,
    'estimated_cost': 0.0,
    'model_name': None
}


def ensure_company_embeddings_table_exists():
    """Ensure the company_embeddings table exists in the database."""
    logger.info("Ensuring company_embeddings table exists")
    db_manager = get_db_manager()
    
    try:
        with db_manager:
            if not db_manager.conn:
                logger.error("Database connection not established")
                return
            
            cursor = db_manager.conn.cursor()
            
            # Check if table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_name = 'company_embeddings'
                )
            """)
            table_exists = cursor.fetchone()[0]
            
            if not table_exists:
                logger.info("Creating company_embeddings table...")
                cursor.execute("""
                    CREATE TABLE company_embeddings (
                        id SERIAL PRIMARY KEY,
                        ticker TEXT NOT NULL REFERENCES companies(ticker) ON DELETE CASCADE,
                        model_name TEXT NOT NULL,
                        embedding_vector FLOAT[] NOT NULL,
                        dimension INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(ticker, model_name)
                    )
                """)
                
                cursor.execute("""
                    CREATE INDEX idx_company_embeddings_ticker ON company_embeddings(ticker)
                """)
                cursor.execute("""
                    CREATE INDEX idx_company_embeddings_model ON company_embeddings(model_name)
                """)
                
                db_manager.conn.commit()
                logger.info("company_embeddings table created successfully")
            else:
                logger.info("company_embeddings table already exists")
            
            cursor.close()
            
    except Exception as e:
        logger.error(f"Error ensuring company_embeddings table exists: {str(e)}", exc_info=True)
        if db_manager.conn:
            db_manager.conn.rollback()


def ensure_correlation_matrix_table_exists():
    """Ensure the company_correlation_matrix table exists in the database."""
    logger.info("Ensuring company_correlation_matrix table exists")
    db_manager = get_db_manager()
    
    try:
        with db_manager:
            if not db_manager.conn:
                logger.error("Database connection not established")
                return
            
            cursor = db_manager.conn.cursor()
            
            # Check if table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_name = 'company_correlation_matrix'
                )
            """)
            table_exists = cursor.fetchone()[0]
            
            if not table_exists:
                logger.info("Creating company_correlation_matrix table...")
                cursor.execute("""
                    CREATE TABLE company_correlation_matrix (
                        id SERIAL PRIMARY KEY,
                        model_name TEXT NOT NULL,
                        tickers TEXT[] NOT NULL,
                        matrix_data JSONB NOT NULL,
                        calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        companies_count INTEGER NOT NULL,
                        UNIQUE(model_name)
                    )
                """)
                
                cursor.execute("""
                    CREATE INDEX idx_company_correlation_matrix_model ON company_correlation_matrix(model_name)
                """)
                cursor.execute("""
                    CREATE INDEX idx_company_correlation_matrix_calculated_at ON company_correlation_matrix(calculated_at)
                """)
                
                db_manager.conn.commit()
                logger.info("company_correlation_matrix table created successfully")
            else:
                logger.info("company_correlation_matrix table already exists")
            
            cursor.close()
            
    except Exception as e:
        logger.error(f"Error ensuring company_correlation_matrix table exists: {str(e)}", exc_info=True)
        if db_manager.conn:
            db_manager.conn.rollback()


def ensure_cross_diffused_sentiment_table_exists():
    """Ensure the ticker_cross_diffused_sentiment table exists in the database."""
    logger.info("Ensuring ticker_cross_diffused_sentiment table exists")
    db_manager = get_db_manager()
    
    try:
        with db_manager:
            if not db_manager.conn:
                logger.error("Database connection not established")
                return
            
            cursor = db_manager.conn.cursor()
            
            # Check if table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_name = 'ticker_cross_diffused_sentiment'
                )
            """)
            table_exists = cursor.fetchone()[0]
            
            if not table_exists:
                logger.info("Creating ticker_cross_diffused_sentiment table...")
                cursor.execute("""
                    CREATE TABLE ticker_cross_diffused_sentiment (
                        id SERIAL PRIMARY KEY,
                        ticker TEXT NOT NULL,
                        date DATE NOT NULL,
                        model_name TEXT NOT NULL,
                        weighted_sentiment_cross_diffused NUMERIC(10, 6) NOT NULL,
                        correlation_threshold NUMERIC(5, 4) DEFAULT 0.3,
                        time_decay_days INTEGER DEFAULT 7,
                        calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(ticker, date, model_name)
                    )
                """)
                
                cursor.execute("""
                    CREATE INDEX idx_ticker_cross_diffused_ticker ON ticker_cross_diffused_sentiment(ticker)
                """)
                cursor.execute("""
                    CREATE INDEX idx_ticker_cross_diffused_date ON ticker_cross_diffused_sentiment(date)
                """)
                cursor.execute("""
                    CREATE INDEX idx_ticker_cross_diffused_model ON ticker_cross_diffused_sentiment(model_name)
                """)
                cursor.execute("""
                    CREATE INDEX idx_ticker_cross_diffused_ticker_date ON ticker_cross_diffused_sentiment(ticker, date)
                """)
                
                db_manager.conn.commit()
                logger.info("ticker_cross_diffused_sentiment table created successfully")
            else:
                logger.info("ticker_cross_diffused_sentiment table already exists")
            
            cursor.close()
            
    except Exception as e:
        logger.error(f"Error ensuring ticker_cross_diffused_sentiment table exists: {str(e)}", exc_info=True)
        if db_manager.conn:
            db_manager.conn.rollback()


def ensure_decayed_sentiment_table_exists():
    """Ensure the ticker_decayed_sentiment table exists in the database."""
    logger.info("Ensuring ticker_decayed_sentiment table exists")
    db_manager = get_db_manager()
    
    try:
        with db_manager:
            if not db_manager.conn:
                logger.error("Database connection not established")
                return
            
            cursor = db_manager.conn.cursor()
            
            # Check if table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_name = 'ticker_decayed_sentiment'
                )
            """)
            table_exists = cursor.fetchone()[0]
            
            if not table_exists:
                logger.info("Creating ticker_decayed_sentiment table...")
                cursor.execute("""
                    CREATE TABLE ticker_decayed_sentiment (
                        id SERIAL PRIMARY KEY,
                        ticker TEXT NOT NULL,
                        date DATE NOT NULL,
                        weighted_sentiment_decayed NUMERIC(10, 6) NOT NULL,
                        half_life_days INTEGER DEFAULT 7,
                        lookback_days INTEGER DEFAULT 30,
                        calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(ticker, date)
                    )
                """)
                
                cursor.execute("""
                    CREATE INDEX idx_ticker_decayed_ticker ON ticker_decayed_sentiment(ticker)
                """)
                cursor.execute("""
                    CREATE INDEX idx_ticker_decayed_date ON ticker_decayed_sentiment(date)
                """)
                cursor.execute("""
                    CREATE INDEX idx_ticker_decayed_ticker_date ON ticker_decayed_sentiment(ticker, date)
                """)
                
                db_manager.conn.commit()
                logger.info("ticker_decayed_sentiment table created successfully")
            else:
                logger.info("ticker_decayed_sentiment table already exists")
            
            cursor.close()
            
    except Exception as e:
        logger.error(f"Error ensuring ticker_decayed_sentiment table exists: {str(e)}", exc_info=True)
        if db_manager.conn:
            db_manager.conn.rollback()


def generate_company_embedding_text(business_description, sector, industry):
    """
    Combine company fields into a single text for embedding.
    Format: Sector and Industry first (metadata), then description.
    
    Args:
        business_description: Company business description
        sector: Company sector
        industry: Company industry
    
    Returns:
        Combined text string or None if no text available
    """
    parts = []
    
    # Metadata first - helps with semantic clustering
    if sector:
        parts.append(f"Sector: {sector}")
    if industry:
        parts.append(f"Industry: {industry}")
    
    # Main description
    if business_description:
        parts.append(business_description)
    
    return "\n\n".join(parts) if parts else None


def count_tokens(text, model="text-embedding-3-small"):
    """
    Count tokens in text using tiktoken.
    
    Args:
        text: Text to count tokens for
        model: OpenAI model name
    
    Returns:
        Number of tokens
    """
    if not OPENAI_AVAILABLE:
        return 0
    
    try:
        # Map model names to tiktoken encoding names
        encoding_map = {
            'text-embedding-3-small': 'cl100k_base',
            'text-embedding-3-large': 'cl100k_base',
            'text-embedding-ada-002': 'cl100k_base'
        }
        encoding_name = encoding_map.get(model, 'cl100k_base')
        encoding = tiktoken.get_encoding(encoding_name)
        return len(encoding.encode(text))
    except Exception as e:
        logger.warning(f"Error counting tokens: {str(e)}")
        # Fallback: rough estimate (1 token ≈ 4 characters)
        return len(text) // 4


def calculate_embedding_cost(tokens, model="text-embedding-3-small"):
    """
    Calculate cost for embedding based on token count and model.
    
    Args:
        tokens: Number of tokens
        model: OpenAI model name
    
    Returns:
        Cost in USD
    """
    # Pricing per 1M tokens (as of 2024)
    pricing = {
        'text-embedding-3-small': 0.02,
        'text-embedding-3-large': 0.13,
        'text-embedding-ada-002': 0.10
    }
    price_per_million = pricing.get(model, 0.02)
    return (tokens / 1_000_000) * price_per_million


def generate_openai_embedding(text, model="text-embedding-3-small"):
    """
    Generate embedding using OpenAI API.
    
    Args:
        text: Text to embed
        model: OpenAI embedding model name
    
    Returns:
        List of floats representing the embedding vector
    """
    if not OPENAI_AVAILABLE:
        raise ImportError("OpenAI library not available")
    
    openai_key = os.getenv('OPENAI_API_KEY')
    if not openai_key:
        raise ValueError("OPENAI_API_KEY not found in environment variables")
    
    client = openai.OpenAI(api_key=openai_key)
    response = client.embeddings.create(
        model=model,
        input=text
    )
    return response.data[0].embedding


def worker_thread_vectorize_companies(worker_id, companies_batch, model_name, dimension, results_dict):
    """Worker thread to vectorize a batch of companies."""
    logger.info(f"Vectorization worker {worker_id} starting: processing {len(companies_batch)} companies")
    
    success_count = 0
    failed_count = 0
    worker_tokens = 0
    worker_cost = 0.0
    
    db_manager = get_db_manager()
    
    try:
        with db_manager:
            if not db_manager.conn:
                logger.error(f"Worker {worker_id}: Database connection not established")
                results_dict[worker_id] = {
                    'success': 0,
                    'failed': len(companies_batch),
                    'tokens': 0,
                    'cost': 0.0
                }
                return
            
            cursor = db_manager.conn.cursor()
            
            for idx, (ticker, description, sector, industry, last_updated) in enumerate(companies_batch, 1):
                try:
                    # Update global status with current ticker
                    vectorization_status['current_ticker'] = ticker
                    vectorization_status['last_updated'] = datetime.now().isoformat()
                    
                    # Combine all fields
                    combined_text = generate_company_embedding_text(
                        description, sector, industry
                    )
                    
                    if not combined_text:
                        logger.warning(f"Worker {worker_id}: Skipping {ticker}: no text to vectorize")
                        failed_count += 1
                        continue
                    
                    # Count tokens before API call
                    tokens = count_tokens(combined_text, model_name)
                    cost = calculate_embedding_cost(tokens, model_name)
                    
                    # Generate embedding
                    embedding = generate_openai_embedding(combined_text, model=model_name)
                    
                    # Update worker totals
                    worker_tokens += tokens
                    worker_cost += cost
                    
                    # Save to embeddings table
                    cursor.execute("""
                        INSERT INTO company_embeddings 
                            (ticker, model_name, embedding_vector, dimension, updated_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (ticker, model_name) 
                        DO UPDATE SET
                            embedding_vector = EXCLUDED.embedding_vector,
                            dimension = EXCLUDED.dimension,
                            updated_at = CURRENT_TIMESTAMP
                    """, (ticker, model_name, embedding, dimension))
                    
                    success_count += 1
                    
                    # Commit every 10 companies for safety
                    if idx % 10 == 0:
                        db_manager.conn.commit()
                        logger.debug(f"Worker {worker_id}: Vectorized {idx}/{len(companies_batch)} companies")
                
                except Exception as e:
                    failed_count += 1
                    logger.error(f"Worker {worker_id}: Error vectorizing {ticker}: {str(e)}", exc_info=True)
            
            # Final commit
            db_manager.conn.commit()
            cursor.close()
            
            results_dict[worker_id] = {
                'success': success_count,
                'failed': failed_count,
                'tokens': worker_tokens,
                'cost': worker_cost
            }
            
            logger.info(f"Worker {worker_id} completed: {success_count} success, {failed_count} failed, "
                       f"{worker_tokens:,} tokens, ${worker_cost:.4f} cost")
            
    except Exception as e:
        logger.error(f"Worker {worker_id}: Fatal error: {str(e)}", exc_info=True)
        results_dict[worker_id] = {
            'success': success_count,
            'failed': failed_count + len(companies_batch) - success_count - failed_count,
            'tokens': worker_tokens,
            'cost': worker_cost
        }


def start_distributed_vectorization(companies, model_name="text-embedding-3-small"):
    """Start distributed vectorization using Python threads."""
    global vectorization_status
    
    total = len(companies)
    logger.info(f"Starting distributed vectorization for {total} companies with {model_name}")
    
    # Determine dimension based on model
    dimension_map = {
        'text-embedding-3-small': 1536,
        'text-embedding-3-large': 3072,
        'text-embedding-ada-002': 1536
    }
    dimension = dimension_map.get(model_name, 1536)
    
    # Initialize status
    vectorization_status.update({
        'running': True,
        'message': f'Vectorizing {total} companies across 20 worker threads...',
        'started_at': datetime.now().isoformat(),
        'total': total,
        'processed': 0,
        'success': 0,
        'failed': 0,
        'current_ticker': None,
        'last_updated': datetime.now().isoformat(),
        'tokens_used': 0,
        'estimated_cost': 0.0,
        'model_name': model_name
    })
    
    # Split companies across 20 worker threads
    num_workers = 40
    companies_per_worker = total // num_workers
    remainder = total % num_workers
    
    worker_tasks = []
    start_idx = 0
    
    for worker_num in range(1, num_workers + 1):
        # Distribute remainder across first workers
        size = companies_per_worker + (1 if worker_num <= remainder else 0)
        worker_companies = companies[start_idx:start_idx + size]
        start_idx += size
        
        if worker_companies:
            worker_tasks.append((worker_num, worker_companies))
    
    logger.info(f"Distributing {total} companies across {len(worker_tasks)} worker threads")
    
    # Shared results dictionary
    results_dict = {}
    
    # Start worker threads
    threads = []
    for worker_num, worker_companies in worker_tasks:
        logger.info(f"Starting vectorization worker thread {worker_num} with {len(worker_companies)} companies")
        thread = threading.Thread(
            target=worker_thread_vectorize_companies,
            args=(worker_num, worker_companies, model_name, dimension, results_dict),
            daemon=True
        )
        thread.start()
        threads.append((worker_num, thread, len(worker_companies)))
    
    # Monitor workers and update status
    def monitor_workers():
        global vectorization_status
        
        total_processed = 0
        total_success = 0
        total_failed = 0
        total_tokens = 0
        total_cost = 0.0
        processed_per_worker = {wn: 0 for wn, _, _ in threads}
        
        while True:
            all_done = True
            current_tickers = []
            
            for worker_num, thread, worker_total in threads:
                if thread.is_alive():
                    # Still running
                    all_done = False
                    # Estimate progress (rough)
                    if processed_per_worker[worker_num] < worker_total:
                        processed_per_worker[worker_num] = min(
                            processed_per_worker[worker_num] + 1,
                            worker_total
                        )
                else:
                    # Worker finished
                    if worker_num in results_dict:
                        result = results_dict[worker_num]
                        total_success += result.get('success', 0)
                        total_failed += result.get('failed', 0)
                        total_tokens += result.get('tokens', 0)
                        total_cost += result.get('cost', 0.0)
                        logger.info(f"Vectorization worker {worker_num} completed: {result.get('success', 0)} success, {result.get('failed', 0)} failed")
                        processed_per_worker[worker_num] = worker_total
            
            # Update status
            total_processed = sum(processed_per_worker.values())
            vectorization_status.update({
                'processed': min(total_processed, total),
                'success': total_success,
                'failed': total_failed,
                'tokens_used': total_tokens,
                'estimated_cost': round(total_cost, 6),
                'last_updated': datetime.now().isoformat()
            })
            
            if all_done:
                break
            
            time.sleep(2)  # Check every 2 seconds
        
        # Final status
        vectorization_status.update({
            'running': False,
            'message': f'Completed: {total_success} successful, {total_failed} failed',
            'processed': total,
            'success': total_success,
            'failed': total_failed,
            'current_ticker': None,
            'last_updated': datetime.now().isoformat(),
            'tokens_used': total_tokens,
            'estimated_cost': round(total_cost, 6)
        })
        
        logger.info(f"Distributed vectorization completed: {total_success} successful, {total_failed} failed out of {total} total. "
                   f"Tokens: {total_tokens:,}, Cost: ${total_cost:.4f}")
    
    # Start monitoring in background
    monitor_thread = threading.Thread(target=monitor_workers, daemon=True)
    monitor_thread.start()


def vectorize_companies_openai(model_name="text-embedding-3-small"):
    """
    Vectorize all company descriptions using OpenAI embeddings.
    Includes business_description, sector, and industry.
    
    Args:
        model_name: OpenAI embedding model name (default: text-embedding-3-small)
    
    Returns:
        dict with statistics about the vectorization process
    """
    global vectorization_status
    
    if not OPENAI_AVAILABLE:
        error_msg = "OpenAI library not available. Please install: pip install openai tiktoken"
        logger.error(error_msg)
        vectorization_status.update({
            'running': False,
            'message': error_msg,
            'failed': 0,
            'success': 0
        })
        return {'error': error_msg}
    
    openai_key = os.getenv('OPENAI_API_KEY')
    if not openai_key:
        error_msg = "OPENAI_API_KEY not found in environment variables"
        logger.error(error_msg)
        vectorization_status.update({
            'running': False,
            'message': error_msg,
            'failed': 0,
            'success': 0
        })
        return {'error': error_msg}
    
    # Determine dimension based on model
    dimension_map = {
        'text-embedding-3-small': 1536,
        'text-embedding-3-large': 3072,
        'text-embedding-ada-002': 1536
    }
    dimension = dimension_map.get(model_name, 1536)
    
    db_manager = get_db_manager()
    
    try:
        with db_manager:
            if not db_manager.conn:
                raise ConnectionError("Database connection not established")
            
            cursor = db_manager.conn.cursor()
            
            # First, let's check how many companies have descriptions
            cursor.execute("""
                SELECT COUNT(*) 
                FROM companies 
                WHERE business_description IS NOT NULL 
                AND TRIM(business_description) != ''
                AND UPPER(TRIM(business_description)) NOT IN ('NO DESCRIPTION', 'NONE', 'N/A', 'NA', '-')
                AND LENGTH(TRIM(business_description)) > 5
            """)
            total_with_descriptions = cursor.fetchone()[0]
            logger.info(f"Total companies with descriptions: {total_with_descriptions}")
            
            # Check how many already have embeddings
            cursor.execute("""
                SELECT COUNT(DISTINCT ticker) 
                FROM company_embeddings 
                WHERE model_name = %s
            """, (model_name,))
            already_vectorized = cursor.fetchone()[0]
            logger.info(f"Companies already vectorized with {model_name}: {already_vectorized}")
            logger.info(f"Companies needing vectorization: {total_with_descriptions - already_vectorized}")
            
            # Get companies that need vectorization
            # IMPORTANT: Only process companies that don't have embeddings yet for this model
            # OR companies that were updated after the embedding was created (need re-vectorization)
            cursor.execute("""
                SELECT ticker, business_description, sector, industry, last_updated
                FROM companies 
                WHERE business_description IS NOT NULL 
                AND TRIM(business_description) != ''
                AND UPPER(TRIM(business_description)) NOT IN ('NO DESCRIPTION', 'NONE', 'N/A', 'NA', '-')
                AND LENGTH(TRIM(business_description)) > 5
                AND (
                    -- No embedding exists for this model (skip already vectorized companies)
                    NOT EXISTS (
                        SELECT 1 FROM company_embeddings 
                        WHERE company_embeddings.ticker = companies.ticker 
                        AND company_embeddings.model_name = %s
                    )
                    -- OR company was updated after embedding was created (need to re-vectorize)
                    OR (
                        EXISTS (
                            SELECT 1 FROM company_embeddings 
                            WHERE company_embeddings.ticker = companies.ticker 
                            AND company_embeddings.model_name = %s
                        )
                        AND companies.last_updated > (
                            SELECT updated_at FROM company_embeddings 
                            WHERE company_embeddings.ticker = companies.ticker 
                            AND company_embeddings.model_name = %s
                            LIMIT 1
                        )
                    )
                )
                ORDER BY ticker
            """, (model_name, model_name, model_name))
            
            companies = cursor.fetchall()
            total = len(companies)
            
            cursor.close()
            
            if total == 0:
                logger.info(f"No companies need vectorization for model {model_name}")
                vectorization_status.update({
                    'running': False,
                    'message': f'No companies need vectorization for model {model_name}. All companies already have embeddings.',
                    'total': 0,
                    'processed': 0,
                    'success': 0,
                    'failed': 0,
                    'current_ticker': None,
                    'last_updated': datetime.now().isoformat(),
                    'tokens_used': 0,
                    'estimated_cost': 0.0,
                    'model_name': model_name
                })
                return {
                    'total': 0,
                    'success': 0,
                    'failed': 0,
                    'model_name': model_name,
                    'message': f'No companies need vectorization. All companies already have embeddings for model {model_name}.',
                    'no_companies': True
                }
            
            logger.info(f"Vectorizing {total} companies with {model_name} (including sector/industry) using 20 worker threads")
            
            # Use distributed vectorization for better performance
            if total > 10:
                # Start distributed vectorization (runs in background)
                start_distributed_vectorization(companies, model_name)
                return {
                    'total': total,
                    'message': f'Distributed vectorization started for {total} companies across 20 worker threads',
                    'model_name': model_name,
                    'distributed': True
                }
            else:
                # For small batches, use sequential processing
                logger.info("Using sequential processing for small batch")
                # Fall through to original sequential code (kept for small batches)
                # But we'll use distributed anyway for consistency
                start_distributed_vectorization(companies, model_name)
                return {
                    'total': total,
                    'message': f'Vectorization started for {total} companies',
                    'model_name': model_name,
                    'distributed': True
                }
            
    except Exception as e:
        error_msg = f"Error during vectorization: {str(e)}"
        logger.error(error_msg, exc_info=True)
        vectorization_status.update({
            'running': False,
            'message': error_msg,
            'last_updated': datetime.now().isoformat()
        })
        return {'error': error_msg}


def vectorize_companies_thread(model_name="text-embedding-3-small"):
    """Wrapper function to run vectorization in a background thread."""
    try:
        vectorize_companies_openai(model_name)
    except Exception as e:
        logger.error(f"Error in vectorization thread: {str(e)}", exc_info=True)
        vectorization_status.update({
            'running': False,
            'message': f'Error: {str(e)}',
            'last_updated': datetime.now().isoformat()
        })


@app.route('/api/companies/vectorize', methods=['POST'])
def vectorize_companies():
    """API endpoint to start vectorizing company descriptions."""
    global vectorization_status
    
    if vectorization_status['running']:
        return jsonify({
            'error': 'Vectorization already in progress',
            'status': vectorization_status
        }), 400
    
    model_name = request.json.get('model_name', 'text-embedding-3-small') if request.json else 'text-embedding-3-small'
    
    # Validate model name
    valid_models = ['text-embedding-3-small', 'text-embedding-3-large', 'text-embedding-ada-002']
    if model_name not in valid_models:
        return jsonify({
            'error': f'Invalid model name. Must be one of: {", ".join(valid_models)}'
        }), 400
    
    # Check if there are companies to vectorize BEFORE starting the thread
    # This gives immediate feedback to the user
    db_manager = get_db_manager()
    try:
        with db_manager:
            if db_manager.conn:
                cursor = db_manager.conn.cursor()
                
                # First check total with descriptions
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM companies 
                    WHERE business_description IS NOT NULL 
                    AND TRIM(business_description) != ''
                    AND UPPER(TRIM(business_description)) NOT IN ('NO DESCRIPTION', 'NONE', 'N/A', 'NA', '-')
                    AND LENGTH(TRIM(business_description)) > 5
                """)
                total_with_desc = cursor.fetchone()[0]
                logger.info(f"Pre-check: Total companies with descriptions: {total_with_desc}")
                
                # Check how many have embeddings
                cursor.execute("""
                    SELECT COUNT(DISTINCT ticker) 
                    FROM company_embeddings 
                    WHERE model_name = %s
                """, (model_name,))
                already_vectorized = cursor.fetchone()[0]
                logger.info(f"Pre-check: Already vectorized: {already_vectorized}")
                
                # Now check how many need vectorization
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM companies 
                    WHERE business_description IS NOT NULL 
                    AND TRIM(business_description) != ''
                    AND UPPER(TRIM(business_description)) NOT IN ('NO DESCRIPTION', 'NONE', 'N/A', 'NA', '-')
                    AND LENGTH(TRIM(business_description)) > 5
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM company_embeddings 
                            WHERE company_embeddings.ticker = companies.ticker 
                            AND company_embeddings.model_name = %s
                        )
                        OR (
                            EXISTS (
                                SELECT 1 FROM company_embeddings 
                                WHERE company_embeddings.ticker = companies.ticker 
                                AND company_embeddings.model_name = %s
                            )
                            AND companies.last_updated > (
                                SELECT updated_at FROM company_embeddings 
                                WHERE company_embeddings.ticker = companies.ticker 
                                AND company_embeddings.model_name = %s
                                LIMIT 1
                            )
                        )
                    )
                """, (model_name, model_name, model_name))
                count = cursor.fetchone()[0]
                logger.info(f"Pre-check: Companies needing vectorization: {count} (expected: {total_with_desc - already_vectorized})")
                cursor.close()
                
                if count == 0:
                    vectorization_status.update({
                        'running': False,
                        'message': f'No companies need vectorization. All {total_with_desc} companies with descriptions already have embeddings for model {model_name}.',
                        'total': 0,
                        'processed': 0,
                        'success': 0,
                        'failed': 0,
                        'current_ticker': None,
                        'last_updated': datetime.now().isoformat(),
                        'tokens_used': 0,
                        'estimated_cost': 0.0,
                        'model_name': model_name
                    })
                    return jsonify({
                        'message': f'No companies need vectorization. All {total_with_desc} companies with descriptions already have embeddings for model {model_name}.',
                        'status': vectorization_status,
                        'no_companies': True,
                        'total_with_descriptions': total_with_desc,
                        'already_vectorized': already_vectorized
                    })
    except Exception as e:
        logger.error(f"Error checking companies count before vectorization: {str(e)}", exc_info=True)
        # Continue anyway - the check will happen in the thread
    
    # Start vectorization in background thread
    thread = threading.Thread(
        target=vectorize_companies_thread,
        args=(model_name,),
        daemon=True
    )
    thread.start()
    
    return jsonify({
        'message': f'Vectorization started with model {model_name}',
        'status': vectorization_status
    })


@app.route('/api/companies/vectorization-status', methods=['GET'])
def get_vectorization_status():
    """API endpoint to get current vectorization status."""
    return jsonify(vectorization_status)


def cosine_similarity(vec1, vec2):
    """
    Calculate cosine similarity between two vectors.
    
    Args:
        vec1: First vector (list of floats)
        vec2: Second vector (list of floats)
    
    Returns:
        Cosine similarity value between -1 and 1
    """
    import numpy as np
    
    vec1 = np.array(vec1)
    vec2 = np.array(vec2)
    
    dot_product = np.dot(vec1, vec2)
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return float(dot_product / (norm1 * norm2))


@app.route('/api/companies/correlation-matrix', methods=['GET'])
def get_correlation_matrix():
    """
    API endpoint to get correlation matrix (cosine similarity) for vectorized companies.
    
    Returns a matrix where each cell [i][j] represents the cosine similarity
    between company i and company j embeddings.
    
    The matrix is cached in the database and recalculated only when embeddings change.
    Use ?force_recalculate=true to force recalculation.
    """
    logger.info("API /api/companies/correlation-matrix called")
    
    try:
        import numpy as np
        import json
        
        model_name = request.args.get('model_name', 'text-embedding-3-small').strip()
        force_recalculate = request.args.get('force_recalculate', 'false').lower() == 'true'
        limit = int(request.args.get('limit', 500))  # Limit to avoid memory issues
        
        db_manager = get_db_manager()
        with db_manager:
            if not db_manager.conn:
                raise ConnectionError("Database connection not established")
            
            cursor = db_manager.conn.cursor()
            
            # Get all vectorized companies with their embeddings
            # Exclude companies with placeholder descriptions
            cursor.execute("""
                SELECT 
                    c.ticker,
                    c.name,
                    c.sector,
                    ce.embedding_vector
                FROM companies c
                INNER JOIN company_embeddings ce ON c.ticker = ce.ticker
                WHERE ce.model_name = %s
                AND ce.embedding_vector IS NOT NULL
                AND (
                    c.business_description IS NULL
                    OR (
                        c.business_description IS NOT NULL
                        AND TRIM(c.business_description) != ''
                        AND UPPER(TRIM(c.business_description)) NOT IN ('NO DESCRIPTION', 'NONE', 'N/A', 'NA', '-')
                        AND LENGTH(TRIM(c.business_description)) > 5
                    )
                )
                ORDER BY c.ticker
                LIMIT %s
            """, (model_name, limit))
            
            companies_data = cursor.fetchall()
            
            # Get available model names for the selector
            cursor.execute("""
                SELECT DISTINCT model_name 
                FROM company_embeddings 
                ORDER BY model_name
            """)
            available_models = [row[0] for row in cursor.fetchall()]
            
            if len(companies_data) == 0:
                cursor.close()
                return jsonify({
                    'error': f'No vectorized companies found for model {model_name}',
                    'available_models': available_models
                }), 404
            
            # Extract tickers, names, and embeddings
            tickers = [row[0] for row in companies_data]
            names = [row[1] for row in companies_data]
            sectors = [row[2] for row in companies_data]
            embeddings = [row[3] for row in companies_data]
            n = len(embeddings)
            
            # Check if cached matrix exists and is up-to-date
            if not force_recalculate:
                cursor.execute("""
                    SELECT 
                        matrix_data,
                        tickers,
                        calculated_at,
                        companies_count
                    FROM company_correlation_matrix
                    WHERE model_name = %s
                """, (model_name,))
                
                cached_result = cursor.fetchone()
                
                if cached_result:
                    cached_matrix_data, cached_tickers, calculated_at, cached_count = cached_result
                    
                    # Check if cached matrix matches current companies
                    if (cached_count == n and 
                        list(cached_tickers) == tickers and
                        cached_matrix_data is not None):
                        
                        # Check if any embeddings were updated after matrix calculation
                        cursor.execute("""
                            SELECT COUNT(*) 
                            FROM company_embeddings 
                            WHERE model_name = %s 
                            AND updated_at > %s
                        """, (model_name, calculated_at))
                        
                        updated_count = cursor.fetchone()[0]
                        
                        if updated_count == 0:
                            # Cache is valid, return it
                            logger.info(f"Returning cached correlation matrix for {model_name} (calculated at {calculated_at})")
                            matrix = json.loads(cached_matrix_data) if isinstance(cached_matrix_data, str) else cached_matrix_data
                            
                            cursor.close()
                            
                            return jsonify({
                                'tickers': tickers,
                                'names': names,
                                'sectors': sectors,
                                'matrix': matrix,
                                'model_name': model_name,
                                'size': n,
                                'available_models': available_models,
                                'cached': True,
                                'calculated_at': calculated_at.isoformat() if calculated_at else None
                            })
                        else:
                            logger.info(f"Cache invalid: {updated_count} embeddings updated since matrix calculation")
                    else:
                        logger.info(f"Cache invalid: company count or tickers changed (cached: {cached_count}, current: {n})")
            
            # Need to calculate matrix
            logger.info(f"Calculating correlation matrix for {n} companies using 20 worker threads")
            
            # Calculate cosine similarity matrix using parallel threads
            similarity_matrix = [None] * n  # Pre-allocate matrix
            
            def calculate_row_chunk(worker_id, row_indices, embeddings_list, result_dict):
                """Calculate similarity for a chunk of rows."""
                logger.info(f"Correlation worker {worker_id}: processing {len(row_indices)} rows")
                chunk_results = {}
                
                for i in row_indices:
                    row = []
                    for j in range(n):
                        if i == j:
                            # Same company = perfect similarity
                            similarity = 1.0
                        else:
                            similarity = cosine_similarity(embeddings_list[i], embeddings_list[j])
                        row.append(round(similarity, 4))
                    chunk_results[i] = row
                
                result_dict[worker_id] = chunk_results
                logger.info(f"Correlation worker {worker_id}: completed {len(row_indices)} rows")
            
            # Split rows across 20 worker threads
            num_workers = 40
            rows_per_worker = n // num_workers
            remainder = n % num_workers
            
            worker_tasks = []
            start_idx = 0
            
            for worker_num in range(1, num_workers + 1):
                # Distribute remainder across first workers
                size = rows_per_worker + (1 if worker_num <= remainder else 0)
                row_indices = list(range(start_idx, start_idx + size))
                start_idx += size
                
                if row_indices:
                    worker_tasks.append((worker_num, row_indices))
            
            logger.info(f"Distributing {n} rows across {len(worker_tasks)} worker threads for correlation calculation")
            
            # Shared results dictionary
            results_dict = {}
            
            # Start worker threads
            threads = []
            for worker_num, row_indices in worker_tasks:
                thread = threading.Thread(
                    target=calculate_row_chunk,
                    args=(worker_num, row_indices, embeddings, results_dict),
                    daemon=True
                )
                thread.start()
                threads.append((worker_num, thread))
            
            # Wait for all threads to complete
            for worker_num, thread in threads:
                thread.join()
            
            # Aggregate results into similarity_matrix
            for worker_num, _ in threads:
                if worker_num in results_dict:
                    chunk_results = results_dict[worker_num]
                    for row_idx, row_data in chunk_results.items():
                        similarity_matrix[row_idx] = row_data
            
            # Save matrix to database cache
            try:
                matrix_json = json.dumps(similarity_matrix)
                cursor.execute("""
                    INSERT INTO company_correlation_matrix 
                        (model_name, tickers, matrix_data, companies_count, calculated_at)
                    VALUES (%s, %s, %s::jsonb, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (model_name) 
                    DO UPDATE SET
                        tickers = EXCLUDED.tickers,
                        matrix_data = EXCLUDED.matrix_data,
                        companies_count = EXCLUDED.companies_count,
                        calculated_at = CURRENT_TIMESTAMP
                """, (model_name, tickers, matrix_json, n))
                db_manager.conn.commit()
                logger.info(f"Correlation matrix saved to database cache for {model_name}")
            except Exception as e:
                logger.warning(f"Could not save correlation matrix to cache: {str(e)}")
                db_manager.conn.rollback()
            
            cursor.close()
            
            logger.info(f"Correlation matrix calculated: {n}x{n} using {len(worker_tasks)} worker threads")
            
            return jsonify({
                'tickers': tickers,
                'names': names,
                'sectors': sectors,
                'matrix': similarity_matrix,
                'model_name': model_name,
                'size': n,
                'available_models': available_models,
                'cached': False,
                'calculated_at': datetime.now().isoformat()
            })
            
    except Exception as e:
        logger.error(f"Error calculating correlation matrix: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


def get_correlation_matrix_from_db(model_name):
    """
    Retrieve correlation matrix from database.
    
    Returns:
        tuple: (tickers_list, correlation_matrix, ticker_to_index_dict) or None if not found
    """
    db_manager = get_db_manager()
    try:
        with db_manager:
            if not db_manager.conn:
                logger.error("Database connection not established")
                return None
            
            cursor = db_manager.conn.cursor()
            cursor.execute("""
                SELECT tickers, matrix_data
                FROM company_correlation_matrix
                WHERE model_name = %s
            """, (model_name,))
            
            result = cursor.fetchone()
            cursor.close()
            
            if result:
                import json
                tickers = list(result[0])  # Convert array to list
                matrix = json.loads(result[1]) if isinstance(result[1], str) else result[1]
                
                # Create ticker to index mapping
                ticker_to_index = {ticker: idx for idx, ticker in enumerate(tickers)}
                
                logger.info(f"Retrieved correlation matrix for {model_name}: {len(tickers)} tickers")
                return tickers, matrix, ticker_to_index
            else:
                logger.warning(f"No correlation matrix found for model {model_name}")
                return None
                
    except Exception as e:
        logger.error(f"Error retrieving correlation matrix: {str(e)}", exc_info=True)
        return None


@app.route('/api/companies/vectorized', methods=['GET'])


@app.route('/api/companies/vectorized', methods=['GET'])
def get_vectorized_companies():
    """API endpoint to fetch vectorized companies with filters."""
    logger.debug("API /api/companies/vectorized called")
    
    try:
        db_manager = get_db_manager()
        with db_manager:
            if not db_manager.conn:
                raise ConnectionError("Database connection not established")
            
            cursor = db_manager.conn.cursor()
            
            # Get query parameters
            search = request.args.get('search', '').strip()
            sector = request.args.get('sector', '').strip()
            model_name = request.args.get('model_name', 'text-embedding-3-small').strip()
            limit = int(request.args.get('limit', 100))
            offset = int(request.args.get('offset', 0))
            
            logger.debug(f"Vectorized companies query params: search='{search}', sector='{sector}', model='{model_name}', limit={limit}, offset={offset}")
            
            # Build query - join companies with company_embeddings
            # Exclude companies with placeholder descriptions
            where_clauses = [
                "ce.embedding_vector IS NOT NULL",
                "(c.business_description IS NULL OR (c.business_description IS NOT NULL AND TRIM(c.business_description) != '' AND UPPER(TRIM(c.business_description)) NOT IN ('NO DESCRIPTION', 'NONE', 'N/A', 'NA', '-') AND LENGTH(TRIM(c.business_description)) > 5))"
            ]
            params = []
            
            # Filter by model_name
            where_clauses.append("ce.model_name = %s")
            params.append(model_name)
            
            if search:
                where_clauses.append("(c.ticker ILIKE %s OR c.name ILIKE %s)")
                search_pattern = f"%{search}%"
                params.extend([search_pattern, search_pattern])
            
            if sector:
                where_clauses.append("c.sector = %s")
                params.append(sector)
            
            where_clause = " AND ".join(where_clauses)
            
            # Get total count
            count_query = f"""
                SELECT COUNT(DISTINCT c.ticker)
                FROM companies c
                INNER JOIN company_embeddings ce ON c.ticker = ce.ticker
                WHERE {where_clause}
            """
            cursor.execute(count_query, params)
            total_count = cursor.fetchone()[0]
            
            # Get vectorized companies
            query = f"""
                SELECT 
                    c.ticker,
                    c.name,
                    c.business_description,
                    c.sector,
                    c.industry,
                    ce.model_name,
                    ce.dimension,
                    ce.updated_at as embedding_updated_at,
                    c.last_updated
                FROM companies c
                INNER JOIN company_embeddings ce ON c.ticker = ce.ticker
                WHERE {where_clause}
                ORDER BY c.ticker
                LIMIT %s OFFSET %s
            """
            params.extend([limit, offset])
            
            cursor.execute(query, params)
            companies = []
            
            for row in cursor.fetchall():
                companies.append({
                    'ticker': row[0],
                    'name': row[1],
                    'business_description': row[2],
                    'sector': row[3],
                    'industry': row[4],
                    'model_name': row[5],
                    'dimension': row[6],
                    'embedding_updated_at': row[7].isoformat() if row[7] else None,
                    'last_updated': row[8].isoformat() if row[8] else None
                })
            
            # Get unique sectors for filter
            cursor.execute("""
                SELECT DISTINCT c.sector 
                FROM companies c
                INNER JOIN company_embeddings ce ON c.ticker = ce.ticker
                WHERE c.sector IS NOT NULL AND ce.model_name = %s
                ORDER BY c.sector
            """, (model_name,))
            sectors = [row[0] for row in cursor.fetchall()]
            
            # Get unique model names
            cursor.execute("""
                SELECT DISTINCT model_name 
                FROM company_embeddings 
                ORDER BY model_name
            """)
            model_names = [row[0] for row in cursor.fetchall()]
            
            # Get statistics
            cursor.execute("""
                SELECT COUNT(DISTINCT ce.ticker)
                FROM company_embeddings ce
                WHERE ce.model_name = %s
            """, (model_name,))
            vectorized_count = cursor.fetchone()[0]
            
            cursor.close()
            
            return jsonify({
                'companies': companies,
                'total': total_count,
                'vectorized_count': vectorized_count,
                'sectors': sectors,
                'model_names': model_names,
                'current_model': model_name
            })
            
    except Exception as e:
        logger.error(f"Error fetching vectorized companies: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Personal holdings tracker (per-user): P&L vs buy price + sell-decision signal
# ---------------------------------------------------------------------------

def _holding_market_info(ticker):
    """Current price + technical/sentiment snapshot for one ticker, all from the
    cached daily series (no extra Alpha Vantage calls beyond the lazy price cache).
    """
    out = {'current_price': None, 'rsi': None, 'dist_sma50_pct': None,
           'bb_position': None, 'sentiment_kpi': None}
    try:
        data = _fetch_av_daily(ticker, points=120)
        if not data.get('error'):
            closes = [c for c in data['closes'] if c is not None]
            if closes:
                last = closes[-1]
                out['current_price'] = last
                sma50 = _sma(closes, 50)
                rsi14 = _rsi(closes, 14)
                _, bb_upper, bb_lower = _bollinger(closes, 20, 2.0)
                if sma50[-1]:
                    out['dist_sma50_pct'] = round((last / sma50[-1] - 1.0) * 100.0, 2)
                if rsi14[-1] is not None:
                    out['rsi'] = round(rsi14[-1], 1)
                if bb_upper[-1] is not None and bb_lower[-1] is not None:
                    width = bb_upper[-1] - bb_lower[-1]
                    if width > 0:
                        out['bb_position'] = round((last - bb_lower[-1]) / width, 2)
    except Exception as e:
        logger.warning(f"Holding market info failed for {ticker}: {e}")
    try:
        series = _compute_ticker_kpi_series(ticker)
        if series:
            out['sentiment_kpi'] = series[-1].get('kpi')
    except Exception as e:
        logger.warning(f"Holding sentiment KPI failed for {ticker}: {e}")
    return out


def _decision_signal(pnl_pct, sentiment_kpi, rsi, dist_sma50_pct, bb_position):
    """Transparent, rule-based sell-decision helper. NOT financial advice: it
    just summarizes P&L + sentiment + technicals into a label with the reasons
    that fired, so the user can decide. Each bearish condition adds sell
    pressure, each bullish one adds hold pressure.
    """
    reasons = []
    sell = 0
    hold = 0
    if sentiment_kpi is not None:
        if sentiment_kpi <= -0.15:
            sell += 1; reasons.append(f"Sentiment news negativo ({sentiment_kpi:+.2f})")
        elif sentiment_kpi >= 0.15:
            hold += 1; reasons.append(f"Sentiment news positivo ({sentiment_kpi:+.2f})")
    if rsi is not None:
        if rsi >= 70:
            sell += 1; reasons.append(f"RSI ipercomprato ({rsi:.0f})")
        elif rsi <= 30:
            hold += 1; reasons.append(f"RSI ipervenduto ({rsi:.0f}), possibile rimbalzo")
    if dist_sma50_pct is not None:
        if dist_sma50_pct <= -5:
            sell += 1; reasons.append(f"Sotto la media 50gg ({dist_sma50_pct:+.1f}%)")
        elif dist_sma50_pct >= 5:
            hold += 1; reasons.append(f"Sopra la media 50gg ({dist_sma50_pct:+.1f}%)")
    if bb_position is not None:
        if bb_position >= 0.95:
            sell += 1; reasons.append("Prezzo al limite superiore delle Bollinger")
        elif bb_position <= 0.05:
            hold += 1; reasons.append("Prezzo al limite inferiore delle Bollinger")
    if pnl_pct is not None:
        if pnl_pct >= 25 and sell >= 1:
            sell += 1; reasons.append(f"Guadagno ampio (+{pnl_pct:.0f}%) con segnali di debolezza: valuta presa di profitto")
        elif pnl_pct <= -15 and sell >= 1:
            sell += 1; reasons.append(f"Perdita rilevante ({pnl_pct:.0f}%) con segnali negativi: valuta stop")
    net = sell - hold
    if net >= 2:
        label = 'VALUTA USCITA'
    elif net <= -1:
        label = 'MANTIENI'
    else:
        label = 'DA RIVEDERE'
    if not reasons:
        reasons.append("Dati insufficienti per un segnale forte")
    return {'label': label, 'reasons': reasons, 'sell_pressure': sell, 'hold_pressure': hold}


@app.route('/holdings')
def holdings_page():
    """Personal holdings tracker page (per logged-in user)."""
    return render_template('holdings.html')


@app.route('/api/holdings', methods=['GET'])
def api_holdings_list():
    """List the current user's holdings, each enriched with live P&L, sentiment,
    technicals and the sell-decision signal."""
    uid = current_user_id()
    if not uid:
        return jsonify({'error': 'login required'}), 401
    _ensure_user_tables()
    db = get_db_manager()
    rows = []
    with db:
        cur = db.conn.cursor()
        try:
            cur.execute(
                "SELECT id, ticker, quantity, buy_price, buy_currency, buy_date, notes "
                "FROM user_holdings WHERE user_id = %s ORDER BY ticker", (uid,)
            )
            rows = cur.fetchall()
        finally:
            cur.close()
    holdings = []
    totals = {'cost': 0.0, 'market': 0.0}
    for r in rows:
        hid, ticker, qty, buy_price, ccy, buy_date, notes = r
        qty = float(qty)
        buy_price = float(buy_price)
        info = _holding_market_info(ticker)
        cp = info['current_price']
        cost = qty * buy_price
        mkt = qty * cp if cp is not None else None
        pnl_abs = (mkt - cost) if mkt is not None else None
        pnl_pct = ((cp / buy_price - 1.0) * 100.0) if (cp is not None and buy_price) else None
        signal = _decision_signal(pnl_pct, info['sentiment_kpi'], info['rsi'],
                                  info['dist_sma50_pct'], info['bb_position'])
        totals['cost'] += cost
        if mkt is not None:
            totals['market'] += mkt
        holdings.append({
            'id': hid, 'ticker': ticker, 'quantity': qty,
            'buy_price': buy_price, 'buy_currency': ccy,
            'buy_date': buy_date.isoformat() if buy_date else None,
            'notes': notes,
            'current_price': round(cp, 4) if cp is not None else None,
            'cost_basis': round(cost, 2),
            'market_value': round(mkt, 2) if mkt is not None else None,
            'pnl_abs': round(pnl_abs, 2) if pnl_abs is not None else None,
            'pnl_pct': round(pnl_pct, 2) if pnl_pct is not None else None,
            'sentiment_kpi': info['sentiment_kpi'],
            'rsi': info['rsi'],
            'dist_sma50_pct': info['dist_sma50_pct'],
            'signal': signal,
        })
    tot_pnl = totals['market'] - totals['cost'] if totals['market'] else None
    return jsonify({
        'holdings': holdings,
        'totals': {
            'cost_basis': round(totals['cost'], 2),
            'market_value': round(totals['market'], 2) if totals['market'] else None,
            'pnl_abs': round(tot_pnl, 2) if tot_pnl is not None else None,
            'pnl_pct': round((tot_pnl / totals['cost']) * 100.0, 2) if (tot_pnl is not None and totals['cost']) else None,
        },
    })


@app.route('/api/holdings', methods=['POST'])
def api_holdings_create():
    """Add a holding for the current user."""
    uid = current_user_id()
    if not uid:
        return jsonify({'error': 'login required'}), 401
    d = request.get_json(silent=True) or request.form
    ticker = (d.get('ticker') or '').strip().upper()
    try:
        qty = float(d.get('quantity'))
        buy_price = float(d.get('buy_price'))
    except (TypeError, ValueError):
        return jsonify({'error': 'quantity e buy_price devono essere numerici'}), 400
    if not ticker or qty <= 0 or buy_price < 0:
        return jsonify({'error': 'dati non validi'}), 400
    buy_currency = (d.get('buy_currency') or 'USD').strip().upper()[:8]
    buy_date = (d.get('buy_date') or '').strip() or None
    notes = (d.get('notes') or '').strip() or None
    _ensure_user_tables()
    db = get_db_manager()
    new_id = None
    with db:
        cur = db.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO user_holdings (user_id, ticker, quantity, buy_price, "
                "buy_currency, buy_date, notes) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (uid, ticker, qty, buy_price, buy_currency, buy_date, notes)
            )
            new_id = cur.fetchone()[0]
            db.conn.commit()
        finally:
            cur.close()
    return jsonify({'ok': True, 'id': new_id})


@app.route('/api/holdings/<int:hid>', methods=['PATCH'])
def api_holdings_update(hid):
    """Update fields of a holding owned by the current user."""
    uid = current_user_id()
    if not uid:
        return jsonify({'error': 'login required'}), 401
    d = request.get_json(silent=True) or {}
    sets, vals = [], []
    for k in ('quantity', 'buy_price', 'buy_currency', 'buy_date', 'notes'):
        if k in d:
            sets.append(f"{k} = %s")
            vals.append(d[k] if d[k] != '' else None)
    if not sets:
        return jsonify({'error': 'niente da aggiornare'}), 400
    sets.append("updated_at = CURRENT_TIMESTAMP")
    vals += [hid, uid]
    db = get_db_manager()
    updated = 0
    with db:
        cur = db.conn.cursor()
        try:
            cur.execute(
                f"UPDATE user_holdings SET {', '.join(sets)} WHERE id = %s AND user_id = %s",
                vals
            )
            updated = cur.rowcount
            db.conn.commit()
        finally:
            cur.close()
    return jsonify({'ok': updated > 0})


@app.route('/api/holdings/<int:hid>', methods=['DELETE'])
def api_holdings_delete(hid):
    """Delete a holding owned by the current user."""
    uid = current_user_id()
    if not uid:
        return jsonify({'error': 'login required'}), 401
    db = get_db_manager()
    deleted = 0
    with db:
        cur = db.conn.cursor()
        try:
            cur.execute("DELETE FROM user_holdings WHERE id = %s AND user_id = %s", (hid, uid))
            deleted = cur.rowcount
            db.conn.commit()
        finally:
            cur.close()
    return jsonify({'ok': deleted > 0})


if __name__ == '__main__':
    # Ensure all tables exist BEFORE creating views that depend on them
    # Ensure companies table exists
    ensure_companies_table_exists()
    # Ensure company embeddings table exists
    ensure_company_embeddings_table_exists()
    # Ensure correlation matrix table exists
    ensure_correlation_matrix_table_exists()
    # Note: ticker_cross_diffused_sentiment and ticker_decayed_sentiment tables
    # are not created automatically as they are not currently used in the application
    
    port = int(os.getenv('FLASK_PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
