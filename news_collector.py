#!/usr/bin/env python3
"""
Financial News Collector with Sentiment Analysis
Uses Alpha Vantage API to collect financial news with sentiment scores.
"""

import os
import json
import logging
import requests
from datetime import datetime
from typing import List, Dict, Optional
import argparse
import psycopg2
from psycopg2.extras import execute_values
from psycopg2 import sql

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class AlphaVantageNewsCollector:
    """Collects financial news with sentiment scores from Alpha Vantage API."""
    
    BASE_URL = "https://www.alphavantage.co/query"
    
    def __init__(self, api_key: str):
        """
        Initialize the news collector.
        
        Args:
            api_key: Alpha Vantage API key
        """
        if not api_key:
            raise ValueError("API key is required")
        self.api_key = api_key
    
    def get_news_sentiment(
        self,
        tickers: Optional[str] = None,
        topics: Optional[str] = None,
        time_from: Optional[str] = None,
        time_to: Optional[str] = None,
        limit: int = 50,
        sort: str = "LATEST"
    ) -> Dict:
        """
        Get news and sentiment data from Alpha Vantage.
        
        Args:
            tickers: Comma-separated list of stock tickers (e.g., "AAPL,MSFT")
            topics: Comma-separated list of topics (e.g., "technology,earnings")
            time_from: Start time in YYYYMMDDTHHMM format
            time_to: End time in YYYYMMDDTHHMM format
            limit: Number of results to return (default: 50, max: 1000)
            sort: Sort order - "LATEST" or "EARLIEST" (default: "LATEST")
        
        Returns:
            Dictionary containing news and sentiment data
        """
        params = {
            "function": "NEWS_SENTIMENT",
            "apikey": self.api_key,
            "limit": limit,
            "sort": sort
        }
        
        if tickers:
            params["tickers"] = tickers
        if topics:
            params["topics"] = topics
        if time_from:
            params["time_from"] = time_from
        if time_to:
            params["time_to"] = time_to
        
        try:
            logger.info(f"Fetching news from Alpha Vantage: tickers={tickers}, topics={topics}, "
                       f"limit={limit}, sort={sort}, time_from={time_from}, time_to={time_to}")
            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            # Check for API errors
            if "Error Message" in data:
                logger.error(f"Alpha Vantage API Error: {data['Error Message']}")
                raise ValueError(f"API Error: {data['Error Message']}")
            if "Note" in data:
                logger.warning(f"Alpha Vantage API Note: {data['Note']}")
                raise ValueError(f"API Note: {data['Note']}")
            
            feed_count = len(data.get('feed', []))
            logger.info(f"Successfully retrieved {feed_count} articles from Alpha Vantage")
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to connect to Alpha Vantage API: {str(e)}", exc_info=True)
            raise ConnectionError(f"Failed to connect to Alpha Vantage API: {str(e)}")
    
    def format_news_output(self, data: Dict, output_format: str = "json") -> str:
        """
        Format news data for output.
        
        Args:
            data: News data from API
            output_format: Output format - "json", "text", or "summary"
        
        Returns:
            Formatted string output
        """
        if "feed" not in data:
            return "No news data available"
        
        feed = data["feed"]
        
        if output_format == "json":
            return json.dumps(data, indent=2)
        
        elif output_format == "summary":
            summary = []
            summary.append(f"Total articles: {len(feed)}")
            summary.append(f"Last updated: {data.get('last_updated', 'N/A')}")
            summary.append("\n" + "="*80 + "\n")
            
            for article in feed[:10]:  # Show first 10
                summary.append(f"Title: {article.get('title', 'N/A')}")
                summary.append(f"Source: {article.get('source', 'N/A')}")
                summary.append(f"Time: {article.get('time_published', 'N/A')}")
                
                # Sentiment scores
                ticker_sentiments = article.get("ticker_sentiment", [])
                if ticker_sentiments:
                    for ticker_sent in ticker_sentiments:
                        summary.append(
                            f"  {ticker_sent.get('ticker', 'N/A')}: "
                            f"Relevance: {ticker_sent.get('relevance_score', 'N/A')}, "
                            f"Sentiment: {ticker_sent.get('ticker_sentiment_label', 'N/A')} "
                            f"({ticker_sent.get('ticker_sentiment_score', 'N/A')})"
                        )
                
                overall_sentiment = article.get("overall_sentiment_score", "N/A")
                overall_label = article.get("overall_sentiment_label", "N/A")
                summary.append(f"Overall Sentiment: {overall_label} ({overall_sentiment})")
                summary.append("-" * 80)
            
            return "\n".join(summary)
        
        else:  # text format
            output = []
            for article in feed:
                output.append(f"Title: {article.get('title', 'N/A')}")
                output.append(f"Source: {article.get('source', 'N/A')}")
                output.append(f"Time: {article.get('time_published', 'N/A')}")
                output.append(f"URL: {article.get('url', 'N/A')}")
                output.append(f"Summary: {article.get('summary', 'N/A')}")
                
                # Sentiment information
                ticker_sentiments = article.get("ticker_sentiment", [])
                if ticker_sentiments:
                    output.append("Ticker Sentiments:")
                    for ticker_sent in ticker_sentiments:
                        output.append(
                            f"  {ticker_sent.get('ticker', 'N/A')}: "
                            f"Relevance={ticker_sent.get('relevance_score', 'N/A')}, "
                            f"Sentiment={ticker_sent.get('ticker_sentiment_label', 'N/A')} "
                            f"(Score: {ticker_sent.get('ticker_sentiment_score', 'N/A')})"
                        )
                
                overall_sentiment = article.get("overall_sentiment_score", "N/A")
                overall_label = article.get("overall_sentiment_label", "N/A")
                output.append(f"Overall Sentiment: {overall_label} (Score: {overall_sentiment})")
                output.append("\n" + "="*80 + "\n")
            
            return "\n".join(output)
    
    def save_to_file(self, data: Dict, filename: Optional[str] = None):
        """
        Save news data to a JSON file.
        
        Args:
            data: News data to save
            filename: Optional filename (default: news_sentiment_YYYYMMDD_HHMMSS.json)
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"news_sentiment_{timestamp}.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"Data saved to {filename}")


class DatabaseManager:
    """Manages database operations for storing articles idempotently."""
    
    def __init__(
        self,
        host: str = None,
        port: int = None,
        database: str = None,
        user: str = None,
        password: str = None
    ):
        """
        Initialize database connection.
        
        Args:
            host: Database host (default: from DB_HOST env var or 'localhost')
            port: Database port (default: from DB_PORT env var or 5432)
            database: Database name (default: from DB_NAME env var)
            user: Database user (default: from DB_USER env var)
            password: Database password (default: from DB_PASSWORD env var)
        """
        self.host = host or os.getenv("DB_HOST", "localhost")
        self.port = port or int(os.getenv("DB_PORT", "5432"))
        self.database = database or os.getenv("DB_NAME", "newsdb")
        self.user = user or os.getenv("DB_USER", "newsuser")
        self.password = password or os.getenv("DB_PASSWORD", "newspass")
        self.conn = None
    
    def connect(self):
        """Establish database connection."""
        try:
            self.conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password
            )
            self.conn.autocommit = False
            return True
        except psycopg2.Error as e:
            raise ConnectionError(f"Failed to connect to database: {str(e)}")
    
    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None
    
    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
    
    def parse_time_published(self, time_str: str) -> Optional[datetime]:
        """
        Parse Alpha Vantage time format to datetime.
        
        Args:
            time_str: Time string in format YYYYMMDDTHHMMSS
        
        Returns:
            datetime object or None
        """
        if not time_str:
            return None
        try:
            # Format: YYYYMMDDTHHMMSS
            return datetime.strptime(time_str, "%Y%m%dT%H%M%S")
        except ValueError:
            return None
    
    def save_articles(self, articles: List[Dict]) -> Dict[str, int]:
        """
        Save articles to database idempotently.
        
        Uses URL as unique key to prevent duplicates.
        
        Args:
            articles: List of article dictionaries from API
        
        Returns:
            Dictionary with counts: {'inserted': X, 'skipped': Y, 'total': Z}
        """
        if not self.conn:
            logger.error("Database connection not established")
            raise ConnectionError("Database connection not established")
        
        if not articles:
            logger.warning("No articles to save")
            return {'inserted': 0, 'skipped': 0, 'total': 0}
        
        logger.info(f"Saving {len(articles)} articles to database (idempotent)")
        inserted = 0
        skipped = 0
        
        cursor = self.conn.cursor()
        
        try:
            for article in articles:
                url = article.get('url')
                if not url:
                    logger.warning(f"Article missing URL, skipping: {article.get('title', 'Unknown')[:50]}")
                    skipped += 1
                    continue
                
                # Check if article already exists (idempotency)
                cursor.execute(
                    "SELECT id FROM articles WHERE url = %s",
                    (url,)
                )
                if cursor.fetchone():
                    skipped += 1
                    continue
                
                # Parse time_published
                time_published = self.parse_time_published(
                    article.get('time_published')
                )
                
                # Convert ticker_sentiment to JSONB
                ticker_sentiment = json.dumps(article.get('ticker_sentiment', []))
                
                # Convert topics to JSONB
                topics = json.dumps(article.get('topics', []))
                
                # Parse overall_sentiment_score
                overall_sentiment_score = article.get('overall_sentiment_score')
                if overall_sentiment_score:
                    try:
                        overall_sentiment_score = float(overall_sentiment_score)
                    except (ValueError, TypeError):
                        overall_sentiment_score = None
                
                # Insert article
                insert_query = """
                    INSERT INTO articles (
                        url, title, source, time_published, summary,
                        overall_sentiment_score, overall_sentiment_label,
                        ticker_sentiment, topics, banner_image, source_domain
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                """
                
                cursor.execute(insert_query, (
                    url,
                    article.get('title', ''),
                    article.get('source', ''),
                    time_published,
                    article.get('summary', ''),
                    overall_sentiment_score,
                    article.get('overall_sentiment_label', ''),
                    ticker_sentiment,
                    topics,
                    article.get('banner_image', ''),
                    article.get('source_domain', '')
                ))
                
                inserted += 1
            
            self.conn.commit()
            logger.info(f"Database transaction committed: {inserted} inserted, {skipped} skipped")
            
        except psycopg2.Error as e:
            self.conn.rollback()
            logger.error(f"Database error during article save: {str(e)}", exc_info=True)
            raise RuntimeError(f"Database error: {str(e)}")
        finally:
            cursor.close()
        
        return {
            'inserted': inserted,
            'skipped': skipped,
            'total': len(articles)
        }
    
    def get_article_count(self) -> int:
        """
        Get total number of articles in database.
        
        Returns:
            Total count of articles
        """
        if not self.conn:
            raise ConnectionError("Database connection not established")
        
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM articles")
            count = cursor.fetchone()[0]
            return count
        finally:
            cursor.close()


def main():
    """Main function to run the news collector."""
    parser = argparse.ArgumentParser(
        description="Collect financial news with sentiment scores from Alpha Vantage"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.getenv("ALPHA_VANTAGE_API_KEY"),
        help="Alpha Vantage API key (or set ALPHA_VANTAGE_API_KEY env var)"
    )
    parser.add_argument(
        "--tickers",
        type=str,
        help="Comma-separated list of stock tickers (e.g., AAPL,MSFT,GOOGL)"
    )
    parser.add_argument(
        "--topics",
        type=str,
        help="Comma-separated list of topics (e.g., technology,earnings,ipo)"
    )
    parser.add_argument(
        "--time-from",
        type=str,
        help="Start time in YYYYMMDDTHHMM format"
    )
    parser.add_argument(
        "--time-to",
        type=str,
        help="End time in YYYYMMDDTHHMM format"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of results to return (default: 50, max: 1000)"
    )
    parser.add_argument(
        "--sort",
        type=str,
        choices=["LATEST", "EARLIEST"],
        default="LATEST",
        help="Sort order (default: LATEST)"
    )
    parser.add_argument(
        "--format",
        type=str,
        choices=["json", "text", "summary"],
        default="summary",
        help="Output format (default: summary)"
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save results to a JSON file"
    )
    parser.add_argument(
        "--output-file",
        type=str,
        help="Output filename (only used with --save)"
    )
    parser.add_argument(
        "--save-db",
        action="store_true",
        help="Save articles to PostgreSQL database (idempotent)"
    )
    parser.add_argument(
        "--db-host",
        type=str,
        help="Database host (or use DB_HOST env var)"
    )
    parser.add_argument(
        "--db-port",
        type=int,
        help="Database port (or use DB_PORT env var)"
    )
    parser.add_argument(
        "--db-name",
        type=str,
        help="Database name (or use DB_NAME env var)"
    )
    parser.add_argument(
        "--db-user",
        type=str,
        help="Database user (or use DB_USER env var)"
    )
    parser.add_argument(
        "--db-password",
        type=str,
        help="Database password (or use DB_PASSWORD env var)"
    )
    
    args = parser.parse_args()
    
    # Validate API key
    if not args.api_key:
        logger.error("API key is required. Set ALPHA_VANTAGE_API_KEY environment variable or use --api-key")
        print("Error: API key is required. Set ALPHA_VANTAGE_API_KEY environment variable or use --api-key")
        return 1
    
    try:
        logger.info("Starting news collection")
        logger.info(f"Parameters: tickers={args.tickers}, topics={args.topics}, limit={args.limit}, "
                   f"sort={args.sort}, time_from={args.time_from}, time_to={args.time_to}")
        
        # Initialize collector
        collector = AlphaVantageNewsCollector(args.api_key)
        
        # Fetch news
        logger.info("Fetching financial news with sentiment scores...")
        print("Fetching financial news with sentiment scores...")
        data = collector.get_news_sentiment(
            tickers=args.tickers,
            topics=args.topics,
            time_from=args.time_from,
            time_to=args.time_to,
            limit=args.limit,
            sort=args.sort
        )
        
        # Display results
        output = collector.format_news_output(data, args.format)
        print(output)
        
        # Save to file if requested
        if args.save:
            logger.info(f"Saving to file: {args.output_file}")
            collector.save_to_file(data, args.output_file)
        
        # Save to database if requested
        if args.save_db:
            logger.info("Saving articles to database...")
            print("\nSaving articles to database...")
            try:
                db_manager = DatabaseManager(
                    host=args.db_host,
                    port=args.db_port,
                    database=args.db_name,
                    user=args.db_user,
                    password=args.db_password
                )
                
                with db_manager:
                    articles = data.get('feed', [])
                    if articles:
                        result = db_manager.save_articles(articles)
                        total_count = db_manager.get_article_count()
                        logger.info(f"Database save complete: {result['inserted']} inserted, "
                                  f"{result['skipped']} skipped, total in DB: {total_count}")
                        print(f"✓ Inserted: {result['inserted']} new articles")
                        print(f"✓ Skipped: {result['skipped']} duplicates")
                        print(f"✓ Total articles in database: {total_count}")
                    else:
                        logger.warning("No articles to save")
                        print("No articles to save")
                        
            except Exception as e:
                logger.error(f"Error saving to database: {str(e)}", exc_info=True)
                print(f"Error saving to database: {str(e)}")
                return 1
        
        logger.info("News collection completed successfully")
        return 0
        
    except Exception as e:
        logger.error(f"Error during news collection: {str(e)}", exc_info=True)
        print(f"Error: {str(e)}")
        return 1


if __name__ == "__main__":
    exit(main())

