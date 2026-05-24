# CloudNote Architectural Design Specification

This document provides a highly detailed engineering overview of the CloudNote autonomous lecture joining and attendance proof validation stack. It maps out our current stable MVP architecture, the scheduler state machine, the screenshot validation pipeline, and lays out a production-ready future scaling roadmap.

---

## 1. High-Level Core System Architecture

The following diagram illustrates the interaction between the decoupled microservice layers and the live production sidecar integrations:
- **Client Layer**: Single Page Application built with React (served via Nginx) alongside the pre-built **Grafana Operational Dashboard** rendering active performance metrics.
- **Service Layer**: REST Web API backend powered by FastAPI, responsible for serving cache details, database records, and exporting the Prometheus `/metrics` scrape endpoint.
- **In-Memory Cache & Heartbeat Layer**: Optional Redis container holding ephemeral state caches, active connection telemetry, and periodic scheduler heartbeats.
- **Storage Tier**: Persistent SQLite3 database and JSON state files which serve as a bulletproof local fallback if Redis is offline.
- **Autonomous Ingestion Layer**: Playwright-based background scheduler worker managing CodeTantra student portal logins, countdown checks, and class join automated actions.
- **Telemetry Collection Layer**: Prometheus monitoring daemon pulling scrape records from the backend and provisioning Grafana panels.

```mermaid
graph TD
    subgraph Client Layer
        ReactClient["React SPA (Port 80/3000)"]
        GrafanaDashboards["Grafana dashboard (Port 3001)"]
    end

    subgraph Service Layer
        FastAPIServer["FastAPI Backend (Port 8000)"]
    end

    subgraph In-Memory Cache & Heartbeat
        RedisServer[("Redis Store (Port 6379)")]
    end

    subgraph Storage Tier
        SQLiteDB[("SQLite Database<br/>(cloudnote.db)")]
        JSONCache[("Session Cache<br/>(session_status.json)")]
        DiskStorage["Screenshots Directory<br/>(/app/screenshots)"]
    end

    subgraph Autonomous Ingestion Layer
        PlaywrightWorker["Playwright Ingestion Worker"]
        LPUUMS[("LPU UMS Student Portal<br/>(CodeTantra LTI Integration)")]
    end

    subgraph Telemetry Collection
        PrometheusServer["Prometheus Server (Port 9090)"]
    end

    %% Client Interactions
    ReactClient -- "REST APIs" --> FastAPIServer
    GrafanaDashboards -- "Query Metrics" --> PrometheusServer
    
    %% Backend Interactions
    FastAPIServer -- "Read/Write SQL" --> SQLiteDB
    FastAPIServer -- "Read/Write State" --> RedisServer
    FastAPIServer -- "Fallback Read/Write State" --> JSONCache
    FastAPIServer -- "Serve Proof files" --> DiskStorage

    %% Worker Interactions
    PlaywrightWorker -- "Write State & Heartbeats" --> RedisServer
    PlaywrightWorker -- "Fallback Write State" --> JSONCache
    PlaywrightWorker -- "Headless Timetable Sync" --> SQLiteDB
    PlaywrightWorker -- "Save png captures" --> DiskStorage
    PlaywrightWorker -- "Automation & Scraping" --> LPUUMS

    %% Telemetry Collection
    PrometheusServer -- "Poll /metrics scraper" --> FastAPIServer
```


---

## 2. Scheduler & Rollover State Machine

The scheduler worker executes as a continuous daemon or via scheduled triggers (such as Cron or Systemd). It maintains extreme stability on days with no scheduled classes (like Sundays) and handles date rollovers gracefully without redundant CPU spin:

```mermaid
stateDiagram-v2
    [*] --> Initialize: Boot Worker
    Initialize --> Synchronize: Is cached timetable missing or stale?
    
    state Synchronize {
        [*] --> HeadlessLogin: Authenticate UMS Portal
        HeadlessLogin --> ParseSchedule: Extract today's lecture schedule
        ParseSchedule --> CacheTimetable: Persist in SQLite DB
    }

    Synchronize --> EmptyScheduleBranch: 0 Classes Detected
    Synchronize --> ActiveScheduleBranch: 1+ Classes Detected

    state EmptyScheduleBranch {
        [*] --> CacheSuccess: Mark current date as synced cache
        CacheSuccess --> InterruptionWatch: Sleep until tomorrow 8:00 AM
        InterruptionWatch --> DateRollover: Interrupt sleep if midnight passes
        DateRollover --> Synchronize: Start sync for the new day
    }

    state ActiveScheduleBranch {
        [*] --> ClassCheck: Is there an active class now?
        ClassCheck --> SleepInterval: No active class? Sleep until next class starts
        ClassCheck --> JoinClass: Yes, active class exists
        SleepInterval --> ClassCheck: Wake up
    }
```

---

## 3. Attendance Ingestion & Monitoring Lifecycle

Once an active class is detected, the Playwright automation browser goes through a robust lifecycle: attempting to join, capturing visual proofs, actively logging telemetry, recovering connection errors via a watchdog, and initiating graceful shutdown:

```mermaid
flowchart TD
    Start([Class Start Time Reached]) --> Setup[Initialize Playwright & Browser Context]
    Setup --> JoinPipeline{Attempt to Join Class}
    
    JoinPipeline -- Success --> ConnectedState[Set status CONNECTED<br/>Save Connection Proof Screenshot]
    JoinPipeline -- Failure after Max Attempts --> FailedState[Set status FAILED<br/>Save Failure Proof Screenshot]
    
    ConnectedState --> ExtractLoop[Ingest Chat, Slides, and Notes Telemetry]
    
    ExtractLoop --> Checks{Session Check}
    Checks -- "Exceeds End Time + 2m Grace" --> StopLoop[Initiate Graceful Shutdown]
    Checks -- "Lecture Completed (UI Sign)" --> StopLoop
    Checks -- "Watchdog Session Dead" --> Recovery{Attempt Watchdog Recovery}
    
    Recovery -- Success --> ExtractLoop
    Recovery -- Failure --> DisconnectedState[Set status DISCONNECTED<br/>Save Disconnect Proof Screenshot]
    
    StopLoop --> CaptureDisconnect[Capture Final Disconnect Screenshot]
    CaptureDisconnect --> DisconnectedState
    
    DisconnectedState --> Summarize[Trigger Atomic Gemini Summarization]
    FailedState --> Summarize
    
    Summarize --> SQLitePersist[Upsert SQLite Database Record]
    SQLitePersist --> Complete([Session Finalized])
```

---

## 4. Mutually Exclusive Screenshot Persistence Mapping

Visual proof of attendance is categorized by session outcome. To prevent rendering contradictory images (such as displaying both successful join and failure screens simultaneously), the UI maps state files strictly according to this schema:

```mermaid
graph LR
    subgraph Scraper Execution [main.py]
        JoinWin[Success: Captures Join proof]
        JoinFail[Failure: Captures Failure proof]
        LeaveWin[Success: Captures Disconnect proof]
    end

    subgraph Storage [Disk / Logs]
        DiskSS["/app/screenshots/"]
        JSONLog["logs/session_status.json"]
    end

    subgraph Frontend Rendering [App.jsx]
        ActiveStatus["Unified Status Card"]
        HistoricalCard["Last Attended Class Card"]
    end

    %% Scraper outputs
    JoinWin -- "save file" --> DiskSS
    JoinWin -- "update 'latest_join_screenshot'" --> JSONLog
    JoinFail -- "save file" --> DiskSS
    JoinFail -- "update 'latest_failure_screenshot'" --> JSONLog
    LeaveWin -- "save file" --> DiskSS
    LeaveWin -- "update 'latest_disconnect_screenshot'" --> JSONLog

    %% Frontend reads
    JSONLog -- "API status read" --> ActiveStatus
    JSONLog -- "API history read" --> HistoricalCard
    DiskSS -- "Render Image src" --> ActiveStatus
    DiskSS -- "Render Image src" --> HistoricalCard
```

---

## 5. Production-Ready Future Scalability Architecture

To migrate CloudNote from a single-student local MVP to a high-capacity, multi-tenant SaaS application supporting thousands of concurrent automated sessions, we propose the following horizontally scalable cloud-native design:

```mermaid
graph TD
    subgraph Ingress & Routing
        NginxGateway["Nginx Reverse Proxy & SSL Gateway"]
    end

    subgraph Client Layer
        WebApps["Scale Out React SPAs"]
    end

    subgraph API Application Cluster
        APIInstances["Load-Balanced FastAPI Instances<br/>(Gunicorn workers)"]
    end

    subgraph Asynchronous Message Broker
        RedisBroker["Redis Cluster<br/>(Celery Task Queue & WebSockets)"]
    end

    subgraph Distributed Worker Pool
        WorkerPod1["Worker Pod 1 (User Sandbox 1)"]
        WorkerPod2["Worker Pod 2 (User Sandbox 2)"]
        WorkerPod3["Worker Pod N (User Sandbox N)"]
    end

    subgraph Highly Available Storage Tier
        PostgresDB[("PostgreSQL Database Cluster<br/>(Multi-tenant Isolated Roles)")]
        S3Storage[("AWS S3 Bucket<br/>(Compressed Screenshots Archive)")]
    end

    subgraph Telemetry & Observability
        PrometheusIngest["Prometheus Ingest Worker"]
        GrafanaDashboards["Grafana Analytics dashboards"]
    end

    %% Flows
    WebApps -- "HTTPS Requests" --> NginxGateway
    NginxGateway -- "Load Balance" --> APIInstances
    APIInstances -- "Enqueue Scraping Jobs" --> RedisBroker
    RedisBroker -- "Distribute Workload" --> DistributedWorkerPool
    
    %% Worker persistence
    DistributedWorkerPool -- "Post summaries & analytics" --> PostgresDB
    DistributedWorkerPool -- "Upload images securely" --> S3Storage

    %% Telemetry hooks
    DistributedWorkerPool -- "Expose metrics port 9090" --> PrometheusIngest
    APIInstances -- "Expose metrics" --> PrometheusIngest
    PrometheusIngest -- "Query data source" --> GrafanaDashboards
```

### Future Scalability Detail Specs:
1. **Asynchronous Redis Queues**: Integrates Redis as a broker. The API scheduler publishes scheduled join events to the Celery queue instead of self-spawning Playwright workers inside the VM.
2. **Worker Isolation**: Each student's Playwright worker executes inside a sandboxed, isolated, lightweight container (e.g. dynamic Kubernetes Pod) to prevent cookie contamination, resource starvation, or cross-student privilege escalation.
3. **Observability Stack**: Prometheus scrapes state metrics from all active containers while Grafana acts as the operational nerve center, alerting administrators of network anomalies, OCR failures, or student login failures.
4. **Reliable Storage**: Replaces SQLite with a robust, highly available multi-tenant PostgreSQL system and stores screenshot blobs securely inside object storage (such as AWS S3 or Google Cloud Storage) behind expiring signed URLs.
