"""
Step 3D Test Suite — Process Separation Verification
Validates the complete decoupling of the Autonomous Timetable Scheduler and
the Ingestion WorkerConsumer into separate entrypoints and processes.

Tests:
1. Scheduler module isolation from WorkerConsumer (AST + symbol inspection)
2. Worker module isolation from scheduler and timetable fetching logic (AST + symbol inspection)
3. Scheduler successfully enqueues a class job to Redis Stream
4. Worker independently consumes a queued job and ACKs it
5. process_class receives the correct user context and decrypted credentials
6. Strict security verification: zero credentials enter Redis stream
7. Scheduler and worker are configured as separate Docker processes in docker-compose.yml
8. Scheduler container runs only python -m app.scheduler
9. Worker container runs only python -m app.worker
10. Graceful worker shutdown behavior (signal handling and consumer stop)
11. Existing direct-processing function (process_class_direct) remains available in app.main
    and is strictly isolated from app.scheduler
"""

import ast
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import (
    init_db,
    get_db_connection,
    set_user_portal_credentials,
    get_user_portal_credentials,
    get_or_create_ingestion_user,
)
from app.job_queue import (
    IngestionJob,
    enqueue_job,
    get_pending_jobs,
    get_redis_client,
    ensure_consumer_group,
    generate_class_idempotency_key,
    schedule_class_to_queue,
    DEFAULT_STREAM_NAME,
    DEFAULT_CONSUMER_GROUP,
)
from app.worker_consumer import WorkerConsumer
from app.scheduler import StandaloneSchedulerService
from app.worker import StandaloneWorkerService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3DTests")


def test_scheduler_module_isolation():
    """Verify that app.scheduler has zero imports or dependencies on WorkerConsumer."""
    scheduler_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "scheduler.py"
    )
    assert os.path.exists(scheduler_path), f"File not found: {scheduler_path}"

    with open(scheduler_path, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source, filename=scheduler_path)

    imported_names = set()
    used_identifiers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                imported_names.add(f"{mod}.{alias.name}")
                imported_names.add(alias.name)
        elif isinstance(node, ast.Name):
            used_identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            used_identifiers.add(node.attr)

    # Assert WorkerConsumer is nowhere in imports or code identifiers
    assert "WorkerConsumer" not in imported_names, (
        "FAIL: app.scheduler must NEVER import WorkerConsumer!"
    )
    assert "worker_consumer" not in imported_names, (
        "FAIL: app.scheduler must NEVER import worker_consumer!"
    )
    assert "worker" not in imported_names, (
        "FAIL: app.scheduler must NEVER import worker!"
    )
    assert "WorkerConsumer" not in used_identifiers, (
        "FAIL: app.scheduler must NEVER reference or instantiate WorkerConsumer!"
    )
    assert "process_class_direct" not in imported_names, (
        "FAIL: app.scheduler must not import process_class_direct!"
    )
    assert "process_class_direct" not in used_identifiers, (
        "FAIL: app.scheduler must not invoke process_class_direct!"
    )

    logger.info("PASS [1/11]: Scheduler module is strictly isolated from WorkerConsumer and direct browser joining.")


def test_worker_module_isolation():
    """Verify that app.worker has zero imports or dependencies on timetable scheduling logic."""
    worker_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "worker.py"
    )
    assert os.path.exists(worker_path), f"File not found: {worker_path}"

    with open(worker_path, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source, filename=worker_path)

    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                imported_names.add(f"{mod}.{alias.name}")
                imported_names.add(alias.name)

    forbidden_worker_imports = [
        "timetable_cache",
        "timetable_fetcher",
        "fetch_timetable_data",
        "perform_login",
        "run_scheduler",
        "StandaloneSchedulerService",
        "scheduler",
    ]
    for forbidden in forbidden_worker_imports:
        assert forbidden not in imported_names, (
            f"FAIL: app.worker must NEVER import {forbidden}!"
        )

    logger.info("PASS [2/11]: Worker module is strictly isolated from timetable scheduling/fetching logic.")


def get_test_redis_client():
    """Returns a connected Redis client, automatically handling host vs container DNS."""
    try:
        r = get_redis_client()
        r.ping()
        return r
    except Exception:
        r = get_redis_client(redis_url="redis://localhost:6379/0")
        assert r.ping(), "Redis connection failed on both settings.REDIS_URL and localhost:6379!"
        return r


async def test_end_to_end_process_separation():
    """
    Simulate the full decoupled flow across the Redis boundary:
    1. Scheduler enqueues active class to Redis Stream
    2. Redis Stream contains zero credentials
    3. Standalone worker consumer reads and processes the job
    4. process_class receives decrypted user credentials
    5. Job is successfully ACKed
    """
    r = get_test_redis_client()
    assert r.ping(), "Redis connection failed!"
    init_db()

    run_id = uuid.uuid4().hex[:8]
    test_stream = f"cloudnote:test:stream:3d_{run_id}"
    test_group = f"cloudnote:test:group:3d_{run_id}"
    test_consumer = f"worker-3d-{run_id}"
    ensure_consumer_group(client=r, stream_name=test_stream, group_name=test_group)

    # 1. Setup user and portal credentials in SQLite
    user_id = int(f"98{int(run_id[:4], 16) % 80 + 10}")
    portal_user = f"std_3d_{run_id}"
    portal_pwd = f"SecretPass_3d!{run_id}"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username) VALUES (?, ?, ?, ?, ?)",
        (user_id, f"test_user_{user_id}", "mock_hash_xyz", f"test_{user_id}@test.com", portal_user),
    )
    conn.commit()
    conn.close()

    set_user_portal_credentials(user_id=user_id, lpu_username=portal_user, plain_password=portal_pwd)

    # 2. Scheduler Enqueues a Job
    class_meta = {
        "subject_code": f"CSE499_{run_id}",
        "subject_name": "Decoupled Distributed Systems",
        "faculty": "Prof. Separation",
        "timings": "11:00 AM - 12:00 PM",
        "start_time": "2026-09-17 11:00:00",
        "end_time": "2026-09-17 12:00:00",
        "join_url": f"https://myclass.lpu.in/join/{run_id}",
    }

    job, enqueued = schedule_class_to_queue(class_meta, user_id=user_id, client=r, stream_name=test_stream)
    assert enqueued is True, "Scheduler must successfully enqueue new class job!"
    assert job is not None, "Scheduler must return valid IngestionJob!"
    assert job.user_id == user_id
    assert job.class_metadata["subject_code"] == f"CSE499_{run_id}"
    logger.info("PASS [3/11]: Scheduler successfully enqueues class job to Redis Stream.")

    # 3. Security Verification: Inspect raw Redis stream payload
    raw_entries = r.xrange(test_stream)
    assert len(raw_entries) == 1, "Redis stream must contain exactly 1 entry!"
    entry_id, entry_fields = raw_entries[0]
    raw_payload_field = entry_fields.get("payload") or entry_fields.get(b"payload")
    payload_str = raw_payload_field.decode("utf-8") if isinstance(raw_payload_field, bytes) else raw_payload_field
    raw_payload = json.loads(payload_str)

    forbidden_keys = {"password", "lpu_password", "credentials", "secret", "token"}
    for f_key in forbidden_keys:
        assert f_key not in raw_payload, f"SECURITY VIOLATION: Forbidden key '{f_key}' in Redis payload!"
        assert f_key not in payload_str.lower(), f"SECURITY VIOLATION: '{f_key}' found in serialized payload!"
    assert portal_pwd not in payload_str, "SECURITY VIOLATION: Plaintext password found in Redis payload!"

    logger.info("PASS [4/11]: Zero credentials enter Redis Stream payload.")

    # 4. Standalone Worker Consumes the Queued Job
    received_contexts = []

    async def mock_process_class(user_id, lpu_username, lpu_password, class_metadata):
        received_contexts.append({
            "user_id": user_id,
            "username": lpu_username,
            "password": lpu_password,
            "metadata": class_metadata,
        })
        return {"status": "COMPLETED", "details": "Processed by independent worker"}

    consumer = WorkerConsumer(
        consumer_name=test_consumer,
        stream_name=test_stream,
        group_name=test_group,
        redis_client=r,
    )

    # Process pending stream messages
    processed = await consumer.poll_and_process(
        count=1,
        block_ms=2000,
        process_class_fn=mock_process_class,
        check_stale=False,
    )
    assert processed == 1, "Worker must process exactly 1 job from Redis stream!"
    logger.info("PASS [5/11]: Worker independently consumes queued job from Redis Stream.")

    # 5. Verify process_class receives the exact user context and decrypted credentials
    assert len(received_contexts) == 1, "mock_process_class must have been called exactly once!"
    ctx = received_contexts[0]
    assert ctx["user_id"] == user_id, f"Expected user_id {user_id}, got {ctx['user_id']}"
    assert ctx["username"] == portal_user, f"Expected username {portal_user}, got {ctx['username']}"
    assert ctx["password"] == portal_pwd, "Worker must supply decrypted in-memory password!"
    assert ctx["metadata"]["subject_code"] == f"CSE499_{run_id}"
    logger.info("PASS [6/11]: process_class receives verified user context and decrypted credentials.")

    # 6. Verify Job is ACKed (no pending jobs in PEL)
    pending = get_pending_jobs(client=r, stream_name=test_stream, group_name=test_group)
    assert len(pending) == 0, "Job must be fully ACKed by worker consumer!"

    # Cleanup test stream
    r.delete(test_stream)
    r.delete(f"cloudnote:job:idempotency:{job.idempotency_key}")


def test_docker_compose_process_separation():
    """Verify docker-compose.yml defines separate scheduler and worker processes."""
    compose_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker-compose.yml"
    )
    assert os.path.exists(compose_path), f"File not found: {compose_path}"

    # Use docker compose config --format json to parse the resolved compose file
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=os.path.dirname(compose_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"docker compose config failed: {result.stderr}"

    config = json.loads(result.stdout)
    services = config.get("services", {})

    # Check 1: Separate services exist
    assert "scheduler" in services, "FAIL: 'scheduler' service missing in docker-compose.yml!"
    assert "worker" in services, "FAIL: 'worker' service missing in docker-compose.yml!"
    assert "ingestion-worker" not in services, (
        "FAIL: Legacy monolithic 'ingestion-worker' service should be replaced by 'scheduler' and 'worker'!"
    )
    logger.info("PASS [7/11]: docker-compose.yml defines separate 'scheduler' and 'worker' services.")

    # Check 2: Scheduler container configuration
    sched_service = services["scheduler"]
    assert sched_service.get("container_name") == "cloudnote-scheduler", (
        f"Expected container_name 'cloudnote-scheduler', got '{sched_service.get('container_name')}'"
    )
    sched_cmd = sched_service.get("command")
    assert sched_cmd == ["python", "-m", "app.scheduler"], (
        f"Expected scheduler command ['python', '-m', 'app.scheduler'], got {sched_cmd}"
    )
    logger.info("PASS [8/11]: Scheduler container runs exclusively 'python -m app.scheduler'.")

    # Check 3: Worker container configuration
    worker_service = services["worker"]
    assert worker_service.get("container_name") == "cloudnote-worker", (
        f"Expected container_name 'cloudnote-worker', got '{worker_service.get('container_name')}'"
    )
    worker_cmd = worker_service.get("command")
    assert worker_cmd == ["python", "-m", "app.worker"], (
        f"Expected worker command ['python', '-m', 'app.worker'], got {worker_cmd}"
    )
    logger.info("PASS [9/11]: Worker container runs exclusively 'python -m app.worker'.")


def test_worker_service_graceful_shutdown():
    """Verify that StandaloneWorkerService handles termination signals and stops the consumer cleanly."""
    service = StandaloneWorkerService(consumer_name="test-shutdown-worker")
    mock_consumer = MagicMock()
    service.consumer = mock_consumer

    assert service._shutdown_event.is_set() is False

    # Trigger signal handler
    service.handle_signal(signal.SIGTERM)

    assert service._shutdown_event.is_set() is True
    mock_consumer.stop.assert_called_once()
    logger.info("PASS [10/11]: StandaloneWorkerService implements clean and graceful signal shutdown.")


def test_direct_processing_fallback_preservation():
    """Verify that process_class_direct remains available in app.main for legacy direct execution."""
    from app.main import process_class_direct, run_ingestion
    assert callable(process_class_direct), "process_class_direct must be a callable fallback function"

    # Verify that app.main:run_ingestion delegates to app.scheduler:run_scheduler
    import inspect
    run_ingestion_src = inspect.getsource(run_ingestion)
    assert "run_scheduler" in run_ingestion_src, (
        "app.main:run_ingestion must delegate to app.scheduler:run_scheduler for backward compatibility"
    )
    assert "WorkerConsumer" not in run_ingestion_src, (
        "app.main:run_ingestion must NOT recreate co-located WorkerConsumer!"
    )

    logger.info("PASS [11/11]: process_class_direct remains available and run_ingestion delegates to run_scheduler.")


async def main():
    logger.info("=================================================================")
    logger.info("=== STARTING STEP 3D: PROCESS SEPARATION VERIFICATION SUITE ===")
    logger.info("=================================================================")

    test_scheduler_module_isolation()
    test_worker_module_isolation()
    await test_end_to_end_process_separation()
    test_docker_compose_process_separation()
    test_worker_service_graceful_shutdown()
    test_direct_processing_fallback_preservation()

    logger.info("=================================================================")
    logger.info("=== ALL 11 STEP 3D PROCESS SEPARATION TESTS PASSED CLEANLY! ===")
    logger.info("=================================================================")


if __name__ == "__main__":
    asyncio.run(main())
