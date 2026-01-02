#!/usr/bin/env python3
"""
Flask Web Dashboard for Financial News Collector
Provides a GUI to collect news and visualize collected articles.
"""

import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from news_collector import AlphaVantageNewsCollector, DatabaseManager
import threading
import time

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')

# Global state for collection status
collection_status = {
    'running': False,
    'message': '',
    'last_run': None,
    'articles_collected': 0
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
    # Get statistics
    stats = get_statistics()
    return render_template('index.html', stats=stats)


@app.route('/collect', methods=['POST'])
def collect_news():
    """Collect news based on form parameters."""
    global collection_status
    
    if collection_status['running']:
        flash('Collection already in progress. Please wait.', 'warning')
        return redirect(url_for('index'))
    
    # Get form parameters
    api_key = request.form.get('api_key') or os.getenv('ALPHA_VANTAGE_API_KEY')
    if not api_key:
        flash('API key is required. Please provide it in the form or set ALPHA_VANTAGE_API_KEY environment variable.', 'error')
        return redirect(url_for('index'))
    
    tickers = request.form.get('tickers', '').strip()
    topics = request.form.get('topics', '').strip()
    search_query = request.form.get('search_query', '').strip()
    time_from = request.form.get('time_from', '').strip()
    time_to = request.form.get('time_to', '').strip()
    
    # Convert datetime-local format to YYYYMMDDTHHMM if needed
    if time_from and 'T' in time_from and len(time_from) > 10:
        # Format: YYYY-MM-DDTHH:MM -> YYYYMMDDTHHMM
        try:
            dt = datetime.fromisoformat(time_from.replace('Z', '+00:00'))
            time_from = dt.strftime('%Y%m%dT%H%M')
        except:
            pass  # Keep original if conversion fails
    
    if time_to and 'T' in time_to and len(time_to) > 10:
        try:
            dt = datetime.fromisoformat(time_to.replace('Z', '+00:00'))
            time_to = dt.strftime('%Y%m%dT%H%M')
        except:
            pass
    
    limit = int(request.form.get('limit', 50))
    sort = request.form.get('sort', 'LATEST')
    save_to_db = request.form.get('save_to_db') == 'on'
    
    # Start collection in background thread
    def collect_thread():
        global collection_status
        collection_status['running'] = True
        collection_status['message'] = 'Starting collection...'
        
        try:
            collector = AlphaVantageNewsCollector(api_key)
            
            collection_status['message'] = 'Fetching news from Alpha Vantage...'
            data = collector.get_news_sentiment(
                tickers=tickers if tickers else None,
                topics=topics if topics else None,
                time_from=time_from if time_from else None,
                time_to=time_to if time_to else None,
                limit=limit,
                sort=sort
            )
            
            articles = data.get('feed', [])
            
            # Filter by search query if provided (searches in title and summary)
            if search_query:
                search_lower = search_query.lower()
                articles = [
                    article for article in articles
                    if search_lower in article.get('title', '').lower() or 
                       search_lower in article.get('summary', '').lower()
                ]
                collection_status['message'] = f'Filtered to {len(articles)} articles matching "{search_query}"'
            
            collection_status['articles_collected'] = len(articles)
            
            if save_to_db:
                collection_status['message'] = f'Saving {len(articles)} articles to database...'
                db_manager = get_db_manager()
                with db_manager:
                    result = db_manager.save_articles(articles)
                    collection_status['message'] = (
                        f'✓ Collection complete! '
                        f'Inserted: {result["inserted"]} new articles, '
                        f'Skipped: {result["skipped"]} duplicates'
                    )
                    collection_status['articles_collected'] = result['inserted']
            else:
                collection_status['message'] = f'✓ Collection complete! Retrieved {len(articles)} articles.'
            
            collection_status['last_run'] = datetime.now().isoformat()
            
        except Exception as e:
            collection_status['message'] = f'✗ Error: {str(e)}'
        finally:
            collection_status['running'] = False
    
    thread = threading.Thread(target=collect_thread)
    thread.daemon = True
    thread.start()
    
    flash('News collection started in background. Check status below.', 'info')
    return redirect(url_for('index'))


@app.route('/api/status')
def get_status():
    """Get collection status (AJAX endpoint)."""
    return jsonify(collection_status)


@app.route('/articles')
def view_articles():
    """View collected articles from database."""
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    sentiment_filter = request.args.get('sentiment', '')
    ticker_filter = request.args.get('ticker', '')
    search_query = request.args.get('search', '')
    
    try:
        db_manager = get_db_manager()
        with db_manager:
            articles, total, stats = get_articles_paginated(
                db_manager, page, per_page, sentiment_filter, ticker_filter, search_query
            )
        
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
            # Parse JSONB fields
            if article['ticker_sentiment']:
                article['ticker_sentiment'] = json.loads(article['ticker_sentiment'])
            if article['topics']:
                article['topics'] = json.loads(article['topics'])
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
                return stats
            finally:
                cursor.close()
    except Exception as e:
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

