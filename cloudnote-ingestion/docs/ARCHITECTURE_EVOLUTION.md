# CloudNote: Architecture Evolution

This document details the architectural paradigms, subsystem designs, and data flow transformations that shaped CloudNote from its original prototype into the current observable microservice platform.

---

## Architectural Paradigms Across Phases

```
Phase 1: Monolithic Script
+-------------------------------------------------------------+
| auto_join_class.py (Selenium + Chrome + Plaintext Creds)    |
+-------------------------------------------------------------+

Phase 3: Host-Bound Hybrid Architecture
+-------------------------------------------------------------+
| Host OS Linux (Nginx) -> Systemd Timer -> Docker Worker     |
+-------------------------------------------------------------+

Phase 5 & 6: Decoupled Observable Microservices (CURRENT VALIDATED BASELINE)
+-------------------------------------------------------------+
| React SPA (3000) <---> FastAPI Backend (8000) <---> SQLite |
|                            |               ^                |
|                      Prometheus (9090)     | (State / Lock) |
|                            |               v                |
|                       Grafana (3001)     Redis (6379)       |
|                                            ^                |
| Ingestion Worker (Playwright) -------------+                |
|  - Smart Sleep / Zero-Class Proof / Gemini 2.5 Flash        |
+-------------------------------------------------------------+

Phase 7: Cloud Target (AWS / Kubernetes)
+-------------------------------------------------------------+
| AWS EC2/ECS Cloud Deploy | Multi-Pod K8s Cluster (Staged)   |
+-------------------------------------------------------------+
```

---

## 1. Evolution of the Ingestion Engine

### Phase 1: Monolithic Synchronous Selenium
- **Mechanism:** Synchronous Python execution using Selenium WebDriver.
- **Timing:** Static delays using `time.sleep()`.
- **Failure Handling:** None. If an element selector changed or timed out, the script crashed with unhandled exceptions.
- **Portability:** Tied to a specific developer Windows machine (`C:\Users\Ravi\aut\chrome-win64\chrome.exe`).

### Current: Modular Asynchronous Playwright State Machine
- **Mechanism:** Asynchronous execution using Python `asyncio` and `playwright.async_api`.
- **Locale & Timezone:** Enforces `Asia/Kolkata` timezone and `en-IN` locale across browser contexts to ensure university timetable alignment regardless of server location.
- **Priority State Machine:** `app/joiner.py` evaluates active lecture pages across 5 distinct priority states:
  1. `FACULTY_NOT_STARTED`: Scans for explicit text (`"not started yet"`, `"class not started"`). Saves verification screenshot `not_started_<timestamp>.png` and avoids redundant join loops.
  2. `JOINABLE_ACTIVE`: Detects active Join buttons, handles meeting room iframe navigation, and completes the WebRTC echo test dialog.
  3. `UPCOMING`: Detects on-page countdown clocks (`\d+h \d+m \d+s`), computing exact wait intervals.
  4. `NOT_YET_AVAILABLE`: Handles temporary pre-join delays with short retry intervals.
  5. `COMPLETED`: Detects ended sessions, capturing diagnostic artifacts.
- **Autonomous Scheduling:** Implements smart low-power sleep in `app/main.py`: sleeps until 5 minutes before upcoming classes, or sleeps until 8:00 AM tomorrow when no classes remain, releasing 100% of browser resources.

---

## 2. Evolution of State & Cache Management

### Phase 1: Stateless Execution
- Every run required opening the browser, re-authenticating with the portal, and querying the DOM. There was no local cache or persistence.

### Phase 3: File-Only Status
- Used flat JSON files on disk (`logs/session_status.json`) to track basic runtime states.

### Current: Hybrid Redis & Resilient Local File Fallback
- **Redis Primary:** In `app/redis_service.py`, worker runtime status is written to Redis key `cloudnote:session_status` (1-hour TTL), heartbeat is updated every 30s in `cloudnote:scheduler_heartbeat` (45s TTL), and distributed mutexes are acquired via `cloudnote:lock:timetable_sync`.
- **Resilient Fallback Mode:** If Redis is offline (e.g. during local development without Redis running), connection errors are caught seamlessly, and state is redirected to `logs/session_status.json`.
- **Daily Timetable Cache:** `app/timetable_cache.py` validates `logs/timetable_cache.json` against the current IST calendar date. If valid, the system bypasses portal login, providing sub-10ms API responses.
- **Manual Sync Queue:** The frontend triggers `POST /api/timetable/sync`, writing `logs/sync_request.json`, which the worker picks up asynchronously on its next cycle.

---

## 3. Evolution of Verification & Proofs

### Phase 1: Zero Verification
- No screenshots or logs were generated. The user had no way of knowing whether a class was joined.

### Phase 3: Basic Connection Proofs
- Captured `connected_<timestamp>.png` upon successful join and `disconnect_<timestamp>.png` upon session conclusion.

### Current: End-to-End Attendance & Timetable Proofing
- **Zero-Class Proof System:** When zero classes are detected for the day, the scraper takes a full-page screenshot of CodeTantra calendar (`screenshots/no_classes_<timestamp>.png`), registers the path and timestamp in cache metadata, and prunes older duplicates.
- **Dedicated Backend Endpoints:**
  - `GET /api/timetable/no-classes-info`: Returns JSON metadata (`available`, `filename`, `url`, `verified_at`).
  - `GET /api/timetable/no-classes-screenshot`: Streams the binary image with custom `X-Verified-At` headers.
- **Interactive UI Proofing:** React dashboard displays the verified screenshot thumbnail with a green `ShieldCheck` badge and a click-to-zoom modal viewer with download capabilities.
- **Historical Proof Preservation:** Completed sessions persist join, disconnect, and failure screenshots in `logs/class_history.json`.

---

## 4. Evolution of Content Extraction & Lecture Intelligence

### Phase 1: Zero Content Handling
- No content extraction or text processing.

### Current: Multi-Frame Extraction & Google Gemini 2.5 Flash Pipeline
- **DOM Scraper:** `app/extractor.py` scans all nested iframes every 30 seconds to collect:
  - Chat text: `.chat-message-text`, `.msg-body`
  - Presentation text: `svg text`, `div[data-presentation]`
  - Live captions: `.cc-text-line`
- **Rolling Memory Buffer:** Stores extracted lines with a 500-line ceiling to prevent memory bloat.
- **Gemini 2.5 Flash Summarization:** `app/gemini_service.py` formats accumulated text into an academic prompt enforcing a strict JSON schema (`summary`, `topics`, `key_points`). It invokes Google Generative Language API via standard library `urllib.request`.
- **Deterministic AI Fallback:** If the Gemini API returns an HTTP error (e.g. 429 quota exhausted or network error), the service automatically generates a structured local fallback summary using the first 5 captured lines, ensuring data is never lost.
- **Database Persistence:** `app/database.py` stores summaries in the SQLite `lecture_summaries` table with date-based deduplication.

---

## 5. Evolution of Monitoring & Observability

### Phase 1: Console Print Statements
- Relied on raw terminal `print()` statements without timestamps, levels, or log rotation.

### Phase 3: Structured Logging & Health Pings
- Introduced `app/logger.py` with formatted console and file logging (`logs/ingestion.log`).

### Current: Full Prometheus & Grafana Observability
- **Prometheus Exporter:** `backend/main.py` exposes `/metrics` with dynamic scrape-time gauges:
  - `cloudnote_api_requests_total`: Counter tracking API endpoints and HTTP methods.
  - `cloudnote_active_session_state`: Gauge (0=IDLE, 1=CONNECTED, 2=RECOVERING, 3=DISCONNECTED, 4=FAILED, 5=CONNECTING).
  - `cloudnote_ingestion_loop_status`: Gauge (0=idle, 1=processing, 2=failed, 3=completed).
  - `cloudnote_scheduler_heartbeat_timestamp`: Epoch heartbeat timestamp.
- **Prometheus Scraper:** `prometheus.yml` scrapes the backend every 5 seconds.
- **Grafana Dashboards:** Pre-provisioned via `grafana/provisioning/` to visualize:
  - Active Browser Session Status (color-coded stat badge)
  - Ingestion Daemon Status (color-coded stat badge)
  - Scheduler Heartbeat Delta (evaluates worker liveness)
  - FastAPI Dynamic API Traffic (time-series request rate graph)

---

## 6. Architecture Evolution Summary

| Subsystem | Phase 1 (Prototype) | Phase 3 (Oracle Hybrid) | Phase 5/6 (Current Validated Baseline) | Phase 7 (Planned Cloud) |
| :--- | :--- | :--- | :--- | :--- |
| **Automation** | Selenium (sync) | Playwright (on-demand container) | Playwright (autonomous daemon with smart sleep) | Cloud containerized worker |
| **Frontend** | None | React served by Nginx | React 19 SPA with polling & modal proof zoom | Cloud CDN / Nginx container |
| **Backend** | None | FastAPI in Docker | FastAPI with JWT, SQLite, and `/metrics` | Scaled cloud API service |
| **Database** | None | SQLite (shared host volume) | SQLite with deduplication & date filtering | SQLite / PostgreSQL migration |
| **State / Cache**| None | Flat files | Redis key-value with resilient file fallback | Managed Redis (ElastiCache) |
| **Observability**| Console prints | File logs | Prometheus (5s scrape) + Grafana dashboards | CloudWatch / Grafana Cloud |
| **Verification** | None | Join / disconnect screenshots | Zero-class full-page proof + modal viewer | S3-backed proof archive |
