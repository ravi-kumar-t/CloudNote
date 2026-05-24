import os
import sqlite3
import json
import hashlib
import jwt
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

from app.database import get_db_connection, init_db

# Ensure SQLite schema initialized on server start
init_db()

# Ensure screenshots directory exists to prevent FastAPI mount failures
os.makedirs("screenshots", exist_ok=True)

# Prometheus Observability Metrics (Sidecar Integration)
REQUEST_COUNT = Counter(
    "cloudnote_api_requests_total",
    "Total incoming API requests",
    ["method", "endpoint"]
)
SCHEDULER_HEARTBEAT = Gauge(
    "cloudnote_scheduler_heartbeat_timestamp",
    "Epoch timestamp of the last active scheduler heartbeat"
)
ACTIVE_SESSION_STATE = Gauge(
    "cloudnote_active_session_state",
    "Active Playwright session state: 0=IDLE, 1=CONNECTED, 2=RECOVERING, 3=DISCONNECTED, 4=FAILED, 5=CONNECTING"
)
INGESTION_LOOP_STATUS = Gauge(
    "cloudnote_ingestion_loop_status",
    "Current status of the ingestion scheduler: 0=idle, 1=processing, 2=failed, 3=completed"
)

app = FastAPI(title="CloudNote API", version="1.0.0")

# Mount static screenshots directory
app.mount("/screenshots", StaticFiles(directory="screenshots"), name="screenshots")

# Enable CORS for React dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://80.225.202.140:5173",
        "http://localhost:5173",
        "http://localhost:3000",
        "http://80.225.202.140:3000",
        "http://80.225.202.140",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus Request Tracking Middleware
@app.middleware("http")
async def track_prometheus_requests(request, call_next):
    # Exclude metrics endpoint from tracking itself to avoid scrape noise
    if request.url.path != "/metrics":
        REQUEST_COUNT.labels(method=request.method, endpoint=request.url.path).inc()
    response = await call_next(request)
    return response

# JWT Configurations
JWT_SECRET = "cloudnote_premium_jwt_secret_key_888"
JWT_ALGORITHM = "HS256"
SALT = b"cloudnote_secure_hash_salt_999"

# Security utilities (disabling generic auto-error to prevent raw 403 blocks)
security = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    """Zero-dependency PBKDF2 secure password hashing."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), SALT, 100000).hex()

def create_access_token(username: str) -> str:
    """Generates JWT token valid for 24 hours."""
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    payload = {
        "sub": username,
        "exp": expire
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> dict:
    """Dependency injection to authenticate and inject the active user context."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed: Authorization header missing or malformed."
        )
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed: Missing subject claims."
            )
            
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, email FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed: Active profile not found."
            )
            
        return {"id": row["id"], "username": row["username"], "email": row["email"]}
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed: Expired or malformed bearer token."
        )

# Pydantic schemas
class UserRegister(BaseModel):
    username: str
    password: str
    email: str

class UserLogin(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    username: str

class LectureSummaryResponse(BaseModel):
    id: int
    timestamp: str
    subject: str
    summary: str
    topics: List[str]
    key_points: List[str]

# 0. Prometheus Metrics Endpoint (Public sidecar scrape target)
@app.get("/metrics", response_class=PlainTextResponse)
def get_prometheus_metrics():
    """Exposes custom and system metrics dynamically updated at scrape-time."""
    # A. Dynamic Scrape-Time Active Session Status update
    try:
        from app.session_status import get_session_status
        session_data = get_session_status()
        status_str = session_data.get("status", "IDLE").upper()
        state_map = {"IDLE": 0, "CONNECTED": 1, "RECOVERING": 2, "DISCONNECTED": 3, "FAILED": 4, "CONNECTING": 5}
        ACTIVE_SESSION_STATE.set(state_map.get(status_str, 0))
    except Exception:
        pass

    # B. Dynamic Scrape-Time Ingestion Loop Status update
    try:
        status_file = "logs/ingestion_status.json"
        if os.path.exists(status_file):
            with open(status_file, "r", encoding="utf-8") as f:
                ing_data = json.load(f)
            ing_status = ing_data.get("status", "idle").lower()
            ing_map = {"idle": 0, "processing": 1, "failed": 2, "completed": 3}
            INGESTION_LOOP_STATUS.set(ing_map.get(ing_status, 0))
    except Exception:
        pass

    # C. Dynamic Scrape-Time Scheduler Heartbeat update
    try:
        from app.redis_service import redis_service
        hb = redis_service.get_scheduler_heartbeat()
        if hb:
            SCHEDULER_HEARTBEAT.set(hb)
        else:
            SCHEDULER_HEARTBEAT.set(0)
    except Exception:
        pass

    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# 1. Registration endpoint
@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register_user(user: UserRegister):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check uniqueness
        cursor.execute("SELECT id FROM users WHERE username = ?", (user.username,))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username is already registered."
            )
            
        hashed = hash_password(user.password)
        cursor.execute(
            "INSERT INTO users (username, hashed_password, email) VALUES (?, ?, ?)",
            (user.username, hashed, user.email)
        )
        conn.commit()
        conn.close()
        return {"message": "Student account successfully created."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database registration failure: {e}"
        )

# 2. Login endpoint
@app.post("/api/auth/login", response_model=TokenResponse)
def login_user(credentials: UserLogin):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT hashed_password FROM users WHERE username = ?",
            (credentials.username,)
        )
        row = cursor.fetchone()
        conn.close()
        
        if not row or row["hashed_password"] != hash_password(credentials.password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password credentials."
            )
            
        token = create_access_token(credentials.username)
        return {
            "access_token": token,
            "token_type": "bearer",
            "username": credentials.username
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Authentication core error: {e}"
        )

# Ingestion status endpoint
@app.get("/api/ingestion/status")
def get_ingestion_status(current_user: dict = Depends(get_current_user)):
    """Retrieves the active ingestion state of the Playwright bot."""
    status_file = "logs/ingestion_status.json"
    if not os.path.exists(status_file):
        return {
            "status": "idle",
            "details": "No active scraper processes registered yet.",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read ingestion status snapshot: {e}"
        )

# Session status endpoint for screenshot validation
@app.get("/api/session-status")
def get_session_status_api(current_user: dict = Depends(get_current_user)):
    """Retrieves the active browser session join status and validation screenshots."""
    try:
        from app.session_status import get_session_status
        return get_session_status()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load session status metadata: {e}"
        )

# Get cached timetable endpoint
@app.get("/api/timetable")
def get_timetable(current_user: dict = Depends(get_current_user)):
    """Retrieves today's class schedule details from cache."""
    # Resolve path absolutely relative to backend file's parent folder
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    cache_file = os.path.abspath(os.path.join(backend_dir, "..", "logs", "timetable_cache.json"))
    
    print(f"[DEBUG] Timetable Endpoint Request by user: {current_user.get('username')}")
    print(f"[DEBUG] Resolved cache file path: {cache_file}")
    
    if not os.path.exists(cache_file):
        print(f"[DEBUG] Timetable cache file does NOT exist at: {cache_file}")
        return []
        
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        from datetime import timezone, timedelta
        # Compute dates in UTC, IST (UTC+5:30), and server-local
        IST = timezone(timedelta(hours=5, minutes=30))
        today_ist = datetime.now(IST).strftime("%Y-%m-%d")
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_local = datetime.now().strftime("%Y-%m-%d")
        
        cache_date = data.get("date")
        classes = data.get("classes", [])
        
        print(f"[DEBUG] Cache Date: {cache_date}")
        print(f"[DEBUG] Calculated Dates - IST: {today_ist}, UTC: {today_utc}, Local: {today_local}")
        print(f"[DEBUG] Total classes loaded from cache file: {len(classes)}")
        
        # Resilient match against IST, UTC, or Server-local dates
        if cache_date not in (today_ist, today_utc, today_local):
            print(f"[DEBUG] Date mismatch! Stale cache date '{cache_date}' does not match today.")
            return []
            
        print("[DEBUG] Date match successful. Returning cached class array.")
        return classes
    except Exception as e:
        print(f"[DEBUG] Failed to read or parse timetable cache: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read timetable cache: {e}"
        )

# Trigger manual timetable sync endpoint
@app.post("/api/timetable/sync")
def sync_timetable(current_user: dict = Depends(get_current_user)):
    """Triggers an on-demand sync request for the ingestion worker."""
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    sync_file = os.path.abspath(os.path.join(backend_dir, "..", "logs", "sync_request.json"))
    try:
        os.makedirs(os.path.dirname(sync_file), exist_ok=True)
        with open(sync_file, "w", encoding="utf-8") as f:
            json.dump({"sync_requested": True}, f)
        return {"status": "sync_requested", "message": "Manual sync successfully queued."}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to queue sync request: {e}"
        )

# 3. Retrieve all summaries scoped to the authenticated user
@app.get("/api/summaries", response_model=List[LectureSummaryResponse])
def get_user_summaries(current_user: dict = Depends(get_current_user)):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, timestamp, subject, summary, topics_json, key_points_json "
            "FROM lecture_summaries WHERE user_id = ? ORDER BY id DESC",
            (current_user["id"],)
        )
        rows = cursor.fetchall()
        conn.close()
        
        summaries = []
        for r in rows:
            summaries.append({
                "id": r["id"],
                "timestamp": r["timestamp"],
                "subject": r["subject"],
                "summary": r["summary"],
                "topics": json.loads(r["topics_json"]),
                "key_points": json.loads(r["key_points_json"])
            })
        return summaries
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve user summaries: {e}"
        )

# 4. Retrieve single lecture summary detail (auth and scope protected)
@app.get("/api/summaries/{summary_id}", response_model=LectureSummaryResponse)
def get_summary_detail(summary_id: int, current_user: dict = Depends(get_current_user)):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, timestamp, subject, summary, topics_json, key_points_json "
            "FROM lecture_summaries WHERE id = ?",
            (summary_id,)
        )
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Lecture summary record not found."
            )
            
        if row["user_id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied: Summary owned by another student profile."
            )
            
        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "subject": row["subject"],
            "summary": row["summary"],
            "topics": json.loads(row["topics_json"]),
            "key_points": json.loads(row["key_points_json"])
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch summary detail: {e}"
        )
