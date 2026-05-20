import unittest
import os
import sqlite3
import json
from datetime import datetime
from fastapi.testclient import TestClient

# Mock database path during testing
os.environ["DB_PATH"] = "logs/test_cloudnote.db"
import app.database
app.database.DB_PATH = "logs/test_cloudnote.db"

from backend.main import app, hash_password
from app.database import get_db_connection, init_db, save_summary_to_db

class TestDashboardBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ensure log dir exists
        os.makedirs("logs", exist_ok=True)

    def setUp(self):
        # Reset database file
        if os.path.exists("logs/test_cloudnote.db"):
            os.remove("logs/test_cloudnote.db")
        init_db()
        self.client = TestClient(app)

    def tearDown(self):
        if os.path.exists("logs/test_cloudnote.db"):
            os.remove("logs/test_cloudnote.db")

    def test_registration_and_hash(self):
        # Test successful user registration
        payload = {
            "username": "student1",
            "password": "securepassword",
            "email": "student1@lpu.in"
        }
        response = self.client.post("/api/auth/register", json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertIn("Student account successfully created", response.json()["message"])

        # Test hashing is applied correctly
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT hashed_password FROM users WHERE username = ?", ("student1",))
        row = cursor.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["hashed_password"], "securepassword")
        self.assertEqual(row["hashed_password"], hash_password("securepassword"))

        # Test registration uniqueness conflict
        response = self.client.post("/api/auth/register", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Username is already registered", response.json()["detail"])

    def test_login_and_jwt(self):
        # Register a user
        reg_payload = {
            "username": "student2",
            "password": "mypassword",
            "email": "student2@lpu.in"
        }
        self.client.post("/api/auth/register", json=reg_payload)

        # Success login
        login_payload = {
            "username": "student2",
            "password": "mypassword"
        }
        response = self.client.post("/api/auth/login", json=login_payload)
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        self.assertIn("access_token", res_data)
        self.assertEqual(res_data["token_type"], "bearer")
        self.assertEqual(res_data["username"], "student2")

        # Failed login due to invalid password
        failed_payload = {
            "username": "student2",
            "password": "wrongpassword"
        }
        response = self.client.post("/api/auth/login", json=failed_payload)
        self.assertEqual(response.status_code, 401)
        self.assertIn("Invalid username or password credentials", response.json()["detail"])

    def test_user_scoped_summary_retrieval(self):
        # 1. Register 2 separate students
        self.client.post("/api/auth/register", json={"username": "studentA", "password": "passA", "email": "a@lpu.in"})
        self.client.post("/api/auth/register", json={"username": "studentB", "password": "passB", "email": "b@lpu.in"})

        # Log in and get tokens
        resA = self.client.post("/api/auth/login", json={"username": "studentA", "password": "passA"}).json()
        resB = self.client.post("/api/auth/login", json={"username": "studentB", "password": "passB"}).json()
        tokenA = resA["access_token"]
        tokenB = resB["access_token"]

        # 2. Ingest summaries under studentA
        summary_payload = {
            "summary": "This is a scoped lecture summary for student A.",
            "topics": ["React Hooks", "Vite SPAs"],
            "key_points": ["Use useEffect carefully", "HMR is fast"]
        }
        # Under the hood, during ingestion, save_summary_to_db is called under studentA's username
        save_summary_to_db("studentA", summary_payload)

        # 3. Retrieve summaries as Student A -> Should return 1 summary
        headersA = {"Authorization": f"Bearer {tokenA}"}
        response = self.client.get("/api/summaries", headers=headersA)
        self.assertEqual(response.status_code, 200)
        summariesA = response.json()
        self.assertEqual(len(summariesA), 1)
        self.assertEqual(summariesA[0]["subject"], "Ingested Lecture")
        self.assertEqual(summariesA[0]["summary"], "This is a scoped lecture summary for student A.")
        self.assertEqual(summariesA[0]["topics"], ["React Hooks", "Vite SPAs"])

        # 4. Retrieve summaries as Student B -> Should return 0 summaries (strict isolation!)
        headersB = {"Authorization": f"Bearer {tokenB}"}
        response = self.client.get("/api/summaries", headers=headersB)
        self.assertEqual(response.status_code, 200)
        summariesB = response.json()
        self.assertEqual(len(summariesB), 0)

        # 5. Access specific summary detail by ID
        summary_id = summariesA[0]["id"]
        # Student A requesting their own summary -> Success
        detail_res = self.client.get(f"/api/summaries/{summary_id}", headers=headersA)
        self.assertEqual(detail_res.status_code, 200)
        self.assertEqual(detail_res.json()["summary"], "This is a scoped lecture summary for student A.")

        # Student B requesting Student A's summary -> Forbidden 403!
        detail_res = self.client.get(f"/api/summaries/{summary_id}", headers=headersB)
        self.assertEqual(detail_res.status_code, 403)
        self.assertIn("Permission denied", detail_res.json()["detail"])

if __name__ == '__main__':
    unittest.main()
