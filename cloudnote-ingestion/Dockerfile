# Use official Playwright Python image (Jammy based for stability)
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEADLESS=True \
    LOG_LEVEL=INFO

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install only Chromium browser and dependencies
RUN playwright install --with-deps chromium

# Create directories for persistence
RUN mkdir -p logs screenshots

# Copy application code
COPY . .

# Metadata
LABEL maintainer="CloudNote DevOps" \
      version="1.0.0" \
      description="Playwright Ingestion Worker for Lecture Sessions"

# Command to run the application
CMD ["python", "-m", "app.main"]
