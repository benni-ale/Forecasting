#!/bin/sh
# One-shot migration: local Postgres -> Heroku Postgres.
# Companies: full. Articles: last 30 days only (Essential-0 1GB storage).
# Explicit column lists so local/remote column ORDER differences don't matter.
# HURL env var must contain the Heroku DATABASE_URL.
set -e

LOCAL="psql -U newsuser -d newsdb"
REMOTE="psql ${HURL}?sslmode=require -v ON_ERROR_STOP=1"

COLS_COMP="ticker,name,business_description,pipeline_description,sector,industry,exchange,market_cap,website,ceo,employees,address,city,state,country,phone,embedding_vector,last_updated,last_error_date,last_error_message,created_at"
COLS_ART="id,url,title,source,time_published,summary,overall_sentiment_score,overall_sentiment_label,ticker_sentiment,topics,banner_image,source_domain,created_at,updated_at"

echo ">>> [1/3] companies (full)"
$LOCAL -c "COPY (SELECT $COLS_COMP FROM companies) TO STDOUT" \
  | $REMOTE -c "COPY companies ($COLS_COMP) FROM STDIN"

echo ">>> [2/3] articles (last 30 days)"
$LOCAL -c "COPY (SELECT $COLS_ART FROM articles WHERE time_published > CURRENT_DATE - 30) TO STDOUT" \
  | $REMOTE -c "COPY articles ($COLS_ART) FROM STDIN"

echo ">>> [3/3] fix articles id sequence"
$REMOTE -c "SELECT setval('articles_id_seq', COALESCE((SELECT max(id) FROM articles), 1), true)"

echo ">>> verify (remote counts)"
$REMOTE -c "SELECT (SELECT count(*) FROM articles) AS articles, (SELECT count(*) FROM companies) AS companies, (SELECT count(*) FROM article_ticker_sentiment_view) AS view_rows"

echo ">>> DONE"
