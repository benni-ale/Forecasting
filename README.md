# Financial News Collector with Sentiment Analysis

A containerized Python application that collects financial news with sentiment scores using the Alpha Vantage API.

## Features

- 📰 Collect financial news from Alpha Vantage API
- 😊 Get sentiment scores for news articles
- 📊 Filter by tickers, topics, and time ranges
- 🐳 Fully containerized with Docker
- 💾 Save results to JSON files or PostgreSQL database
- 📝 Multiple output formats (JSON, text, summary)
- 🌐 **Web Dashboard** - GUI to collect news and visualize articles

## Prerequisites

- Docker installed on your system
- Alpha Vantage API key (free at [alphavantage.co](https://www.alphavantage.co/support/#api-key))

## Quick Start

### 1. Get Your API Key

Sign up for a free API key at [Alpha Vantage](https://www.alphavantage.co/support/#api-key).

### 2. Configure API Key

Create a `.env` file in the project root:

```bash
# Copy the example file
cp env.example .env

# Edit .env and add your API key
ALPHA_VANTAGE_API_KEY=your_actual_api_key_here
```

**Important:** The `.env` file is already in `.gitignore` so your API key won't be committed to git.

### 3. Build and Run with Docker Compose

```bash
docker-compose up --build
```

This will:
- Start PostgreSQL database
- Build the Docker image
- Start the **Web Dashboard** on http://localhost:5000
- Run the news collector (optional, can be triggered from dashboard)

### 4. Access the Web Dashboard

Open your browser and navigate to:
```
http://localhost:5000
```

The dashboard provides:
- **Collection Form**: Set parameters (tickers, topics, dates, etc.) and collect news
- **Statistics**: View total articles, sentiment distribution
- **Articles Browser**: Browse, search, and filter collected articles
- **Real-time Status**: Monitor collection progress

The default command includes `--save-db`, so articles are automatically saved to PostgreSQL. Duplicate articles (based on URL) are automatically skipped.

### Alternative: Build the Docker Image Manually

If you prefer not to use docker-compose:

```bash
docker build -t news-collector .
```

### 3. Run the Container

#### Basic Usage (Summary Format)

```bash
docker run --rm -e ALPHA_VANTAGE_API_KEY=your_api_key_here news-collector
```

#### Get News for Specific Tickers

```bash
docker run --rm -e ALPHA_VANTAGE_API_KEY=your_api_key_here \
  news-collector --tickers "AAPL,MSFT,GOOGL" --limit 20
```

#### Get News by Topics

```bash
docker run --rm -e ALPHA_VANTAGE_API_KEY=your_api_key_here \
  news-collector --topics "technology,earnings" --limit 30
```

#### Get News in JSON Format

```bash
docker run --rm -e ALPHA_VANTAGE_API_KEY=your_api_key_here \
  news-collector --format json --limit 10
```

#### Save Results to File

```bash
docker run --rm -e ALPHA_VANTAGE_API_KEY=your_api_key_here \
  -v $(pwd)/output:/app/output \
  news-collector --format json --save --output-file output/news.json
```

#### Get News with Time Range

```bash
docker run --rm -e ALPHA_VANTAGE_API_KEY=your_api_key_here \
  news-collector --time-from "20240101T0000" --time-to "20240131T2359" --limit 50
```

## Command Line Options

| Option | Description | Example |
|--------|-------------|---------|
| `--api-key` | Alpha Vantage API key (or use env var) | `--api-key YOUR_KEY` |
| `--tickers` | Comma-separated stock tickers | `--tickers "AAPL,MSFT"` |
| `--topics` | Comma-separated topics | `--topics "technology,earnings"` |
| `--time-from` | Start time (YYYYMMDDTHHMM) | `--time-from "20240101T0000"` |
| `--time-to` | End time (YYYYMMDDTHHMM) | `--time-to "20240131T2359"` |
| `--limit` | Number of results (max 1000) | `--limit 100` |
| `--sort` | Sort order (LATEST/EARLIEST) | `--sort LATEST` |
| `--format` | Output format (json/text/summary) | `--format json` |
| `--save` | Save results to JSON file | `--save` |
| `--output-file` | Output filename | `--output-file news.json` |

## Available Topics

Alpha Vantage supports various topics including:
- `blockchain`
- `earnings`
- `ipo`
- `mergers_and_acquisitions`
- `financial_markets`
- `economy_fiscal`
- `economy_monetary`
- `economy_macro`
- `energy_transportation`
- `finance`
- `life_sciences`
- `manufacturing`
- `real_estate`
- `retail_wholesale`
- `technology`

## Output Formats

### Summary Format (Default)
Shows a concise summary with the first 10 articles including:
- Title, source, and time
- Ticker-specific sentiment scores
- Overall sentiment score

### Text Format
Detailed text output with all articles including:
- Full article details
- Complete sentiment analysis
- URLs and summaries

### JSON Format
Raw JSON output from the API, suitable for further processing.

## Sentiment Scores

The API provides:
- **Ticker Sentiment**: Relevance and sentiment for specific tickers mentioned
- **Overall Sentiment**: Overall sentiment score and label (Bullish/Bearish/Neutral)
- **Sentiment Score Range**: -1.0 (most bearish) to +1.0 (most bullish)

## Example Output

```
Total articles: 50
Last updated: 20240115T120000

================================================================================
Title: Apple Reports Record Q4 Earnings
Source: Financial Times
Time: 20240115T100000
  AAPL: Relevance: 0.95, Sentiment: Bullish (0.75)
Overall Sentiment: Bullish (0.65)
--------------------------------------------------------------------------------
```

## Database Integration

The application supports saving articles to a PostgreSQL database with **idempotent insertion** (no duplicates).

### Database Schema

The `articles` table stores:
- Article metadata (title, source, URL, time published)
- Sentiment scores (overall and ticker-specific)
- JSONB fields for ticker sentiments and topics
- Automatic timestamps (created_at, updated_at)

### Saving to Database

Articles are saved idempotently using the URL as a unique key. Running the collector multiple times will:
- Insert new articles
- Skip articles that already exist (based on URL)

**Example:**
```bash
# Save articles to database
docker-compose run --rm news-collector --save-db --tickers "AAPL" --limit 50

# Run again - duplicates will be skipped automatically
docker-compose run --rm news-collector --save-db --tickers "AAPL" --limit 50
```

### Accessing the Database

Connect to PostgreSQL:
```bash
# Using docker-compose
docker-compose exec db psql -U newsuser -d newsdb

# Or from host (if port is exposed)
psql -h localhost -p 5432 -U newsuser -d newsdb
```

**Useful queries:**
```sql
-- Count total articles
SELECT COUNT(*) FROM articles;

-- Get latest articles
SELECT title, source, time_published, overall_sentiment_label 
FROM articles 
ORDER BY time_published DESC 
LIMIT 10;

-- Articles by sentiment
SELECT overall_sentiment_label, COUNT(*) 
FROM articles 
GROUP BY overall_sentiment_label;

-- Search articles by ticker in sentiment data
SELECT title, ticker_sentiment 
FROM articles 
WHERE ticker_sentiment::text LIKE '%AAPL%';
```

## Using with Docker Compose

The `docker-compose.yml` is already configured with PostgreSQL. To customize:

```yaml
services:
  news-collector:
    command: ["--format", "summary", "--save-db"]
```

Then run:
```bash
docker-compose up --build
```

## Local Development (Without Docker)

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Set environment variable:
```bash
export ALPHA_VANTAGE_API_KEY=your_api_key_here
```

3. Run the script:
```bash
python news_collector.py --tickers "AAPL" --format summary
```

## API Rate Limits

Alpha Vantage free tier has rate limits:
- 5 API calls per minute
- 500 API calls per day

Plan your usage accordingly or upgrade to a premium plan.

## Error Handling

The application handles:
- Missing API keys
- API rate limits
- Network errors
- Invalid parameters
- API errors

## License

This project is provided as-is for educational and development purposes.

## Support

For Alpha Vantage API issues, visit: https://www.alphavantage.co/support/

