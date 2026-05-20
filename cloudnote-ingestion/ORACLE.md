# Oracle VM Production Deployment Guide & Runbook

This guide outlines the production deployment architecture, host hardening configurations, and operational runbook for running the full CloudNote platform (Web Dashboard + Ingestion Scheduler) on an **Oracle Free Tier VM** as a self-hosted production ecosystem.

---

## 1. Production Architecture Overview

To provide maximum reliability and a **zero-idle memory footprint** on a resource-constrained Oracle Free Tier VM, the production architecture employs a hybrid strategy. It decouples the 24/7 web application services from the heavy browser automation container:

```
+-----------------------------------------------------------------------------------------------+
|                                    Oracle VM (Host Linux)                                     |
|                                                                                               |
|   +---------------------------------------------------------------------------------------+   |
|   | Host Nginx Reverse Proxy (Port 80/443 with SSL)                                       |   |
|   |  - Public traffic routing, SSL termination (Certbot), API Rate-limiting               |   |
|   +---------+-----------------------------------------+-----------------------------------+   |
|             |                                         |                                       |
|             | (Proxy: /)                              | (Proxy: /api/)                        |
|             v                                         v                                       |
|   +---------+--------------------+          +---------+-------------------+                   |
|   | Frontend Container (Vite SPA)|          | Backend Container (FastAPI) |                   |
|   |  - Port 3000 (Internal)      |          |  - Port 8000 (Internal)     |                   |
|   +------------------------------+          +---------+-------------------+                   |
|                                                       |                                       |
|                                                       | (Shared database connection)          |
|                                                       v                                       |
|                                             +---------+-------------------+                   |
|   +------------------------------+          | Persistent Storage Volume   |                   |
|   | Ingestion Worker Container   | -------->|  - logs/cloudnote.db (SQLite) |                   |
|   |  - Playwright Scraper        | (Shared) |  - logs/ingestion.log       |                   |
|   |  - Spun up/torn down on demand|          |  - screenshots/             |                   |
|   +--------------^---------------+          +-----------------------------+                   |
|                  |                                                                            |
|                  | (On-demand trigger: docker compose run)                                    |
|   +--------------+---------------+                                                            |
|   | Systemd Service & Timer      |                                                            |
|   |  - Runs every 10 mins during |                                                            |
|   |    6 PM - 11 PM IST          |                                                            |
|   +------------------------------+                                                            |
+-----------------------------------------------------------------------------------------------+
```

### Architectural Highlights:
1. **Zero-Idle Ingestion Footprint**: The Playwright scraper uses a **Systemd Timer** to spin up on-demand containers (`docker compose run --rm`) during active hours (6 PM – 11 PM IST). The container is completely destroyed immediately upon exit, freeing up 100% of memory and CPU outside runs.
2. **Zombie Process Protection**: The worker container runs with `init: true` enabling a tiny init daemon (`tini`) to act as PID 1, automatically reaping defunct Chromium child processes and preventing resource exhaustion on the VM host.
3. **Shared Persistence Mount**: Both the FastAPI Backend and the Ingestion Worker bind-mount the host folder `./logs` to `/app/logs`. This enables them to share the SQLite database file (`cloudnote.db`) with file-level locking persistence.
4. **Origin Consolidation & CORS Elimination**: Host Nginx acts as a single gateway reverse-proxying `/` to the Frontend container and `/api/` to the Backend container, combining everything on a single origin and eliminating CORS headers.
5. **Secure Local Bindings**: Docker containers only expose ports internally (`127.0.0.1`), ensuring all external public traffic must go through the hardened Nginx gateway.

---

## 2. Hardened Configuration Files

### 2.1 Unified Docker Compose (`docker-compose.yml`)
Located at the project root, this file defines the unified multi-container stack:
- **`backend`**: FastAPI listening internally on `127.0.0.1:8000`.
- **`frontend`**: React served by Nginx, listening internally on `127.0.0.1:3000`.
- **`ingestion-worker`**: Playwright scraper launched only as a short-lived worker (no restart policy).

---

## 3. Host Initialization & Installation

We provide an automated VM provisioning script. Follow these steps directly on the VM:

### Step 3.1: Copy Code to the VM Host
Clone the project repository to `/opt/cloudnote-ingestion` or upload your workspace files:
```bash
sudo mkdir -p /opt/cloudnote-ingestion
sudo chown -R $USER:$USER /opt/cloudnote-ingestion
# Copy files into this directory...
```

### Step 3.2: Run the VM Setup Script
Execute the automation setup script under root privileges:
```bash
cd /opt/cloudnote-ingestion/oracle
sudo chmod +x deploy_setup.sh
sudo ./deploy_setup.sh
```

This will automatically:
1. Install Docker, Docker Compose, and host Nginx (if missing).
2. Set up `./logs` and `./screenshots` folders with secure read/write access.
3. Copy the Nginx server block to `/etc/nginx/sites-available/cloudnote`, enable it, and reload Nginx.
4. Install all Systemd services:
   - `cloudnote-stack.service` (manages the 24/7 Backend and Frontend containers).
   - `cloudnote.service` + `cloudnote.timer` (manages the 10-minute scheduled scraper).
5. Set the VM host timezone to `Asia/Kolkata` for accurate scheduler execution.
6. Enable all services to boot automatically.

---

## 4. Reverse Proxy and Security Hardening

The Nginx configuration template (`oracle/nginx.conf`) has been pre-configured with industry best practices:
- **API Rate Limiting**: Limit zone `auth_limit` restricts login attempts to `1 request per second` (burst of 5) to block brute-force password cracking.
- **Server Signature Hiding**: `server_tokens off` disables returning Nginx version details in header payloads.
- **Strict Security Headers**: Injects `X-Frame-Options`, `X-Content-Type-Options`, `X-XSS-Protection`, and `Content-Security-Policy`.

---

## 5. HTTPS / Let's Encrypt SSL Activation

To enable full production SSL and upgrade connections to HTTPS:

### Step 5.1: Install Certbot
On the VM Host (assuming Ubuntu/Debian), run:
```bash
sudo apt-get update
sudo apt-get install -y certbot python3-certbot-nginx
```

### Step 5.2: Request Let's Encrypt Certificate
Obtain a certificate using the Nginx certbot plugin:
```bash
sudo certbot --nginx -d yourdomain.com
```
*(Certbot will automatically verify ownership, generate the certificates, and prompt you to automatically redirect HTTP traffic to HTTPS).*

### Step 5.3: Verify Nginx Configuration
Certbot will update `/etc/nginx/sites-available/cloudnote` automatically. You can review the updated SSL paths inside this file. Test the Nginx syntax:
```bash
sudo nginx -t
sudo systemctl restart nginx
```

---

## 6. Continuous Integration & CD Pipeline

The automated deployment workflow is located at `.github/workflows/deploy.yml`. 

### Setup Deployment GitHub Secrets:
To activate automated delivery upon pushing updates to `main` or `master`, add the following **Actions Secrets** to your GitHub repository settings:
- `VM_HOST`: The public IP address of your Oracle Free Tier VM.
- `VM_USER`: The SSH login user (e.g., `ubuntu` or `opc`).
- `SSH_PRIVATE_KEY`: The contents of your private SSH key matching the public key authorized on the VM.
- `VM_PORT`: The SSH port (usually `22` or custom).

### CD Execution Flow:
1. Pushes to `main`/`master` (or manual triggers) launch the GitHub Runner.
2. The Runner securely authenticates with the VM using the provided SSH private key.
3. It navigates to `/opt/cloudnote-ingestion` on the VM.
4. It performs `git fetch` and resets the codebase to the latest commit.
5. It runs `docker compose up -d --build backend frontend` to build and redeploy the 24/7 web application without downtime.
6. It triggers `docker image prune -f` to clean up dangling layers, keeping VM storage clean.

---

## 7. Operations & Diagnostic Commands

Use these commands on the VM host to manage your production deployment:

### Check Status of Dashboard Stack:
```bash
sudo systemctl status cloudnote-stack.service
```

### Check Status of Ingestion Scheduler:
```bash
sudo systemctl status cloudnote.timer
```

### Inspect Live Web Dashboard Logs:
```bash
sudo journalctl -u cloudnote-stack.service -f
```

### Inspect Scheduled Ingestion Execution Logs:
```bash
sudo journalctl -u cloudnote.service -f
```

### Inspect Shared Application Logs:
```bash
tail -f /opt/cloudnote-ingestion/logs/ingestion.log
```
