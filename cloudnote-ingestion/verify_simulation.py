import os
import sys
import json
import asyncio
import glob
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

def clean_old_artifacts():
    print("CLEANING up old simulation artifacts...")
    # Clean logs
    for f in ["logs/session_status.json", "logs/ingestion_status.json"]:
        if os.path.exists(f):
            try:
                os.remove(f)
                print(f"Removed {f}")
            except Exception as e:
                print(f"Failed to remove {f}: {e}")
    # Clean debug screenshots
    for f in (glob.glob("screenshots/join_success_*.png") + 
              glob.glob("screenshots/join_failed_*.png") + 
              glob.glob("screenshots/joining_failure.png") +
              glob.glob("screenshots/connected_*.png") +
              glob.glob("screenshots/failure_*.png") +
              glob.glob("screenshots/disconnect_*.png") +
              glob.glob("screenshots/before_join_*.png") +
              glob.glob("screenshots/login_start_*.png") +
              glob.glob("screenshots/login_success_*.png")):
        try:
            os.remove(f)
            print(f"Removed {f}")
        except Exception as e:
            print(f"Failed to remove {f}: {e}")

async def run_simulation(mode: str):
    print(f"\nSTARTING SIMULATION: {mode} MODE")
    clean_old_artifacts()
    
    # Configure env vars
    os.environ["DEBUG_MODE"] = "True"
    os.environ["DEBUG_SIMULATION_MODE"] = mode
    
    # Reload settings/config manually to apply updated env vars
    from app.config import settings
    settings.DEBUG_MODE = True
    settings.DEBUG_SIMULATION_MODE = mode
    settings.HEADLESS = True # force headless for clean automated local test run
    
    # Run the worker pipeline
    from app.main import run_ingestion
    try:
        await run_ingestion()
    except Exception as e:
        print(f"Worker ended with exception (expected in FAILURE mode): {e}")
        
    print("Checking simulation assertions...")
    
    # 1. Check ingestion_status.json
    status_file = "logs/ingestion_status.json"
    assert os.path.exists(status_file), f"Ingestion status file {status_file} not found!"
    with open(status_file, "r", encoding="utf-8") as f:
        ing_status = json.load(f)
    print(f"Ingestion Status: {json.dumps(ing_status, indent=2)}")
    
    # 2. Check session_status.json
    session_file = "logs/session_status.json"
    assert os.path.exists(session_file), f"Session status file {session_file} not found!"
    with open(session_file, "r", encoding="utf-8") as f:
        sess_status = json.load(f)
    print(f"Session Status: {json.dumps(sess_status, indent=2)}")
    
    # 3. Assertions based on mode
    if mode == "SUCCESS":
        assert sess_status["status"] in ("CONNECTED", "DISCONNECTED"), f"Status should be CONNECTED or DISCONNECTED, got {sess_status['status']}"
        assert sess_status["latest_screenshot"] is not None, "Success latest_screenshot was not saved!"
        assert ("screenshots/join_success" in sess_status["latest_screenshot"] or 
                "screenshots/connected" in sess_status["latest_screenshot"] or 
                "screenshots/disconnect" in sess_status["latest_screenshot"]), f"Unexpected success screenshot path: {sess_status['latest_screenshot']}"
        assert sess_status["latest_event"] in ("JOIN_SUCCESS", "DISCONNECTED", "CONNECTED"), f"Event should be JOIN_SUCCESS, DISCONNECTED or CONNECTED, got {sess_status['latest_event']}"
        success_pngs = (glob.glob("screenshots/join_success_*.png") + 
                        glob.glob("screenshots/disconnect_*.png") + 
                        glob.glob("screenshots/connected_*.png"))
        assert len(success_pngs) > 0, "No success/disconnect/connected screenshots found!"
        print(f"SUCCESS Mode validated! Generated latest_screenshot: {sess_status['latest_screenshot']}")
        
    elif mode == "FAILURE":
        assert sess_status["status"] == "FAILED", f"Status should be FAILED, got {sess_status['status']}"
        assert sess_status["latest_screenshot"] is not None, "Failure latest_screenshot was not saved!"
        assert ("screenshots/join_failed" in sess_status["latest_screenshot"] or 
                "screenshots/failure" in sess_status["latest_screenshot"]), f"Unexpected failure screenshot path: {sess_status['latest_screenshot']}"
        assert sess_status["latest_event"] in ("JOIN_FAILED", "FAILED"), f"Event should be JOIN_FAILED or FAILED, got {sess_status['latest_event']}"
        fail_pngs = glob.glob("screenshots/join_failed_*.png") + glob.glob("screenshots/failure_*.png")
        assert len(fail_pngs) > 0, "No failure/join_failed screenshots found!"
        print(f"FAILURE Mode validated! Generated latest_screenshot: {sess_status['latest_screenshot']}")

async def main():
    # Make sure screenshots folder exists
    os.makedirs("screenshots", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    
    # Run SUCCESS simulation
    try:
        await run_simulation("SUCCESS")
    except Exception as e:
        print(f"SUCCESS mode assertion failed: {e}")
        sys.exit(1)
        
    # Run FAILURE simulation
    try:
        await run_simulation("FAILURE")
    except Exception as e:
        print(f"FAILURE mode assertion failed: {e}")
        sys.exit(1)
        
    print("\nALL LOCAL SIMULATION VALIDATION TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(main())
