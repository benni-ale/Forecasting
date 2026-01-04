#!/usr/bin/env python3
"""
Distributed worker for fetching company descriptions.
Processes a batch of tickers assigned by the main application.
"""

import os
import sys
import time
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
import json
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Database connection
DB_HOST = os.getenv('DB_HOST', 'news_db')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'news_db')
DB_USER = os.getenv('DB_USER', 'news_user')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'news_password')

# API keys
ALPHA_VANTAGE_API_KEY = os.getenv('ALPHA_VANTAGE_API_KEY', '')
FMP_API_KEY = os.getenv('FMP_API_KEY', '')


def get_db_connection():
    """Get database connection."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )


def get_alpha_vantage_company_overview(ticker, api_key):
    """Fetch company overview from Alpha Vantage API."""
    logger.info(f"Fetching Alpha Vantage OVERVIEW for ticker: {ticker}")
    
    if not api_key:
        logger.error(f"No Alpha Vantage API key provided for ticker {ticker}")
        return None
    
    try:
        url = f"https://www.alphavantage.co/query"
        params = {
            'function': 'OVERVIEW',
            'symbol': ticker,
            'apikey': api_key
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        if not data or "Error Message" in data:
            error_msg = data.get("Error Message", "Unknown error from Alpha Vantage API")
            logger.error(f"Alpha Vantage API Error for {ticker}: {error_msg}")
            return None
        if "Note" in data:
            logger.warning(f"Alpha Vantage API Note for {ticker}: {data['Note']}")
            if len(data) <= 1:  # Only contains "Note"
                return None
        
        # Map Alpha Vantage fields to our database schema
        mapped_data = {
            'ticker': ticker,
            'name': data.get('Name', ticker),
            'business_description': data.get('Description', ''),
            'sector': data.get('Sector', ''),
            'industry': data.get('Industry', ''),
            'exchange': data.get('Exchange', ''),
            'website': data.get('Website', ''),
            'ceo': data.get('CEO', ''),
            'employees': int(data['FullTimeEmployees']) if data.get('FullTimeEmployees') else None,
            'address': data.get('Address', ''),
            'country': data.get('Country', ''),
            'phone': data.get('Phone', ''),
            'city': None,
            'state': None,
            'pipeline_description': None,
        }
        
        # Calculate market_cap if possible
        market_cap_str = data.get('MarketCapitalization')
        if market_cap_str and market_cap_str.isdigit():
            mapped_data['market_cap'] = int(market_cap_str)
        else:
            mapped_data['market_cap'] = None
        
        logger.info(f"Successfully parsed Alpha Vantage data for {ticker}")
        return mapped_data
    
    except Exception as e:
        logger.error(f"Error fetching Alpha Vantage overview for {ticker}: {str(e)}")
        return None


def get_fmp_company_profile(ticker, api_key):
    """Fetch company profile from Financial Modeling Prep API."""
    logger.info(f"Fetching FMP profile for ticker: {ticker}")
    
    if not api_key:
        logger.error(f"No FMP API key provided for ticker {ticker}")
        return None
    
    # Try multiple FMP endpoints
    endpoints = [
        f"https://financialmodelingprep.com/api/v4/company/profile/{ticker}",
        f"https://financialmodelingprep.com/api/v4/profile/{ticker}",
        f"https://financialmodelingprep.com/api/v3/profile/{ticker}"
    ]
    
    for endpoint in endpoints:
        try:
            url = f"{endpoint}?apikey={api_key}"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 403:
                error_text = response.text.lower()
                if 'legacy' in error_text or 'deprecated' in error_text:
                    logger.warning(f"Endpoint {endpoint} is deprecated (403), trying next endpoint...")
                    continue
            
            response.raise_for_status()
            data = response.json()
            
            if isinstance(data, list) and len(data) > 0:
                data = data[0]
            
            if not data or (isinstance(data, dict) and 'Error Message' in data):
                continue
            
            # Map FMP fields to our schema
            mapped_data = {
                'ticker': ticker,
                'name': data.get('companyName', ticker),
                'business_description': data.get('description', ''),
                'sector': data.get('sector', ''),
                'industry': data.get('industry', ''),
                'exchange': data.get('exchangeShortName', ''),
                'market_cap': data.get('mktCap'),
                'website': data.get('website', ''),
                'ceo': data.get('ceo', ''),
                'employees': data.get('fullTimeEmployees'),
                'address': data.get('address', ''),
                'city': data.get('city', ''),
                'state': data.get('state', ''),
                'country': data.get('country', ''),
                'phone': data.get('phone', ''),
                'pipeline_description': None,
            }
            
            logger.info(f"Successfully fetched FMP data for {ticker}")
            return mapped_data
        
        except requests.exceptions.HTTPError as e:
            if response.status_code == 404:
                logger.warning(f"Ticker {ticker} not found in FMP API")
                return None
            elif response.status_code == 403:
                continue
            else:
                logger.error(f"HTTP error fetching FMP profile for {ticker}: Status {response.status_code}")
                continue
        except Exception as e:
            logger.error(f"Error fetching FMP profile for {ticker} from {endpoint}: {str(e)}")
            continue
    
    logger.warning(f"All FMP endpoints failed for {ticker}")
    return None


def save_company_to_db(ticker, company_data):
    """Save or update company data to database (idempotent)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Extract fields
        name = company_data.get('name', ticker)
        description = company_data.get('business_description', '')
        sector = company_data.get('sector', '')
        industry = company_data.get('industry', '')
        exchange = company_data.get('exchange', '')
        market_cap = company_data.get('market_cap')
        website = company_data.get('website', '')
        ceo = company_data.get('ceo', '')
        employees = company_data.get('employees')
        address = company_data.get('address', '')
        city = company_data.get('city', '')
        state = company_data.get('state', '')
        country = company_data.get('country', '')
        phone = company_data.get('phone', '')
        
        # Insert or update (idempotent)
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
        
        conn.commit()
        cursor.close()
        return True
    
    except Exception as e:
        logger.error(f"Error saving company {ticker} to database: {str(e)}", exc_info=True)
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def process_tickers(tickers):
    """Process a batch of tickers."""
    logger.info(f"Worker starting: processing {len(tickers)} tickers")
    
    success_count = 0
    fail_count = 0
    
    for idx, ticker in enumerate(tickers, 1):
        ticker = ticker.strip().upper()
        if not ticker:
            continue
        
        logger.info(f"Processing ticker {idx}/{len(tickers)}: {ticker}")
        
        try:
            company_data = None
            source = None
            
            # Try Alpha Vantage first
            if ALPHA_VANTAGE_API_KEY:
                company_data = get_alpha_vantage_company_overview(ticker, ALPHA_VANTAGE_API_KEY)
                if company_data:
                    source = 'Alpha Vantage'
            
            # Fallback to FMP
            if not company_data and FMP_API_KEY:
                company_data = get_fmp_company_profile(ticker, FMP_API_KEY)
                if company_data:
                    source = 'Financial Modeling Prep'
            
            if company_data:
                success = save_company_to_db(ticker, company_data)
                if success:
                    logger.info(f"Successfully processed {ticker} from {source}")
                    success_count += 1
                else:
                    logger.error(f"Database save failed for {ticker}")
                    fail_count += 1
            else:
                logger.warning(f"No data returned for {ticker}")
                fail_count += 1
            
            # Rate limiting
            if idx < len(tickers):
                time.sleep(0.3)  # ~3 requests per second
        
        except Exception as e:
            logger.error(f"Error processing {ticker}: {str(e)}", exc_info=True)
            fail_count += 1
    
    logger.info(f"Worker completed: {success_count} success, {fail_count} failed")
    return success_count, fail_count


def main():
    """Main entry point for worker."""
    if len(sys.argv) < 2:
        logger.error("Usage: company_worker.py <ticker1> <ticker2> ...")
        sys.exit(1)
    
    tickers = sys.argv[1:]
    logger.info(f"Worker started with {len(tickers)} tickers")
    
    success, failed = process_tickers(tickers)
    
    # Output results as JSON for the main app to parse
    print(json.dumps({
        'success': success,
        'failed': failed,
        'total': len(tickers)
    }))


if __name__ == '__main__':
    main()

