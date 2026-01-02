#!/usr/bin/env python3
"""
Flask Web Dashboard for Financial News Collector
Provides a GUI to collect news and visualize collected articles.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from news_collector import AlphaVantageNewsCollector, DatabaseManager
import threading
import time
from dotenv import load_dotenv

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
    
    # All available topics
    all_topics = [
        'blockchain', 'earnings', 'ipo', 'mergers_and_acquisitions',
        'financial_markets', 'economy_fiscal', 'economy_monetary', 'economy_macro',
        'energy_transportation', 'finance', 'life_sciences', 'manufacturing',
        'real_estate', 'retail_wholesale', 'technology'
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
            
            # Keep running until time expires
            while datetime.now() < end_time and deep_ingestion_status['running']:
                # Cycle through topics
                topic = all_topics[topic_index % len(all_topics)]
                topic_index += 1
                
                deep_ingestion_status['current_topic'] = topic
                deep_ingestion_status['topics_completed'] = (topic_index - 1) // len(all_topics)
                # Calculate remaining time
                remaining_seconds = (end_time - datetime.now()).total_seconds()
                remaining_minutes = int(remaining_seconds / 60)
                remaining_secs = int(remaining_seconds % 60)
                deep_ingestion_status['message'] = f'Topic: {topic} | Time remaining: {remaining_minutes}m {remaining_secs}s'
                
                chunk_num += 1
                chunk_days = 30
                
                # Go backwards: chunk_end is more recent, chunk_start is older
                chunk_start = max(current_end - timedelta(days=chunk_days), current_start)
                chunk_from = chunk_start.strftime('%Y%m%dT%H%M')
                chunk_to = current_end.strftime('%Y%m%dT%H%M')
                
                logger.info(f"Deep ingestion: Topic {topic}, chunk {chunk_num}: {chunk_from} to {chunk_to}")
                
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
                    
                    logger.info(f"Deep ingestion: Topic {topic}, chunk {chunk_num}: {len(new_articles)} new articles")
                    
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
                    
                    # Move backwards in time for next chunk
                    current_end = chunk_start - timedelta(seconds=1)
                    
                    # If we've gone too far back, reset to now and continue
                    if current_end < current_start:
                        current_end = datetime.now()
                        current_start = current_end - timedelta(days=365)
                    
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
    """Get stock price history (last year) from Alpha Vantage API."""
    api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'API key not configured'}), 500
    
    try:
        import requests
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "TIME_SERIES_MONTHLY",
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
        
        if "Monthly Time Series" in data:
            time_series = data["Monthly Time Series"]
            # Get last 12 months
            dates = sorted(time_series.keys(), reverse=True)[:12]
            dates.reverse()  # Oldest first for chart
            
            history = {
                'dates': dates,
                'closes': [float(time_series[date]['4. close']) for date in dates]
            }
            return jsonify(history)
        else:
            return jsonify({'error': 'No historical data available'}), 404
            
    except Exception as e:
        logger.error(f"Error fetching stock history for {ticker}: {str(e)}", exc_info=True)
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


if __name__ == '__main__':
    port = int(os.getenv('FLASK_PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)

