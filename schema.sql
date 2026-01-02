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
        -- Weighted average of sentiment from previous days, with exponential decay (half-life = 7 days)
        -- decay_weight = 0.5^(days_ago / 7)
        -- Formula: SUM(sentiment * weight) / SUM(weight) = weighted average
        -- This ensures the result is in the same scale as weighted_sentiment (0-1 range typically)
        COALESCE(
            CASE 
                WHEN SUM(POWER(0.5, (d1.date - d2.date)::numeric / 7.0)) FILTER (WHERE d2.date <= d1.date AND d2.date >= d1.date - INTERVAL '30 days') > 0
                THEN SUM(d2.weighted_sentiment * POWER(0.5, (d1.date - d2.date)::numeric / 7.0))
                     FILTER (WHERE d2.date <= d1.date AND d2.date >= d1.date - INTERVAL '30 days')
                     / SUM(POWER(0.5, (d1.date - d2.date)::numeric / 7.0))
                     FILTER (WHERE d2.date <= d1.date AND d2.date >= d1.date - INTERVAL '30 days')
                ELSE 0
            END,
            0
        ) as weighted_sentiment_diffused
    FROM daily_aggregations d1
    LEFT JOIN daily_aggregations d2 ON d1.ticker = d2.ticker
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

