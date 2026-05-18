# CloudNote Ingestion Service

Modular Playwright-based microservice for automating lecture attendance ingestion.

## Phase 1: Foundation & Login
This phase implements the basic project structure and automated login flow using async Playwright.

## Project Structure
- `app/main.py`: Entry point.
- `app/config.py`: Configuration management.
- `app/login.py`: Login automation logic.
- `app/selectors.py`: Centralized UI selectors.
- `app/logger.py`: Structured logging.

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a `.env` file (see `.env.example`).
3. Run the service:
   ```bash
   python -m app.main
   ```

## Docker
Build the image:
```bash
docker build -t cloudnote-ingestion .
```
Run the container:
```bash
docker run --env-file .env cloudnote-ingestion
```
