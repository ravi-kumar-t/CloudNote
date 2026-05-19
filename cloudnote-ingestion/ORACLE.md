# Oracle VM Deployment Guide & Runbook for CloudNote Ingestion

This guide outlines the production deployment architecture, setup instructions, and management runbook for migrating the CloudNote Playwright Ingestion Worker to an **Oracle Free Tier VM** as the permanent production runtime.

> [!IMPORTANT]
> **Railway Phase Reminder**: Keep the Railway deployment active. Do NOT decommission Railway until a live lecture join has been successfully validated.

---

## 1. Architecture Overview

To provide maximum reliability on a resource-constrained Oracle Free Tier VM, the production architecture employs **Systemd-level scheduling** rather than a continuously running container:

```
+-------------------------------------------------------------------------------+
|                             Oracle VM (Host Linux)                             |
|                                                                               |
|   +--------------------------+                      +-----------------------+ |
|   |      Systemd Timer       |                      | Persistent Storage    | |
|   |  Runs every 10 mins during|                      | (On Host Filesystem)  | |
|   |    6 PM - 11 PM IST      |                      |                       | |
|   +------------+-------------+                      |  ./logs/              | |
|                |                                    |  (ingestion.log)      | |
|                v (Trigger oneshot)                  |                       | |
|   +------------+-------------+                      |  ./screenshots/       | |
|   |     Systemd Service      |                      |  (*.png, error diagnostics) |
|   | Runs 'docker compose run'|                      +-----------+-----------+ |
|   +------------+-------------+                                  ^             |
|                |                                                |             |
|                v (Creates container)                            | (Mounted)   |
|   +------------+------------------------------------------------+-----------+ |
|   | Docker Container (cloudnote-ingestion-worker)                           | |
|   |                                                                         | |
|   |  +--------------------+   +-------------------+                         | |
|   |  |   Tini (PID 1)     |-->| Playwright Worker |                         | |
|   |  |  (Zombie Reaper)   |   | (app.main)        |                         | |
|   |  +--------------------+   +-------------------+                         | |
|   +-------------------------------------------------------------------------+ |
+-------------------------------------------------------------------------------+
```

### Key Architectural Pillars:
1. **Zero-Idle Memory Footprint**: By using Systemd Timer instead of a 24/7 background process, the container only runs when active classes are possible. Outside the active window (6 PM – 11 PM IST), no Playwright or Chromium processes consume memory or CPU.
2. **Zombie Process Protection**: The container uses Docker's native `init: true` engine. It spawns a tiny init daemon (`tini`) as PID 1, ensuring any orphaned or defunct Chromium/Playwright processes are automatically reaped and cleaned, preventing resource exhaustion.
3. **Graceful Serialization**: Systemd `oneshot` services do not overlap. If a lecture run is already active, Systemd automatically skips or holds subsequent timer ticks, preventing parallel browser instances from colliding.
4. **Persistent Diagnostics**: Log files and error screenshots are mounted to the host filesystem, surviving container restarts and updates.

---

## 2. Pre-requisites & VM Provisioning

Ensure your Oracle Free Tier VM is provisioned with:
- **OS**: Ubuntu 22.04 LTS or Oracle Linux 8/9.
- **CPU/RAM**: VM.Standard.A1.Flex (ARM64) or VM.Standard.E2.1.Micro (AMD64).
- **Network**: Internet access enabled, no public ports needed (the bot is an outbound worker).

---

## 3. Automated VM Deployment

We provide an automated setup helper to initialize the host environment. Run these steps on the VM host:

### Step 3.1: Copy the Source Code to the VM
Clone the project repository to `/opt/cloudnote-ingestion` or upload your workspace files:
```bash
sudo mkdir -p /opt/cloudnote-ingestion
sudo chown -R $USER:$USER /opt/cloudnote-ingestion
# Copy files into this directory...
```

### Step 3.2: Run the Setup Script
Execute the automation setup script under root privileges:
```bash
cd /opt/cloudnote-ingestion/oracle
sudo chmod +x deploy_setup.sh
sudo ./deploy_setup.sh
```

The script will automatically:
1. Install Docker and the Docker Compose plugin (if missing).
2. Set up `./logs` and `./screenshots` directories with correct read/write permissions.
3. Copy the Systemd service and timer files to the system configuration directory.
4. Set the host timezone to `Asia/Kolkata` for accurate timer scheduling.
5. Reload Systemd, register the timer, and start it.
6. Generate a template `.env` file at `/opt/cloudnote-ingestion/.env`.

---

## 4. Configuration & Secrets

After running the setup script, configure your environment secrets:
1. Open the `.env` file:
   ```bash
   sudo nano /opt/cloudnote-ingestion/.env
   ```
2. Replace the credentials with your LPU university credentials:
   ```env
   LPU_USERNAME=your_120xxxxx_username
   LPU_PASSWORD=your_secure_password
   HEADLESS=True
   LOG_LEVEL=INFO
   ```
3. Save and close (`Ctrl+O`, `Ctrl+X`).

---

## 5. Operations & Verification Runbook

### Verify Timer Status
Confirm that the timer is successfully registered and running:
```bash
systemctl status cloudnote.timer
```
Verify the next scheduled run and active hours matching:
```bash
systemctl list-timers --all
```

### Execute a Manual Dry-Run
You can trigger the service manually at any time to verify the end-to-end integration:
```bash
sudo systemctl start cloudnote.service
```

### Check Runtime Logs
Inspect real-time console logs:
```bash
tail -f /opt/cloudnote-ingestion/logs/ingestion.log
```
Or check systemd journal logs:
```bash
journalctl -u cloudnote.service -f
```

### View Screenshots
Any captured screenshots (pre-parsing, lecture state, or error screenshots) will appear in:
```bash
ls -la /opt/cloudnote-ingestion/screenshots/
```

### Clean / Re-build Image
To pull code updates and rebuild the docker image:
```bash
cd /opt/cloudnote-ingestion
docker compose build --no-cache
```

---

## 6. Troubleshooting

| Symptom | Cause | Solution |
|---------|-------|----------|
| **"Current time is outside active hours"** | Tested outside 6 PM – 11 PM IST | This is expected behavior. If you want to force test outside these hours, temporarily edit `ACTIVE_HOURS_START` and `ACTIVE_HOURS_END` inside `app/config.py`. |
| **"Systemd timer not firing"** | System timezone incorrect | Ensure the host time is synchronized. Run `timedatectl` to check. The setup script configures `Asia/Kolkata` automatically. |
| **Defunct Chromium processes** | Zombie processes | Our architecture already uses `init: true` in `docker-compose.yml` to automatically reap these. Ensure you did not remove the `init: true` field. |
| **"LPU MyClass Login timeout"** | Network congestion | Check `screenshots/error_screenshot.png` to check the page state visually. |
