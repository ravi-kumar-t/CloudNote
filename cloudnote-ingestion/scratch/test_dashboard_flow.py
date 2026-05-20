import unittest
import os
import sys
import sqlite3
import json
from datetime import datetime

# Adjust sys.path to support importing app and backend modules from the root directory
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Override database path for isolated unit tests to prevent clobbering production/seeded data
TEST_DB_PATH = "logs/test_cloudnote.db"
os.environ["DB_PATH"] = TEST_DB_PATH

import app.database
app.database.DB_PATH = TEST_DB_PATH

from fastapi.testclient import TestClient
from backend.main import app as fastapi_app, hash_password
from app.database import get_db_connection, init_db, save_summary_to_db

class TestDashboardFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs("logs", exist_ok=True)

    def setUp(self):
        # Reset testing database file before each test to maintain state purity
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except PermissionError:
                pass
        init_db()
        self.client = TestClient(fastapi_app)

    def tearDown(self):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except PermissionError:
                pass

    def test_authentication_flow(self):
        print("\n[TEST] Validating User Authentication & JWT Issuance...")
        # 1. Register a test student
        reg_payload = {
            "username": "student_alpha",
            "password": "alpha_password_123",
            "email": "alpha@lpu.in"
        }
        reg_resp = self.client.post("/api/auth/register", json=reg_payload)
        self.assertEqual(reg_resp.status_code, 201)
        self.assertIn("Student account successfully created", reg_resp.json()["message"])

        # 2. Login successfully
        login_payload = {
            "username": "student_alpha",
            "password": "alpha_password_123"
        }
        login_resp = self.client.post("/api/auth/login", json=login_payload)
        self.assertEqual(login_resp.status_code, 200)
        token_data = login_resp.json()
        self.assertIn("access_token", token_data)
        self.assertEqual(token_data["token_type"], "bearer")
        self.assertEqual(token_data["username"], "student_alpha")
        print(" -> Success: Valid JWT token successfully generated upon correct login credentials.")

        # 3. Login failure with wrong password
        bad_login = {
            "username": "student_alpha",
            "password": "wrong_password"
        }
        bad_resp = self.client.post("/api/auth/login", json=bad_login)
        self.assertEqual(bad_resp.status_code, 401)
        self.assertIn("Invalid username or password credentials", bad_resp.json()["detail"])
        print(" -> Success: Login rejected with 401 Unauthorized for incorrect credentials.")

    def test_database_retrieval(self):
        print("\n[TEST] Validating Lecture Summary Database Retrieval...")
        # 1. Register and login student
        self.client.post("/api/auth/register", json={"username": "student_beta", "password": "beta_password", "email": "beta@lpu.in"})
        login_res = self.client.post("/api/auth/login", json={"username": "student_beta", "password": "beta_password"}).json()
        token = login_res["access_token"]

        # 2. Persist mock summaries under student_beta
        summary_1 = {
            "summary": "Introduction to CPU scheduling.",
            "topics": ["CPU Scheduling", "SJF"],
            "key_points": ["FCFS is simple", "SJF is optimal"]
        }
        summary_2 = {
            "summary": "Deep dive into React Hooks.",
            "topics": ["React", "useEffect"],
            "key_points": ["Clean up effects", "Avoid loops"]
        }
        save_summary_to_db("student_beta", summary_1)
        save_summary_to_db("student_beta", summary_2)

        # 3. Retrieve summaries via GET endpoint
        headers = {"Authorization": f"Bearer {token}"}
        get_res = self.client.get("/api/summaries", headers=headers)
        self.assertEqual(get_res.status_code, 200)
        summaries = get_res.json()
        self.assertEqual(len(summaries), 2)
        
        # Verify retrieved data fields
        self.assertEqual(summaries[0]["topics"], ["React", "useEffect"])
        self.assertEqual(summaries[1]["topics"], ["CPU Scheduling", "SJF"])
        print(" -> Success: Database records retrieved and mapped correctly from tables.")

    def test_summary_ownership_isolation(self):
        print("\n[TEST] Validating Multi-User Summary Isolation & 403 Restrictions...")
        # 1. Register student X and student Y
        self.client.post("/api/auth/register", json={"username": "student_x", "password": "password_x", "email": "x@lpu.in"})
        self.client.post("/api/auth/register", json={"username": "student_y", "password": "password_y", "email": "y@lpu.in"})

        # Log in
        token_x = self.client.post("/api/auth/login", json={"username": "student_x", "password": "password_x"}).json()["access_token"]
        token_y = self.client.post("/api/auth/login", json={"username": "student_y", "password": "password_y"}).json()["access_token"]

        # 2. Save a summary for student_x
        x_summary = {
            "summary": "This is highly sensitive and restricted data.",
            "topics": ["Security", "Cryptography"],
            "key_points": ["Encrypt at rest", "Salt hashes"]
        }
        save_summary_to_db("student_x", x_summary)

        # 3. Retrieve summaries as student_x -> Should see 1 summary
        headers_x = {"Authorization": f"Bearer {token_x}"}
        res_x = self.client.get("/api/summaries", headers=headers_x)
        self.assertEqual(res_x.status_code, 200)
        summaries_x = res_x.json()
        self.assertEqual(len(summaries_x), 1)
        summary_id = summaries_x[0]["id"]

        # 4. Retrieve summaries as student_y -> Should see 0 summaries (strict separation)
        headers_y = {"Authorization": f"Bearer {token_y}"}
        res_y = self.client.get("/api/summaries", headers=headers_y)
        self.assertEqual(res_y.status_code, 200)
        self.assertEqual(len(res_y.json()), 0)
        print(" -> Success: Multi-user separation validated. Student Y cannot see Student X's feed.")

        # 5. Access specific summary detail directly as student_x (Allowed)
        detail_x = self.client.get(f"/api/summaries/{summary_id}", headers=headers_x)
        self.assertEqual(detail_x.status_code, 200)
        self.assertEqual(detail_x.json()["summary"], "This is highly sensitive and restricted data.")

        # 6. Access specific summary detail directly as student_y (Forbidden 403)
        detail_y = self.client.get(f"/api/summaries/{summary_id}", headers=headers_y)
        self.assertEqual(detail_y.status_code, 403)
        self.assertIn("Permission denied", detail_y.json()["detail"])
        print(" -> Success: 403 Forbidden properly returned when accessing unauthorized summary.")

def verify_seeded_database():
    print("\n" + "="*60)
    print("=== INTEGRATION CHECK: Seeded Database (logs/cloudnote.db) ===")
    print("="*60)
    
    # 1. Verify existence of the seeded database file
    PROD_DB_PATH = "logs/cloudnote.db"
    if not os.path.exists(PROD_DB_PATH):
        print(f"ERROR: Seeded database file not found at '{PROD_DB_PATH}'. Please run scratch/seed_summary.py first!")
        sys.exit(1)
        
    # Re-point database configurations to production/seeded DB file
    app.database.DB_PATH = PROD_DB_PATH
    
    client = TestClient(fastapi_app)
    
    # 2. Authenticate the seeded student
    print("Integration: Logging in with seeded credentials...")
    login_payload = {
        "username": "dashboard_test_student",
        "password": "password123"
    }
    
    try:
        response = client.post("/api/auth/login", json=login_payload)
        if response.status_code != 200:
            print(f"FAILED: Could not log in with seeded student. Status code: {response.status_code}")
            print(f"Detail: {response.text}")
            sys.exit(1)
            
        token_data = response.json()
        token = token_data["access_token"]
        print(" -> Success: Logged in. JWT token obtained successfully.")
        
        # 3. Retrieve seeded summaries
        print("Integration: Fetching seeded summaries from database...")
        headers = {"Authorization": f"Bearer {token}"}
        res_summaries = client.get("/api/summaries", headers=headers)
        
        if res_summaries.status_code != 200:
            print(f"FAILED: Could not retrieve summaries. Status code: {res_summaries.status_code}")
            sys.exit(1)
            
        summaries = res_summaries.json()
        print(f" -> Success: Fetched {len(summaries)} summaries from seeded database.")
        
        # Verify count is exactly 3
        if len(summaries) != 3:
            print(f"FAILED: Expected exactly 3 summaries, found {len(summaries)}.")
            sys.exit(1)
            
        for i, s in enumerate(summaries, 1):
            print(f"   [{i}] Subject: '{s['subject']}'")
            print(f"       Topics: {s['topics']}")
            print(f"       Abstract: {s['summary'][:80]}...")
            
        print("\nIntegration Check: All validation checks PASSED successfully!")
        
    except Exception as err:
        import traceback
        print(f"FAILED: Integration check encountered unexpected error: {err}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    # Run the isolated unit tests first
    print("=================== RUNNING OFFLINE UNIT TESTS ===================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestDashboardFlow)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    if not result.wasSuccessful():
        print("\nFAILED: Unit tests did not pass. Skipping actual database integration check.")
        sys.exit(1)
        
    # Then verify the seeded database
    verify_seeded_database()
