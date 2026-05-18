# Railway Deployment Guide for CloudNote

This guide outlines how to deploy the CloudNote Playwright ingestion worker on [Railway](https://railway.app/). Railway is being used as a cloud validation environment before full Kubernetes deployment.

> [!IMPORTANT]
> The worker is designed as a **background service**. It does NOT bind to a `$PORT` and does not run an HTTP server. You must deploy it correctly so Railway doesn't mark the deployment as failed for not responding to HTTP requests.

## 1. GitHub Integration
1. Log in to your Railway dashboard.
2. Click **New Project** -> **Deploy from GitHub repo**.
3. Select the `cloudnote-ingestion` repository.
4. Railway will automatically detect the `Dockerfile` in the root of the project and begin building the image.

## 2. Environment Variables Configuration
Before the first successful run, you need to configure the environment variables.
In the Railway dashboard, click on your newly created service, go to the **Variables** tab, and add the following:

- `LPU_USERNAME`: Your university portal username.
- `LPU_PASSWORD`: Your university portal password.
- `HEADLESS`: `True` (Recommended default for cloud, although the code defaults to True now).

## 3. Worker Service Configuration
To prevent Railway from trying to assign a public domain and failing health checks:
1. Go to your service's **Settings** tab.
2. Under **Deploy**, ensure that the **Start Command** is left empty (it will use the `CMD` from the Dockerfile).
3. Under **Networking**, **DO NOT** generate a public domain.
4. If Railway attempts to assign a `$PORT` and fails the health check, you may need to disable health checks or configure the service type as a Worker if prompted.

## 4. Log Inspection & Troubleshooting
Playwright execution requires tracing through logs.
1. Go to the **Deployments** tab and click on the active deployment.
2. Go to **View Logs**.
3. You should see the startup diagnostics:
   ```
   Starting CloudNote Playwright Ingestion Worker...
   Environment: Cloud/Railway Validation (HEADLESS=True)
   Launching browser...
   ```
4. Check for the 5-minute heartbeat logs (`Worker Status: HEALTHY | Session: ACTIVE`) to confirm sustained execution.

> [!WARNING]
> **Ephemeral Storage Notice:** Railway containers use ephemeral disks by default. Any screenshots saved to `/app/screenshots` or local logs saved to `/app/logs` will be lost when the container stops or redeploys. If an error occurs, look at the stack trace in the Railway UI logs. To preserve files permanently, you would need to attach a Railway Volume to the `/app/screenshots` path.

## 5. Redeploy Workflow
If you make changes to the repository:
1. Push the changes to GitHub.
2. Railway will automatically detect the commit and trigger a new build and deployment.
3. The old container will gracefully terminate (catching `asyncio.CancelledError`), and the new one will spin up.

---

### Migration to Kubernetes / GCP
Once validated on Railway, you can move directly to GCP/GKE using the manifests provided in the `k8s/` directory. The worker is fully stateless (excluding logs/screenshots which use PVCs) and containerized perfectly for standard Kubernetes platforms.
