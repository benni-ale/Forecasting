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

# Make the script executable
RUN chmod +x news_collector.py

# Set default command
ENTRYPOINT ["python", "news_collector.py"]

# Default arguments (can be overridden)
CMD ["--format", "summary"]

