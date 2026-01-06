-- Create articles table for storing financial news with sentiment scores
CREATE TABLE IF NOT EXISTS articles (
    id SERIAL PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    source TEXT,
    time_published TIMESTAMP,
    summary TEXT,
    overall_sentiment_score NUMERIC(5, 4),
    overall_sentiment_label TEXT,
    ticker_sentiment JSONB,
    topics JSONB,
    banner_image TEXT,
    source_domain TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create index on URL for fast lookups (idempotency check)
CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url);

-- Create index on time_published for time-based queries
CREATE INDEX IF NOT EXISTS idx_articles_time_published ON articles(time_published);

-- Create index on overall_sentiment_label for sentiment filtering
CREATE INDEX IF NOT EXISTS idx_articles_sentiment_label ON articles(overall_sentiment_label);

-- Create GIN index on ticker_sentiment JSONB for efficient JSON queries
CREATE INDEX IF NOT EXISTS idx_articles_ticker_sentiment ON articles USING GIN(ticker_sentiment);

-- Create function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Create trigger to automatically update updated_at
DROP TRIGGER IF EXISTS update_articles_updated_at ON articles;
CREATE TRIGGER update_articles_updated_at
    BEFORE UPDATE ON articles
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Create view for daily ticker sentiment aggregations
-- Calculates sentiment * relevance per ticker and day
CREATE OR REPLACE VIEW ticker_daily_sentiment_view AS
WITH ticker_data AS (
    SELECT 
        DATE(time_published) as date,
        jsonb_array_elements(ticker_sentiment) as ticker_info,
        time_published
    FROM articles
    WHERE time_published IS NOT NULL
        AND ticker_sentiment IS NOT NULL
        AND jsonb_array_length(ticker_sentiment) > 0
),
ticker_scores AS (
    SELECT 
        date,
        ticker_info->>'ticker' as ticker,
        (ticker_info->>'ticker_sentiment_score')::numeric as sentiment_score,
        (ticker_info->>'relevance_score')::numeric as relevance_score,
        ticker_info->>'ticker_sentiment_label' as sentiment_label,
        time_published
    FROM ticker_data
    WHERE ticker_info->>'ticker' IS NOT NULL
        AND ticker_info->>'ticker_sentiment_score' IS NOT NULL
        AND ticker_info->>'relevance_score' IS NOT NULL
        AND (ticker_info->>'ticker_sentiment_score')::numeric IS NOT NULL
        AND (ticker_info->>'relevance_score')::numeric IS NOT NULL
),
daily_aggregations AS (
    SELECT 
        ticker,
        date,
        AVG(sentiment_score) as avg_sentiment_score,
        AVG(relevance_score) as avg_relevance_score,
        AVG(sentiment_score * relevance_score) as weighted_sentiment,
        SUM(sentiment_score * relevance_score) as total_weighted_sentiment,
        COUNT(*) as article_count,
        COUNT(*) FILTER (WHERE sentiment_label LIKE '%Bullish%') as bullish_count,
        COUNT(*) FILTER (WHERE sentiment_label LIKE '%Bearish%') as bearish_count,
        COUNT(*) FILTER (WHERE sentiment_label = 'Neutral') as neutral_count,
        MAX(time_published) as last_article_time
    FROM ticker_scores
    WHERE sentiment_score IS NOT NULL 
        AND relevance_score IS NOT NULL
    GROUP BY ticker, date
),
-- Generate all dates for each ticker (from first article date to today, max 1 year back)
all_dates AS (
    SELECT DISTINCT
        ticker,
        generate_series(
            GREATEST(MIN(date), CURRENT_DATE - INTERVAL '1 year'),
            CURRENT_DATE,
            INTERVAL '1 day'
        )::date as date
    FROM daily_aggregations
    GROUP BY ticker
),
-- Join all dates with daily aggregations (LEFT JOIN to include days without news)
all_days_with_aggregations AS (
    SELECT 
        ad.ticker,
        ad.date,
        da.avg_sentiment_score,
        da.avg_relevance_score,
        da.weighted_sentiment,
        da.total_weighted_sentiment,
        COALESCE(da.article_count, 0) as article_count,
        COALESCE(da.bullish_count, 0) as bullish_count,
        COALESCE(da.bearish_count, 0) as bearish_count,
        COALESCE(da.neutral_count, 0) as neutral_count,
        da.last_article_time
    FROM all_dates ad
    LEFT JOIN daily_aggregations da ON ad.ticker = da.ticker AND ad.date = da.date
),
-- Calculate diffused sentiment: for each day, sum contributions from all previous days with decay
diffused_sentiment AS (
    SELECT 
        d1.ticker,
        d1.date,
        d1.avg_sentiment_score,
        d1.avg_relevance_score,
        d1.weighted_sentiment,
        d1.total_weighted_sentiment,
        d1.article_count,
        d1.bullish_count,
        d1.bearish_count,
        d1.neutral_count,
        d1.last_article_time,
        -- Sum of decayed sentiment values (not weighted average)
        -- Each day's sentiment decays exponentially: sentiment * 0.5^(days_ago / 7)
        -- This ensures the value decreases over time even with a single article
        -- Half-life = 7 days means after 7 days, the contribution is halved
        COALESCE(
            SUM(d2.weighted_sentiment * POWER(0.5, (d1.date - d2.date)::numeric / 7.0))
                FILTER (WHERE d2.weighted_sentiment IS NOT NULL),
            0
        ) as weighted_sentiment_diffused
    FROM all_days_with_aggregations d1
    LEFT JOIN all_days_with_aggregations d2 
        ON d1.ticker = d2.ticker 
        AND d2.date <= d1.date 
        AND d2.date >= d1.date - INTERVAL '30 days'
    GROUP BY 
        d1.ticker, d1.date, d1.avg_sentiment_score, d1.avg_relevance_score,
        d1.weighted_sentiment, d1.total_weighted_sentiment, d1.article_count,
        d1.bullish_count, d1.bearish_count, d1.neutral_count, d1.last_article_time
)
SELECT 
    ticker,
    date,
    avg_sentiment_score,
    avg_relevance_score,
    weighted_sentiment,
    total_weighted_sentiment,
    weighted_sentiment_diffused,
    article_count,
    bullish_count,
    bearish_count,
    neutral_count,
    last_article_time
FROM diffused_sentiment
ORDER BY ticker, date DESC;

-- Create table for company descriptions (for semantic similarity)
CREATE TABLE IF NOT EXISTS companies (
    ticker TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    business_description TEXT,  -- Main business description
    pipeline_description TEXT,   -- Product/service pipeline description
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
    embedding_vector FLOAT[],    -- Cached embedding vector (optional)
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_error_date DATE,         -- Date of last error (to avoid reprocessing today)
    last_error_message TEXT,      -- Last error message
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for faster lookups
CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);
CREATE INDEX IF NOT EXISTS idx_companies_sector ON companies(sector);
CREATE INDEX IF NOT EXISTS idx_companies_industry ON companies(industry);
CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name);
CREATE INDEX IF NOT EXISTS idx_companies_last_error_date ON companies(last_error_date);

-- Create table for company embeddings (vectorized company descriptions)
CREATE TABLE IF NOT EXISTS company_embeddings (
    id SERIAL PRIMARY KEY,
    ticker TEXT NOT NULL REFERENCES companies(ticker) ON DELETE CASCADE,
    model_name TEXT NOT NULL,  -- 'openai-text-embedding-3-small', etc.
    embedding_vector FLOAT[] NOT NULL,
    dimension INTEGER NOT NULL,  -- 1536, 384, etc.
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, model_name)
);

-- Create indexes for embeddings table
CREATE INDEX IF NOT EXISTS idx_company_embeddings_ticker ON company_embeddings(ticker);
CREATE INDEX IF NOT EXISTS idx_company_embeddings_model ON company_embeddings(model_name);

-- Create table for correlation matrix cache
CREATE TABLE IF NOT EXISTS company_correlation_matrix (
    id SERIAL PRIMARY KEY,
    model_name TEXT NOT NULL,
    tickers TEXT[] NOT NULL,  -- Ordered list of tickers (defines matrix order)
    matrix_data JSONB NOT NULL,  -- The correlation matrix as JSON array of arrays
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    companies_count INTEGER NOT NULL,
    UNIQUE(model_name)
);

-- Create indexes for correlation matrix table
CREATE INDEX IF NOT EXISTS idx_company_correlation_matrix_model ON company_correlation_matrix(model_name);
CREATE INDEX IF NOT EXISTS idx_company_correlation_matrix_calculated_at ON company_correlation_matrix(calculated_at);

-- Create table for cross-diffused sentiment (sentiment diffused across correlated tickers)
CREATE TABLE IF NOT EXISTS ticker_cross_diffused_sentiment (
    id SERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    model_name TEXT NOT NULL,
    weighted_sentiment_cross_diffused NUMERIC(10, 6) NOT NULL,
    correlation_threshold NUMERIC(5, 4) DEFAULT 0.3,
    time_decay_days INTEGER DEFAULT 7,
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, date, model_name)
);

-- Create indexes for cross-diffused sentiment table
CREATE INDEX IF NOT EXISTS idx_ticker_cross_diffused_ticker ON ticker_cross_diffused_sentiment(ticker);
CREATE INDEX IF NOT EXISTS idx_ticker_cross_diffused_date ON ticker_cross_diffused_sentiment(date);
CREATE INDEX IF NOT EXISTS idx_ticker_cross_diffused_model ON ticker_cross_diffused_sentiment(model_name);
CREATE INDEX IF NOT EXISTS idx_ticker_cross_diffused_ticker_date ON ticker_cross_diffused_sentiment(ticker, date);

-- Create table for decayed sentiment (sentiment with exponential time decay)
CREATE TABLE IF NOT EXISTS ticker_decayed_sentiment (
    id SERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    weighted_sentiment_decayed NUMERIC(10, 6) NOT NULL,
    half_life_days INTEGER DEFAULT 7,
    lookback_days INTEGER DEFAULT 30,
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, date)
);

-- Create indexes for decayed sentiment table
CREATE INDEX IF NOT EXISTS idx_ticker_decayed_ticker ON ticker_decayed_sentiment(ticker);
CREATE INDEX IF NOT EXISTS idx_ticker_decayed_date ON ticker_decayed_sentiment(date);
CREATE INDEX IF NOT EXISTS idx_ticker_decayed_ticker_date ON ticker_decayed_sentiment(ticker, date);

