# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy requirements file
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY news_collector.py .
COPY app.py .
COPY templates/ ./templates/
COPY static/ ./static/

# Make the scripts executable
RUN chmod +x news_collector.py app.py

# Default command (can be overridden in docker-compose)
CMD ["python", "news_collector.py", "--format", "summary"]

