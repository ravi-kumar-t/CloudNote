# CloudNote: Deployment History & Environment Evolution

This document details the infrastructure environments, host architectures, and deployment methodologies tested throughout the CloudNote project. It distinguishes past deployment experiments from the current, verified operational baseline.

---

## Deployment Environments Matrix

| Environment | Primary Artifacts | Architectural Model | Status | Key Lessons & Constraints |
| :--- | :--- | :--- | :--- | :--- |
| **Phase 1: Local Prototype** | `auto_join_class.py` | Standalone Selenium script | Historical / Inactive | Hardcoded Windows paths, static sleep timing failures. |
| **Phase 2: Railway** | `railway.json`, `RAILWAY.md`, `cloudnote_scheduler.yml` | Containerized cloud worker with webhook cron | Historical / Inactive | Ephemeral disk wipes screenshot proofs; background daemon fails HTTP port health checks. |
| **Phase 3: Oracle VM** | `oracle/`, `ORACLE.md`, `deploy.yml` | Hybrid systemd timer + on-demand container | Historical / Inactive | 1GB RAM constraint; zombie Chromium processes require tini PID 1 init; root disk layer pruning required. |
| **Phase 4: AWS EC2 (PM2)** | `ecosystem.config.js`, `EC2_DEPLOYMENT.md` | Host VM with PM2 process manager | Historical / Inactive | Bare-metal dependency friction; replaced by containerized Playwright images. |
| **Phase 5 & 6: Docker Compose** | `docker-compose.yml`, `prometheus.yml`, `grafana/` | 6 decoupled microservices on bridge network | **ACTIVE VALIDATED BASELINE** | Confirmed locally: Redis caching, timetable sync, zero-class proof, Prometheus scraping, Grafana dashboard. |
| **Phase 7: AWS Cloud** | Docker Compose / Cloud target | AWS cloud production deployment | **PLANNED / NEXT** | Translating validated local baseline into cloud infrastructure. |
| **Phase 7: Kubernetes** | `k8s/*.yaml`, `k8s/README.md` | Declarative multi-pod cluster | **PLANNED / FUTURE** | Syntactically complete; requires resolving `ReadWriteOnce` PVC access mode before multi-node deployment. |

---

## Historical Deployment Attempts

### 1. Railway (Cloud Worker Validation)

- **Configuration Files:**
  - `railway.json`
  - `RAILWAY.md`
  - `.github/workflows/cloudnote_scheduler.yml`
- **Architecture Overview:**
  The ingestion worker was containerized using `railway.json`, specifying `startCommand: "python -m app.main"` with an `ON_FAILURE` restart policy. Because running an idle browser 24/7 was cost-inefficient, a GitHub Actions workflow (`cloudnote_scheduler.yml`) was configured to hit a Railway Deploy Webhook every 5 minutes during class hours (6:30 PM to 10:25 PM IST).

- **Documented Technical Constraints (from repository evidence):**
  1. **HTTP Port Health-Check Mismatch:** As documented in `RAILWAY.md`, Railway expects services to bind to an assigned `$PORT` and respond to HTTP pings. Because the ingestion worker is a pure background daemon that does not run an HTTP server, Railway would report the deployment as unhealthy unless health checks were explicitly disabled or the service was configured strictly as a Worker.
  2. **Ephemeral Disk Proof Loss:** As documented in `RAILWAY.md`, Railway containers run on ephemeral storage by default. When the container stopped or redeployed, all captured attendance proof screenshots in `/app/screenshots` and execution logs in `/app/logs` were discarded. Persistent storage would have required attaching an external Railway Volume.
  3. **Undocumented Runtime Notes:** The repository documents these configuration challenges, but does not preserve the live runtime logs from when the deployment was decommissioned *(marked for manual confirmation)*.

- **Current Status:** Inactive historical experiment. No active code in `app/`, `backend/`, or `frontend/` depends on Railway.

---

### 2. Oracle Cloud Free Tier VM (Hybrid Systemd Architecture)

- **Configuration Files:**
  - `oracle/cloudnote-stack.service`
  - `oracle/cloudnote.service`
  - `oracle/cloudnote.timer`
  - `oracle/deploy_setup.sh`
  - `oracle/nginx.conf`
  - `ORACLE.md`
  - `.github/workflows/deploy.yml`
- **Architecture Overview:**
  Designed for an Oracle Cloud Always-Free Linux VM (1GB–4GB RAM). To stay within strict memory limits, the system decoupled 24/7 web services from the browser automation engine:
  - **Host Nginx:** Handled SSL termination, public routing, and rate-limiting (`1 req/sec` on auth). Proxied `/` to Frontend (`:3000`) and `/api/` to Backend (`:8000`).
  - **`cloudnote-stack.service`:** Managed continuous Docker containers for backend and frontend.
  - **`cloudnote.timer`:** Systemd timer triggering `cloudnote.service` every 10 minutes between 6 PM and 11 PM IST to execute `docker compose run --rm ingestion-worker`. The container was torn down immediately after execution to maintain zero idle memory.

- **Documented Technical Constraints (from repository evidence):**
  1. **Chromium Zombie Processes:** Forceful termination left orphan Chromium child processes. The configuration enforced `init: true` (tini init daemon) in Docker to ensure zombie processes were correctly reaped.
  2. **Disk Layer Buildup:** Frequent Docker Compose rebuilds on small free-tier root disks caused `no space left on device` errors, necessitating automated `docker image prune -f` commands in the deployment script.
  3. **Undocumented Operational Notes:** Complete configuration scripts are preserved in `oracle/`, but operational logs detailing why this VM was not retained as the primary validated environment are not in the repository *(marked for manual confirmation)*.

- **Current Status:** Historical / Standby configuration.

---

### 3. AWS EC2 with PM2 (Process-Managed VM)

- **Configuration Files:**
  - `ecosystem.config.js`
  - `EC2_DEPLOYMENT.md`
  - Root repository `.github/workflows/deploy.yml`
- **Architecture Overview:**
  Deployed directly onto an Ubuntu EC2 virtual machine. PM2 was used to supervise `python3 -m app.main` with parameters:
  ```javascript
  module.exports = {
    apps: [{
      name: 'cloudnote-ingestion',
      script: 'python3',
      args: '-m app.main',
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 10
    }]
  };
  ```
  The root workflow automated updates by running `pm2 restart cloudnote-backend`, `pm2 restart cloudnote-frontend`, and `pm2 restart cloudnote-ingestion` via SSH.

- **Documented Technical Constraints (from repository evidence):**
  1. **Host-Level Dependency Fragility:** Running Playwright under bare-metal PM2 required installing Chromium system libraries, GTK dependencies, and Python virtual environments directly on the host OS.
  2. **Evolution to Containers:** As documented in `EC2_DEPLOYMENT.md`, this approach was superseded by Docker Compose:
     > *"All dependencies for headless Playwright (including Chromium and fonts) are handled natively within the Microsoft Playwright container image on Ubuntu Jammy. No extra host installations are required."*

- **Current Status:** Inactive historical attempt.

---

## Current Validated Deployment Baseline: Docker Compose

The active and verified deployment baseline for CloudNote is the 6-container Docker Compose stack configured in `docker-compose.yml`.

```
+-----------------------------------------------------------------------------------------------+
|                                    DOCKER COMPOSE STACK                                       |
|                                                                                               |
|   +--------------------------+     +--------------------------+     +---------------------+   |
|   |         frontend         |     |         backend          |     |  ingestion-worker   |   |
|   |   Node 20 / Nginx SPA    |     |   FastAPI (Python 3.12)  |     | Playwright Jammy    |   |
|   |   Port: 3000:80          |     |   Port: 8000:8000        |     | Autonomous Runner   |   |
|   +------------+-------------+     +------------+-------------+     +----------+----------+   |
|                |                                |                              |              |
|                | HTTP REST / Polling (10s)      | Shared Storage & Redis State |              |
|                \--------------------------------+------------------------------/              |
|                                                 |                                             |
|                               +-----------------+-----------------+                           |
|                               |                                   |                           |
|                    +----------v----------+             +----------v----------+                |
|                    |        redis        |             |  Persistent Volumes |                |
|                    |    Redis 7 Alpine   |             |  - ./logs           |                |
|                    |    Port: 6379       |             |  - ./screenshots    |                |
|                    +---------------------+             +---------------------+                |
|                                                 |                                             |
|                               +-----------------+-----------------+                           |
|                               | Scrapes /metrics every 5s         |                           |
|                    +----------v----------+             +----------v----------+                |
|                    |     prometheus      |             |       grafana       |                |
|                    |  Prometheus v2.51   | ----------> |  Grafana OSS 10.4   |                |
|                    |  Port: 9090:9090    | Datasource  |  Port: 3001:3000    |                |
|                    +---------------------+             +---------------------+                |
+-----------------------------------------------------------------------------------------------+
```

### The Six Orchestrated Services

1. **`backend` (FastAPI):**
   - Built from `Dockerfile`.
   - Mounts `./logs` (SQLite database) and `./screenshots`.
   - Exposes `/api/timetable`, `/api/session-status`, `/api/summaries`, and `/metrics`.
   - Configured with internal health check testing `http://localhost:8000/debug-meeting`.
2. **`frontend` (React 19 + Vite):**
   - Built from `frontend/Dockerfile`.
   - Exposed on host port `3000`.
   - Communicates with backend over HTTP with 10-second polling.
3. **`ingestion-worker` (Playwright Runner):**
   - Built from `Dockerfile.worker` (`mcr.microsoft.com/playwright/python:v1.49.1-jammy`).
   - Depends on backend passing health checks (`condition: service_healthy`).
   - Operates with `init: true` for zombie process management.
4. **`redis`:**
   - Image `redis:7-alpine`, internal port `6379`.
   - Caches worker session status and scheduler heartbeats.
5. **`prometheus`:**
   - Image `prom/prometheus:v2.51.0`, host port `9090`.
   - Configured via `./prometheus.yml` to scrape `backend:8000/metrics` every 5 seconds.
6. **`grafana`:**
   - Image `grafana/grafana-oss:10.4.1`, host port `3001`.
   - Auto-provisions Prometheus datasource and the **"CloudNote Live Observability"** dashboard.

### Local Baseline Validation Results
In local testing of the multi-container stack:
- All 6 containers initialized and reported healthy.
- The Ingestion Worker connected to Redis, initialized the SQLite database, validated the timetable cache, determined zero classes were scheduled, captured a full-page verification screenshot proof, and entered smart sleep until tomorrow 8:00 AM IST.
- Prometheus successfully scraped `backend:8000/metrics`.
- Grafana connected to Prometheus and loaded all 4 operational panels with live values.

---

## Future Deployment Targets

### 1. Cloud Target: AWS Deployment (Planned)
The validated Docker Compose configuration serves as the immediate foundation for AWS cloud deployment:
- **Target Options:** Single-instance AWS EC2 deployment via Docker Compose, or containerized service management on AWS ECS.
- **Persistent Data:** Mounting AWS Elastic Block Store (EBS) volumes for `./logs` (SQLite) and `./screenshots` to guarantee zero data loss across container lifecycle events.

### 2. Cloud Target: Kubernetes Cluster (Planned / Future Infrastructure)
The repository contains complete Kubernetes manifests in the `k8s/` directory:
- **`namespace.yaml`**: Isolates the `cloudnote` namespace.
- **`configmap.yaml` & `secret.yaml`**: Configuration and credentials.
- **`pvc.yaml`**: Persistent volume claim (2Gi) for logs and screenshots.
- **`backend-deployment.yaml`**: 2 replicas with HTTP liveness/readiness probes.
- **`frontend-deployment.yaml`**: 2 replicas serving the Nginx SPA.
- **`ingestion-worker-deployment.yaml`**: Single-replica background worker.
- **`redis-deployment.yaml`**: Redis pod and internal ClusterIP service.
- **`cronjob.yaml`**: Daily scheduled execution (`0 9 * * *`).

> [!NOTE]
> **Known Kubernetes Pre-Condition:** In `k8s/pvc.yaml`, the access mode is currently set to `ReadWriteOnce`. Before deploying across a multi-node Kubernetes cluster where backend pods and the worker pod reside on different nodes, the storage class must be updated to a shared filesystem supporting `ReadWriteMany` (such as AWS EFS), or user data must be migrated from SQLite to a dedicated PostgreSQL database.
