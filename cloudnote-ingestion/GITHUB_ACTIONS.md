# CloudNote Event-Driven Architecture

This document explains the GitHub Actions + Railway event-driven scheduling architecture designed for the CloudNote ingestion worker.

## Architecture Diagram

```mermaid
flowchart TD
    A[GitHub Actions Scheduler\n(Cron Job)] -->|curl POST Webhook| B[Railway Deploy Webhook]
    B -->|Triggers Deploy/Restart| C[Railway Container Runtime]
    C -->|Boots Docker| D[Playwright Worker Python Script]
    D -->|Authenticates & Navigates| E[LPU Portal]
    E -->|Lecture State: UPCOMING| D
    D -->|Internal Sleep| D
    D -->|Lecture State: JOINABLE| F[Active Session Heartbeat]
    E -->|Lecture State: COMPLETED/None| G[Graceful Shutdown & Exit]
```

## Scheduler Flow
To prevent running a 24/7 daemon, we use GitHub Actions to trigger Railway exactly when needed.

1. **GitHub Action (`cloudnote_scheduler.yml`)**: Wakes up every 5 minutes during the active window.
2. **Railway Trigger**: The action sends an HTTP POST request to the `RAILWAY_WEBHOOK_URL` secret.
3. **Railway Runtime**: Railway spins up the container.
4. **Session Lifecycle**: 
   - The worker checks if the time is within the allowed window.
   - It performs semantic lecture state detection.
   - If a lecture is UPCOMING, it calculates the countdown and sleeps internally until joinable.
   - If a lecture is JOINABLE, it joins and maintains an active session heartbeat.
   - If no lectures exist or they are completed, the worker gracefully exits, releasing resources and shutting down the Railway container until the next cron trigger.

## Cron Timing and Timezones

The active window for classes is **6:30 PM to 10:30 PM IST**. 
GitHub Actions cron schedules are exclusively in **UTC**.

To convert IST to UTC, we subtract 5 hours and 30 minutes.
- 6:30 PM IST = 18:30 IST = 13:00 UTC
- 10:30 PM IST = 22:30 IST = 17:00 UTC

**Our Cron Expression**: `*/5 13-16 * * *`
- This runs every 5 minutes.
- It operates during hours 13, 14, 15, and 16 UTC.
- This maps perfectly to 6:30 PM to 10:25 PM IST.

## Active Runtime Window Logic
The Python worker itself includes an internal safeguard:
`ACTIVE_HOURS_START = 18` and `ACTIVE_HOURS_END = 23` (IST).
If the worker is somehow triggered manually outside of this 6 PM to 11 PM IST window, it will instantly shut down gracefully to prevent rogue session costs.

## Secrets Required
To enable this architecture, you must configure the following repository secret in GitHub:
- `RAILWAY_WEBHOOK_URL`: The specific trigger URL provided in your Railway project settings (under the Deployments tab of your service).
