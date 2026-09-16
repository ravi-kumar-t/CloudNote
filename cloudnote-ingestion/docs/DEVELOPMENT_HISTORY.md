# CloudNote: Development History

This document chronicles the chronological development and architectural progression of the CloudNote project. It details the iterative evolution of the platform from an initial proof-of-concept script into a containerized, observable microservice stack.

---

## Evolution Timeline Summary

```
+---------------------------------------------------------------------------------------+
| PHASE 1: Initial Prototype (Root auto_join_class.py)                                 |
|  - Standalone Selenium automation script                                              |
|  - Proof-of-concept for LPU MyClass login and event clicking                          |
+-------------------------------------------+-------------------------------------------+
                                            | Learned: brittle timing, hardcoded paths
                                            v
+---------------------------------------------------------------------------------------+
| PHASE 2: Railway Deployment Attempt (railway.json, RAILWAY.md)                        |
|  - Cloud containerized background worker                                              |
|  - Triggered via GitHub Actions webhook cron                                          |
+-------------------------------------------+-------------------------------------------+
                                            | Learned: ephemeral disk wipes proofs, $PORT mismatch
                                            v
+---------------------------------------------------------------------------------------+
| PHASE 3: Oracle Cloud Free Tier VM Attempt (oracle/, ORACLE.md)                       |
|  - Systemd timer hybrid architecture (24/7 web stack + on-demand worker)              |
|  - Host Nginx reverse proxy with rate limiting                                        |
+-------------------------------------------+-------------------------------------------+
                                            | Learned: 1GB RAM constraint, Chromium zombie processes
                                            v
+---------------------------------------------------------------------------------------+
| PHASE 4: AWS EC2 / PM2 Deployment Attempt (ecosystem.config.js, EC2_DEPLOYMENT.md)   |
|  - Process-managed deployment on Ubuntu VM                                            |
|  - Transition from bare-metal PM2 to containerized Playwright                         |
+-------------------------------------------+-------------------------------------------+
                                            | Learned: host dependency conflicts, container image superiority
                                            v
+---------------------------------------------------------------------------------------+
| PHASE 5: Current Modular CloudNote Architecture (cloudnote-ingestion/)                |
|  - Decoupled FastAPI backend, React 19 frontend, Playwright daemon                    |
|  - SQLite persistence, Gemini 2.5 Flash summarization, Redis with fallback            |
+-------------------------------------------+-------------------------------------------+
                                            | Milestone: feature completion & zero-class proofing
                                            v
+---------------------------------------------------------------------------------------+
| PHASE 6: Current Local Docker Compose Validation (VALIDATED BASELINE)                 |
|  - 6 services running healthy: backend, frontend, worker, redis, prometheus, grafana  |
|  - Verified timetable sync, zero-class screenshot capture, and smart sleep            |
+-------------------------------------------+-------------------------------------------+
                                            | Next Step
                                            v
+---------------------------------------------------------------------------------------+
| PHASE 7: Cloud Target — AWS Deployment & Future Kubernetes (PLANNED)                  |
|  - Production cloud deployment on AWS infrastructure                                  |
|  - Kubernetes manifests in k8s/ staged as future/planned infrastructure               |
+---------------------------------------------------------------------------------------+
```

---

## Phase 1 — Initial Prototype (Selenium Automation)

- **Status:** **Historical / Inactive**
- **Relevant Files:**
  - `auto_join_class.py` (Project root)
  - `README.md` (Project root)

### What was attempted
Phase 1 was an early proof-of-concept developed to test whether automated browser software could authenticate into the university portal (`https://myclass.lpu.in/`), open the calendar ("View Classes"), locate scheduled class cards (`.fc-event`), and join BigBlueButton lecture sessions.

The implementation used Selenium WebDriver with Chrome options (`--use-fake-ui-for-media-stream`) and media stream preference overrides to automatically grant microphone and camera permissions.

### What was learned from repository evidence
1. **Hardcoded environments break portability:** `auto_join_class.py` contained hardcoded Windows local binary paths (`C:\Users\Ravi\aut\chrome-win64\chrome.exe`) and plaintext student credentials directly in the script.
2. **Static delays are fragile:** The script relied on arbitrary pauses (`time.sleep(2)`, `time.sleep(5)`). These frequently broke when CodeTantra was slow to render or when live sessions presented countdown clocks.
3. **No persistence or intelligence:** The prototype only attempted to enter the room. It had no database, no session health watchdog, no ability to extract lecture content or slides, and no summarization.
4. **Conclusion:** A single monolithic Selenium script was inadequate for real-world attendance tracking and lecture intelligence. This motivated the complete architectural rewrite into the modular asynchronous Playwright system in `cloudnote-ingestion/`.

### Active Code Dependencies
**None.** No active code in `cloudnote-ingestion/` imports, references, or depends on `auto_join_class.py`.

---

## Phase 2 — Railway Deployment Attempt

- **Status:** **Historical / Inactive**
- **Relevant Files:**
  - `cloudnote-ingestion/railway.json`
  - `cloudnote-ingestion/RAILWAY.md`
  - `cloudnote-ingestion/.github/workflows/cloudnote_scheduler.yml`

### What was attempted
Phase 2 attempted to deploy the automated worker as a cloud container on Railway (`railway.json` with `startCommand: "python -m app.main"`).

To avoid running an idle browser worker 24/7 on paid cloud credits, a GitHub Actions cron schedule was configured in `.github/workflows/cloudnote_scheduler.yml` (`cron: '*/5 13-16 * * *'` UTC / 6:30 PM–10:25 PM IST). Every 5 minutes during university hours, the workflow sent an HTTP POST request to a Railway Deploy Webhook (`secrets.RAILWAY_WEBHOOK_URL`) to trigger worker execution.

### What was learned from repository evidence
1. **HTTP health-check mismatch for background daemons:** As documented in `RAILWAY.md`:
   > *"The worker is designed as a background service. It does NOT bind to a `$PORT` and does not run an HTTP server. You must deploy it correctly so Railway doesn't mark the deployment as failed for not responding to HTTP requests."*
2. **Ephemeral storage wipes attendance proof:** As documented in `RAILWAY.md`:
   > *"Ephemeral Storage Notice: Railway containers use ephemeral disks by default. Any screenshots saved to `/app/screenshots` or local logs saved to `/app/logs` will be lost when the container stops or redeploys... To preserve files permanently, you would need to attach a Railway Volume to the `/app/screenshots` path."*
3. **Undocumented runtime failure note:** While storage caveats and health check constraints are clearly documented in the repository, the exact production failure logs that caused Railway to be discontinued are not preserved in the git tree *(marked for manual confirmation)*.

### Active Code Dependencies
**None.** `cloudnote_scheduler.yml` remains in the repository as a historical CI workflow, but active application code does not rely on Railway.

---

## Phase 3 — Oracle Cloud Free Tier VM Attempt

- **Status:** **Historical / Inactive**
- **Relevant Files:**
  - `cloudnote-ingestion/oracle/` (`cloudnote-stack.service`, `cloudnote.service`, `cloudnote.timer`, `deploy_setup.sh`, `nginx.conf`)
  - `cloudnote-ingestion/ORACLE.md`
  - `cloudnote-ingestion/.github/workflows/deploy.yml`

### What was attempted
Phase 3 explored self-hosting the full CloudNote stack on an Oracle Cloud Always-Free VM. Because Oracle Free Tier VMs are strictly resource-constrained (1GB–4GB RAM), running Chromium continuously alongside the web dashboard risked crashing the instance.

The architecture introduced a hybrid model:
- The web application (FastAPI backend + Vite React frontend) ran 24/7 in Docker containers managed by a systemd unit (`cloudnote-stack.service`).
- The heavy Playwright scraper container was launched on demand every 10 minutes (6 PM–11 PM IST) via a systemd timer (`cloudnote.timer`) executing `docker compose run --rm ingestion-worker`.
- An automated provisioning script (`oracle/deploy_setup.sh`) configured host Nginx reverse proxying, log directories, and systemd units.
- Remote deployment was automated via GitHub Actions (`.github/workflows/deploy.yml`) using SSH commands to pull changes and restart containers.

### What was learned from repository evidence
1. **Chromium process reaping:** Headless browser containers left zombie processes when terminated abruptly. As documented in `ORACLE.md`, the worker required `init: true` (tini daemon) to act as PID 1 and reap defunct Chromium processes.
2. **Free-tier disk exhaustion:** Frequent rebuilds filled the root partition. The automated deployment scripts had to incorporate `docker image prune -f` to prevent disk failure.
3. **Zero-idle memory model:** Spinning up the worker container on-demand and immediately destroying it (`--rm`) successfully kept idle RAM near zero outside of active class windows.
4. **Undocumented operational note:** The repository contains fully written configuration files for Oracle, but it was not kept as the verified development environment. Specific external hosting constraints or network reasons for setting it aside are not documented in the repository *(marked for manual confirmation)*.

### Active Code Dependencies
**None.** No active Python or React code depends on the `oracle/` directory or systemd units.

---

## Phase 4 — AWS EC2 / PM2 Deployment Attempt

- **Status:** **Historical / Inactive**
- **Relevant Files:**
  - `cloudnote-ingestion/ecosystem.config.js`
  - `cloudnote-ingestion/EC2_DEPLOYMENT.md`
  - `CloudNote/.github/workflows/deploy.yml` (Root repository workflow)

### What was attempted
Phase 4 explored deploying CloudNote to an Ubuntu EC2 virtual machine using the PM2 Node.js process manager (`ecosystem.config.js`) to manage `python3 -m app.main` with automatic restart on crash (`autorestart: true`, `max_restarts: 10`, `restart_delay: 10000`).

The root workflow `.github/workflows/deploy.yml` automated deployment via SSH, running:
```bash
pm2 restart cloudnote-backend || true
pm2 restart cloudnote-frontend || true
pm2 restart cloudnote-ingestion || true
```

### What was learned from repository evidence
1. **Bare-metal dependency friction:** Running Playwright under bare-metal PM2 required installing Chromium shared libraries, fonts, and Python virtual environments directly on the host operating system.
2. **Transition to Dockerized EC2:** As reflected in `EC2_DEPLOYMENT.md`, the approach transitioned away from PM2 in favor of Docker Compose:
   > *"All dependencies for headless Playwright (including Chromium and fonts) are handled natively within the Microsoft Playwright container image on Ubuntu Jammy. No extra host installations are required."*
3. **RAM requirements documented:** `EC2_DEPLOYMENT.md` established the baseline memory requirement: *"Minimum 2GB RAM (4GB recommended for headless Chromium)."*

### Active Code Dependencies
**None.** `ecosystem.config.js` is not invoked by any active runner or script.

---

## Phase 5 — Current Modular CloudNote Architecture

- **Status:** **Active / Fully Implemented**
- **Relevant Files:**
  - `cloudnote-ingestion/app/` (all 15 core modules)
  - `cloudnote-ingestion/backend/` (`main.py`, `Dockerfile`)
  - `cloudnote-ingestion/frontend/` (`src/App.jsx`, `src/App.css`, `src/main.jsx`, `package.json`, `Dockerfile`)
  - `cloudnote-ingestion/docker-compose.yml`
  - `cloudnote-ingestion/Dockerfile` & `Dockerfile.worker`
  - `cloudnote-ingestion/prometheus.yml`
  - `cloudnote-ingestion/grafana/` (`provisioning/datasources/datasource.yml`, `provisioning/dashboards/dashboard.yml`, `cloudnote_dashboard.json`)

### What was achieved
Phase 5 redesigned CloudNote into an enterprise-grade, asynchronous, observable microservice system:
- **FastAPI Backend:** Provides REST APIs, JWT authentication, timetable cache reads, and static screenshot serving with sub-10ms response times.
- **Vite + React 19 Frontend:** Modern dark-mode glassmorphism dashboard with 10-second polling for active classes, schedule cards, and proof verification modals.
- **Autonomous Ingestion Worker:** Self-scheduling Playwright daemon using `Asia/Kolkata` locale, multi-attempt join state machines, audio bypass automation, and DOM content extraction.
- **Google Gemini 2.5 Flash Pipeline:** Native HTTPS integration with strict JSON schema enforcement and deterministic local fallback.
- **Resilient Redis Service:** Implements key-value session status and distributed locks, with automatic local fallback to `logs/session_status.json` if Redis is offline.
- **Zero-Class Verification Feature:** Full-page CodeTantra screenshot capture on zero-class detection, persisted in cache metadata and rendered in the dashboard with click-to-zoom modal viewer.

---

## Phase 6 — Current Local Docker Compose Validation Baseline

- **Status:** **ACTIVE VALIDATED BASELINE**
- **Relevant Files:**
  - `cloudnote-ingestion/docker-compose.yml`
  - `cloudnote-ingestion/prometheus.yml`
  - `cloudnote-ingestion/grafana/`

### Validation Results
The multi-container Docker Compose stack was launched and verified locally:
1. **`backend` (FastAPI):** Started, passed internal health checks (`/debug-meeting`), and entered `healthy` state.
2. **`frontend` (React):** Started and served on port `3000`.
3. **`redis`:** Started on port `6379`; worker successfully connected and initialized caching.
4. **`ingestion-worker`:** Connected to Redis, initialized the SQLite database, validated the timetable cache, detected zero classes scheduled for today, captured a full-page verification screenshot proof, and entered smart sleep until tomorrow 8:00 AM IST.
5. **`prometheus`:** Successfully scraped `backend:8000/metrics` every 5 seconds; verified target state is `UP`.
6. **`grafana`:** Launched on port `3001`, auto-connected to Prometheus datasource, and rendered the pre-provisioned **"CloudNote Live Observability"** dashboard.

**Conclusion:** Docker Compose is currently the project's official, validated local baseline.

---

## Phase 7 — Cloud Target: AWS Deployment & Future Kubernetes

- **Status:** **PLANNED / FUTURE**
- **Relevant Files:**
  - `cloudnote-ingestion/k8s/` (`namespace.yaml`, `configmap.yaml`, `secret.yaml`, `pvc.yaml`, `cronjob.yaml`, `backend-deployment.yaml`, `frontend-deployment.yaml`, `ingestion-worker-deployment.yaml`, `redis-deployment.yaml`, `services.yaml`, `README.md`)

### Intended Next Steps
1. **AWS Cloud Deployment:** The immediate next deployment target is to take the validated 6-service Docker Compose baseline and deploy it to AWS infrastructure (e.g. AWS EC2 or AWS ECS with persistent volume attachments).
2. **Kubernetes Staging:** The Kubernetes manifests located in `k8s/` provide a declarative cluster specification. They are classified as **future/planned infrastructure** and are not yet deployed in a live cluster.
3. **Known gap to resolve before Kubernetes:** `k8s/pvc.yaml` currently specifies `ReadWriteOnce`, which must be updated to a multi-node shared storage mode (`ReadWriteMany` such as AWS EFS) before scaling backend replicas alongside the worker pod.
