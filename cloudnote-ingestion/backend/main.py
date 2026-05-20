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

from app.database import get_db_connection, init_db

# Ensure SQLite schema initialized on server start
init_db()

app = FastAPI(title="CloudNote API", version="1.0.0")

# Enable CORS for React dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify React's dev/prod origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# JWT Configurations
JWT_SECRET = "cloudnote_premium_jwt_secret_key_888"
JWT_ALGORITHM = "HS256"
SALT = b"cloudnote_secure_hash_salt_999"

# Security utilities
security = HTTPBearer()

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

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Dependency injection to authenticate and inject the active user context."""
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
