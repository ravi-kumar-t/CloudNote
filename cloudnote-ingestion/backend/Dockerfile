# Use official slim Python image for performance and minimal size
FROM python:3.12-slim

# Set environment variables for Python stability
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/app/logs/cloudnote.db

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create a dedicated, non-root system user for security
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -m -s /bin/bash appuser

# Copy application layers
COPY backend/ ./backend/
COPY app/ ./app/

# Setup persistent directory ownership
RUN mkdir -p /app/logs && \
    chown -R appuser:appgroup /app

# Switch to the non-root user
USER appuser

# Expose FastAPI port
EXpose 8000

# Start Uvicorn ASGI server
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
