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

