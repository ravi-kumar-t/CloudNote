# 🟣 CloudNote: Autonomous Attendance Ingestion System

CloudNote is a production-grade, state-aware autonomous attendance verification and lecture telemetry ingestion platform. It automates class schedule synchronization, securely joins live virtual lectures (via CodeTantra student portals), logs passive real-time lecture streams, and generates AI-driven lecture summaries—all while preserving bulletproof, visual validation proofs of connection, disconnect, and failure states.

Designed with a stunning **premium dark-mode glassmorphic dashboard**, CloudNote is the ultimate student assistant for live lecture verification and lecture summarization.

---

## 🚀 Key Feature Highlights

### 1. Unified System Status Monitor
* **Dynamic State Themes**: Visual state representation with gorgeous glowing backgrounds:
  * 🟣 `Scheduler Active` (Idle Standby)
  * 🟡 `Connecting` (Automating portal login)
  * 🟢 `Connected to Class` (Connection validation proof captured)
  * 🟠 `Recovering Connection` (Automated stream watchdog healing)
  * 🔴 `Disconnected` (Disconnect validation proof captured)
  * ❌ `Ingestion Failed` (Capture join failures and alert status)
* **Zero CPU Spin**: Features midnight rollovers and timetable caching to prevent redundant server polling on empty timetable days (like Sundays).

### 2. High-Fidelity Visual Proofs
* **Mutually Exclusive Screenshot Rendering**: Shows ONLY relevant proofs based on the outcome (join/disconnect for successful sessions, failure proofs exclusively for failed attempts) to maintain historical integrity.
* **Seamless Zoom Modals**: Double-click any proof thumbnail to open a premium, high-resolution modal displaying exact timestamps and capture metadata.

### 3. Structured Data Summarization
* **Zero-Dependency API REST Layer**: Direct atomic urllib connection to Gemini models.
* **Upsert SQLite Class Deduplication**: Calendar-day scoped daily checking protects the database against duplicate row inserts if a student rejoins a lecture multiple times.

---

## 🛠️ Technology Stack
* **Frontend**: React (Vite, vanilla CSS glassmorphism, responsive grid layouts).
* **Backend**: FastAPI (Python 3.12, SQLite database engine, JSON state stores).
* **Ingestion Scraper**: Playwright (Async Chromium automation, OCR passive extraction).
* **AI Engine**: Google Gemini API integration (lightweight self-healing JSON REST client).

---

## 📂 Project Architecture

```
cloudnote-ingestion/
├── app/                      # Core Playwright & Database Automation Services
│   ├── config.py             # Settings (Pydantic-settings)
│   ├── database.py           # SQLite persistence & UPSERT deduplication
│   ├── gemini_service.py     # Gemini API integration & fallbacks
│   ├── joiner.py             # Playwright CodeTantra LTI joining flow
│   ├── main.py               # Main autonomous unified loop
│   ├── selectors.py          # Centralized UI selectors for LPU portals
│   ├── session_status.py     # Active state & persistent last_session cache
│   └── watchdog.py           # Headless iframe status and connection checks
├── backend/                  # FastAPI 24/7 Web API
│   ├── Dockerfile            # Minimal Python slim container setup
│   └── main.py               # REST API endpoints & CORS middleware
├── frontend/                 # React 24/7 Web Dashboard Client
│   ├── Dockerfile            # Multi-stage compilation & alpine-Nginx container
│   ├── nginx.conf            # SPA routing server configuration
│   ├── package.json          # Node dependencies
│   └── src/
│       ├── App.jsx           # Core Dashboard layout & formatTimestamp helper
│       └── App.css           # Premium glassmorphic stylesheets
├── .github/workflows/        # Automated GitHub Actions Workflows
│   ├── ci.yml                # Core Integration CI (Python E2E & Vite build)
│   ├── deploy.yml            # Automated CD to Oracle VM
│   └── cloudnote_scheduler.yml# Railway integration scheduler trigger
├── Dockerfile                # Ingestion Worker container setup
├── docker-compose.yml        # Multi-container microservice orchestrator
├── verify_pipeline.py        # End-to-end local python pipeline verifier
└── requirements.txt          # Python base dependencies
```

*For in-depth visual diagrams (Architecture, Scheduler Lifecycles, Screenshots flows), please review [ARCHITECTURE.md](ARCHITECTURE.md).*

---

## 📦 Local Setup & Dockerization

### 1. Setup Environment Variables
Create a `.env` file in the root directory:
```env
LPU_USERNAME=your_ums_username
LPU_PASSWORD=your_ums_password
GEMINI_API_KEY=your_gemini_api_key_optional
```

### 2. Standard Manual Setup
To run and develop locally without containers:
```bash
# Install core Python dependencies
pip install -r requirements.txt
playwright install --with-deps chromium

# Run the Ingestion loop
python -m app.main

# Start FastAPI backend
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Start Frontend
cd frontend
npm install
npm run dev
```

### 3. High-Maturity Docker Compose Setup
Run the entire decoupled system (Vite frontend, FastAPI backend, Ingestion worker) instantly using Docker Compose:
```bash
# Build and run the service stack
docker compose up -d --build

# View container logs
docker compose logs -f

# Shut down the stack
docker compose down
```
---

## 🌐 Production Infrastructure & Observability Stack (New!)

CloudNote now includes lightweight enterprise sidecar integrations to demonstrate real production-grade deployment and operations:

### 1. Port & Service Layout
When running the Docker Compose stack, the following services are spun up and mapped to host-only loopbacks:
* 🖥️ **React Frontend**: `http://localhost:3000` (Nginx static compilation server)
* ⚙️ **FastAPI Backend**: `http://localhost:8000` (Core REST engine + Prometheus exporter)
* 🔴 **Redis Cache & Heartbeats**: `http://localhost:6379` (Lightweight optional state/heartbeat caching)
* 📈 **Prometheus Collector**: `http://localhost:9090` (Scrapes FastAPI metrics every 5 seconds)
* 📊 **Grafana Visualization**: `http://localhost:3001` (Pre-loaded system analytics dashboard)

### 2. Grafana Dashboard & Credentials
Open `http://localhost:3001` in your browser. CloudNote comes with a pre-wired datasource and pre-loaded dashboard, rendering active worker metrics instantly:
* **Uptime/System Health**: Shows live REST state gauges.
* **Scheduler Heartbeat Age**: Tracks exactly when the Playwright background daemon checked in.
* **Active Session Status**: Visualizes active states (IDLE, CONNECTED, RECOVERING, FAILED).
* **API Traffic Panel**: Real-time traffic breakdowns by HTTP method and URI route.

> [!WARNING]
> **Demo Credentials**: The default Grafana credentials are set to username `admin` and password `admin` for **demo/development-only** purposes. In true production clusters, configure these dynamically via Kubernetes Secret volumes.

### 3. Resiliency Guardrails
* **No Redis Hard Dependency**: All Redis operations are wrapped in robust connection-pool try/catch fallbacks. If the Redis server is offline (e.g. running outside Docker or local developer unit testing), the backend and ingestion worker seamlessly fall back to local JSON file persistence.
* **Passive Prometheus Scrapes**: Prometheus metrics are 100% passive sidecar exporters. Metrics scrape operations query read-only snapshots and will never block or alter active browser session or scheduler processes.

### 4. Kubernetes Manifest Readiness
All manifests are organized under `k8s/` and ready for cluster deployment, showing clean multi-tier decoupling:
* `namespace.yaml`: Configures the dedicated `cloudnote` isolation namespace.
* `configmap.yaml` & `secret.yaml`: Manage environment properties and UMS login secrets.
* `pvc.yaml`: Declares persistent storage sharing for SQLite database files and logs.
* `redis-deployment.yaml`: Orchestrates the in-memory state caching pod.
* `frontend-deployment.yaml` / `backend-deployment.yaml`: Standalone deployments for our web apps.
* `ingestion-worker-deployment.yaml`: Persistent Pod daemon managing the 24/7 background scheduler loop.
* `services.yaml`: Decoupled Services mapping network addresses to individual pods.

---


## 🧪 Automated Continuous Integration

CloudNote implements automated verification checks via GitHub Actions. On every push and pull request, the [Core Integration CI Workflow](.github/workflows/ci.yml) executes:

1. **Backend Integration Validation**: Installs dependencies and runs `verify_pipeline.py` to assert disk file persistence and database-level row SQLite integrity.
2. **Frontend Compile Validation**: Executes `npm run build` in the `frontend` container environment to ensure zero Vite compilation errors.

To run the pipeline verification script locally:
```bash
python verify_pipeline.py
```

---

## 💎 Demo Flow Guide

Presenting CloudNote is simple, logical, and designed to impress:

1. **The Core Screen**: Open the React Web Dashboard. Highlight the responsive grid layout, the dynamic **System Status Monitor**, and the unified active badge showing the background worker status.
2. **Class Schedule Visualizer**: Point out **Today's Class Schedule**. Show how inactive/empty days display a clean schedule-synced banner instead of technical database lists.
3. **The Proof of Attendance**: Scroll to the **Last Attended Class** segment. Demonstrate the student-friendly terms (*Joined Class*, *Left Class*, *Attendance Status*, *Time in Class*) and the formatted, human-friendly timestamps (e.g., `23 May • 8:00 PM`).
4. **Visual Verification Zoom**: Click a screenshot card. Show the gorgeous modal zoom overlay showcasing exact capture times and browser proofs.
5. **Deduplication Test**: Showcase how the system updates existing rows during re-joins to ensure clean, non-technical, duplicate-free records.

---

## 🔮 Future Scalability SaaS Roadmap (Design Specs)

To scale CloudNote from a single-student local MVP to a mass-market SaaS platform supporting thousands of concurrent active student sessions, we recommend implementing the following future architecture layers:

### 1. Redis Task Queue & Celery Workers
* **Current**: The VM utilizes PM2 and local Python asyncio subprocess blocks.
* **SaaS Design**: Transition to a Redis-based message broker. The API publishes scheduled join actions to a queue, allowing a clustered pool of Celery Workers to spin up sandboxed browser instances on-demand.

### 2. User Isolation & Container Sandbox Orchestration
* **Current**: Shared Chromium context within the VM.
* **SaaS Design**: Orchestrate worker executions using dynamic **Kubernetes Job Pods**. When a class is joinable, K8s spawns an isolated container sandbox for the specific user, eliminating cookie leakage, memory contamination, or cross-tenant privilege escalation.

### 3. Observability & Dashboard Analytics
* **Current**: Local logging to logs/app.log.
* **SaaS Design**: Expose worker metrics ports to a central **Prometheus** collector. Integrate **Grafana dashboards** to provide administrators real-time tracking of memory footprints, OCR text accuracy, and student login failures.

### 4. Advanced Notification Pipelines
* **Current**: Pure web-dashboard polling.
* **SaaS Design**: Add a microservice to trigger push alerts (via Webhooks, Telegram Bots, or Discord Channels) whenever a class is successfully joined, when a recovery watchdog is triggered, or if a manual captcha is requested.
