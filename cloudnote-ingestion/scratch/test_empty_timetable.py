import sys
import os

# Set search path to workspace root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.timetable_cache import timetable_cache

def run_test():
    print("=== STARTING EMPTY TIMETABLE SCHEDULER TEST ===")
    
    # 1. Simulate a successful headless fetch on a day with 0 classes (e.g. Sunday)
    print("\n--- STEP 1: Persist Empty Class List ---")
    timetable_cache.set_timetable([])
    
    # 2. Check if cache date validity holds true
    print("\n--- STEP 2: Assert Cache Validity ---")
    cache_valid = timetable_cache.is_valid_for_today()
    print(f"Is cache valid for today? {cache_valid} (Expected: True)")
    assert cache_valid is True
    
    # 3. Assert that get_timetable() returns [] but keeps today's fetch date
    classes = timetable_cache.get_timetable()
    print(f"Classes list fetched: {classes} (Expected: [])")
    assert classes == []
    
    # 4. Check if the sync orchestrator condition `not cache_valid or sync_requested` is False
    sync_requested = timetable_cache.is_sync_requested()
    sync_orchestrator_should_run = not cache_valid or sync_requested
    print(f"Sync Orchestrator should run? {sync_orchestrator_should_run} (Expected: False)")
    assert sync_orchestrator_should_run is False
    
    # 5. Check if the sleep loop sleep interruption condition `is_sync_requested or not is_valid_for_today` is False
    sleep_should_interrupt = sync_requested or not cache_valid
    print(f"Smart Sleep should interrupt? {sleep_should_interrupt} (Expected: False)")
    assert sleep_should_interrupt is False
    
    # 6. Simulate next day date rollover
    print("\n--- STEP 3: Simulate Date Rollover ---")
    # Forcibly mock the last fetch date to yesterday
    timetable_cache.last_fetch_date = "2026-05-23"
    timetable_cache.save_to_disk()
    
    # Re-evaluate validity
    cache_valid_after_rollover = timetable_cache.is_valid_for_today()
    print(f"Is cache valid after date rollover? {cache_valid_after_rollover} (Expected: False)")
    assert cache_valid_after_rollover is False
    
    # Re-evaluate sync orchestrator condition
    sync_orchestrator_should_run_after_rollover = not cache_valid_after_rollover or sync_requested
    print(f"Sync Orchestrator should run after rollover? {sync_orchestrator_should_run_after_rollover} (Expected: True)")
    assert sync_orchestrator_should_run_after_rollover is True
    
    # Re-evaluate sleep interruption condition
    sleep_should_interrupt_after_rollover = sync_requested or not cache_valid_after_rollover
    print(f"Smart Sleep should interrupt after rollover? {sleep_should_interrupt_after_rollover} (Expected: True)")
    assert sleep_should_interrupt_after_rollover is True
    
    print("\n=== SUCCESS: ALL EMPTY TIMETABLE SCHEDULER ASSERTIONS PASSED! ===")

if __name__ == "__main__":
    run_test()
