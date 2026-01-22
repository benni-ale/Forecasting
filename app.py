#!/usr/bin/env python3
"""
Flask Web Dashboard for Financial News Collector
Provides a GUI to collect news and visualize collected articles.
"""

import os
import json
import logging
import requests
import subprocess
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from news_collector import AlphaVantageNewsCollector, DatabaseManager
import threading
import time
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
    'topics_total': 0
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


def get_db_manager():
    """Get database manager with environment variables."""
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


@app.route('/')
def index():
    """Main dashboard page."""
    logger.info("Dashboard page accessed")
    # Get statistics
    stats = get_statistics()
    logger.debug(f"Statistics retrieved: {stats.get('total_articles', 0)} articles")
    return render_template('index.html', stats=stats)


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
    
    logger.info(f"Deep ingestion requested: {duration_minutes} minutes duration")
    
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
        
        logger.info(f"Starting deep ingestion: {duration_minutes} minutes duration, {len(all_topics)} topics")
        
        try:
            rate_limit = int(os.getenv('ALPHA_VANTAGE_RATE_LIMIT', '75'))
            collector = AlphaVantageNewsCollector(api_key, rate_limit_per_minute=rate_limit)
            db_manager = get_db_manager()
            
            # Calculate end time
            start_time = datetime.now()
            end_time = start_time + timedelta(minutes=duration_minutes)
            
            logger.info(f"Deep ingestion: Running for {duration_minutes} minutes (until {end_time.strftime('%Y-%m-%d %H:%M:%S')})")
            deep_ingestion_status['message'] = f'Running for {duration_minutes} minutes, collecting from all topics...'
            
            total_inserted = 0
            total_skipped = 0
            seen_urls = set()  # Global deduplication across all topics
            
            # Start from NOW and go backwards in time
            current_end = datetime.now()
            # Go back up to 1 year initially, will continue going back as time allows
            initial_start = current_end - timedelta(days=365)
            current_start = initial_start
            
            topic_index = 0
            chunk_num = 0
            round_num = 0  # Track how many complete rounds through all topics
            
            # Keep running until time expires
            while datetime.now() < end_time and deep_ingestion_status['running']:
                # Cycle through topics - priority to new topics over older news
                topic = all_topics[topic_index % len(all_topics)]
                
                # Check if we've completed a full round through all topics
                if topic_index > 0 and (topic_index % len(all_topics)) == 0:
                    round_num += 1
                    # After completing a round, go backwards in time for next round
                    current_end = current_end - timedelta(days=30)
                    # If we've gone too far back, reset to now and start fresh
                    if current_end < current_start:
                        current_end = datetime.now()
                        current_start = current_end - timedelta(days=365)
                        round_num = 0
                    logger.info(f"Deep ingestion: Completed round {round_num}, moving back in time. Next range: {current_end.strftime('%Y%m%dT%H%M')}")
                
                topic_index += 1
                
                deep_ingestion_status['current_topic'] = topic
                deep_ingestion_status['topics_completed'] = round_num
                # Calculate remaining time
                remaining_seconds = (end_time - datetime.now()).total_seconds()
                remaining_minutes = int(remaining_seconds / 60)
                remaining_secs = int(remaining_seconds % 60)
                deep_ingestion_status['message'] = f'Topic: {topic} (Round {round_num + 1}) | Time remaining: {remaining_minutes}m {remaining_secs}s'
                
                chunk_num += 1
                chunk_days = 30
                
                # Go backwards: chunk_end is more recent, chunk_start is older
                chunk_start = max(current_end - timedelta(days=chunk_days), current_start)
                chunk_from = chunk_start.strftime('%Y%m%dT%H%M')
                chunk_to = current_end.strftime('%Y%m%dT%H%M')
                
                logger.info(f"Deep ingestion: Topic {topic}, round {round_num + 1}, chunk {chunk_num}: {chunk_from} to {chunk_to}")
                
                try:
                    # Fetch articles for this chunk
                    chunk_data = collector._single_request(
                        tickers=None,
                        topics=topic,
                        time_from=chunk_from,
                        time_to=chunk_to,
                        limit=50,
                        sort="LATEST"
                    )
                    
                    chunk_articles = chunk_data.get('feed', [])
                    
                    # Deduplicate globally
                    new_articles = []
                    for article in chunk_articles:
                        url = article.get('url')
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            new_articles.append(article)
                    
                    logger.info(f"Deep ingestion: Topic {topic}, round {round_num + 1}, chunk {chunk_num}: {len(new_articles)} new articles")
                    
                    # Save to DB after each chunk (not waiting for all chunks)
                    if new_articles:
                        with db_manager:
                            result = db_manager.save_articles(new_articles)
                            total_inserted += result['inserted']
                            total_skipped += result['skipped']
                            deep_ingestion_status['total_inserted'] = total_inserted
                            deep_ingestion_status['total_skipped'] = total_skipped
                            deep_ingestion_status['total_articles'] = total_inserted + total_skipped
                            logger.info(f"Deep ingestion: Saved chunk - {result['inserted']} inserted, {result['skipped']} skipped")
                    
                except Exception as e:
                    logger.error(f"Error in deep ingestion chunk for topic {topic}: {str(e)}", exc_info=True)
                    # Continue with next request
                
                # Rate limit delay (only if we have time left)
                if datetime.now() < end_time:
                    time.sleep(collector.request_delay)
            
            # Final status
            deep_ingestion_status['message'] = (
                f'✓ Deep ingestion complete! '
                f'Processed {len(all_topics)} topics, '
                f'{total_inserted} new articles inserted, '
                f'{total_skipped} duplicates skipped'
            )
            logger.info(f"Deep ingestion completed: {total_inserted} inserted, {total_skipped} skipped")
            
        except Exception as e:
            logger.error(f"Error in deep ingestion thread: {str(e)}", exc_info=True)
            deep_ingestion_status['message'] = f'Error: {str(e)}'
        finally:
            deep_ingestion_status['running'] = False
            deep_ingestion_status['current_topic'] = None
            logger.info("Deep ingestion thread finished")
    
    thread = threading.Thread(target=deep_ingestion_thread)
    thread.daemon = True
    thread.start()
    
    flash(f'Deep ingestion started: running for {duration_minutes} minutes, collecting from all topics.', 'info')
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
            
            # Query to calculate weighted average sentiment
            # sum(relevance*sentiment)/sum(relevance)
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
                        (ticker_info->>'relevance_score')::numeric as relevance_score
                    FROM ticker_data
                    WHERE ticker_info->>'ticker' = %s
                        AND ticker_info->>'ticker_sentiment_score' IS NOT NULL
                        AND ticker_info->>'relevance_score' IS NOT NULL
                        AND (ticker_info->>'ticker_sentiment_score')::numeric IS NOT NULL
                        AND (ticker_info->>'relevance_score')::numeric IS NOT NULL
                ),
                weighted_sentiment AS (
                    SELECT 
                        time_bucket,
                        SUM(sentiment_score * relevance_score) / NULLIF(SUM(relevance_score), 0) as weighted_avg_sentiment,
                        SUM(relevance_score) as total_relevance
                    FROM ticker_scores
                    GROUP BY time_bucket
                    HAVING SUM(relevance_score) > 0
                )
                SELECT 
                    TO_CHAR(time_bucket, %s) as time_bucket_str,
                    weighted_avg_sentiment,
                    total_relevance
                FROM weighted_sentiment
                ORDER BY time_bucket ASC
            """
            
            cursor.execute(query, (ticker.upper(), time_format))
            results = cursor.fetchall()
            cursor.close()
            
            if not results:
                return jsonify({
                    'dates': [],
                    'sentiments': [],
                    'granularity': granularity,
                    'interval': interval if granularity == 'intraday' else None
                })
            
            dates = [row[0] for row in results]
            sentiments = [float(row[1]) if row[1] is not None else 0.0 for row in results]
            
            logger.info(f"Returning {len(dates)} sentiment data points for {ticker} with {granularity} granularity")
            
            return jsonify({
                'dates': dates,
                'sentiments': sentiments,
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
        db_manager = get_db_manager()
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
    
    logger.info(f"Articles page accessed: page={page}, per_page={per_page}, "
                f"sentiment={sentiment_filter}, ticker={ticker_filter}, search={search_query}")
    
    try:
        db_manager = get_db_manager()
        with db_manager:
            articles, total, stats = get_articles_paginated(
                db_manager, page, per_page, sentiment_filter, ticker_filter, search_query
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
                             stats=stats)
    except Exception as e:
        logger.error(f"Error loading articles: {str(e)}", exc_info=True)
        flash(f'Error loading articles: {str(e)}', 'error')
        return redirect(url_for('index'))


def get_articles_paginated(db_manager, page, per_page, sentiment_filter='', ticker_filter='', search_query=''):
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
    
    try:
        db_manager = get_db_manager()
        with db_manager:
            articles, total, _ = get_articles_paginated(
                db_manager, page, per_page, sentiment_filter, ticker_filter, ''
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
        db_manager = get_db_manager()
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

