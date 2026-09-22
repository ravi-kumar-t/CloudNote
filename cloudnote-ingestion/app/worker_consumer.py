"""
CloudNote Ingestion Worker Consumer
-----------------------------------
Redis Streams consumer integration for multi-user class ingestion jobs.
Retrieves jobs from Redis Streams, resolves user credentials securely in-memory
from SQLite, enforces concurrency guards, dispatches to process_class,
and manages job acknowledgment and stale pending entry reclamation.
"""

import asyncio
from datetime import datetime, timezone
import logging
import os
import re
import socket
from typing import Any, Callable, Optional
import uuid
import redis

from .config import settings
from .database import get_user_portal_credentials
from .job_queue import (
    IngestionJob,
    consume_jobs,
    acknowledge_job,
    claim_pending_jobs,
    acquire_execution_lock,
    release_execution_lock,
    get_execution_lock_key,
    ExecutionLock,
    get_redis_client,
    DEFAULT_STREAM_NAME,
    DEFAULT_CONSUMER_GROUP,
    DEFAULT_DLQ_STREAM_NAME,
    route_to_dead_letter_queue,
)
from .logger import logger

# Sensible default timeout for reclaiming jobs abandoned by dead workers
DEFAULT_PENDING_JOB_IDLE_TIMEOUT_MS = 300000  # 5 minutes


def generate_consumer_name(prefix: str = "worker") -> str:
    """
    Generates a globally unique consumer identifier combining hostname, PID, and random UUID.
    Guarantees distinction between Pods, containers, and local processes.
    """
    hostname = socket.gethostname() or "node"
    pid = os.getpid()
    uid = uuid.uuid4().hex[:6]
    return f"{prefix}-{hostname}-{pid}-{uid}"


def sanitize_log_message(message: Any) -> str:
    """
    Sanitizes string inputs to ensure credentials, tokens, or secrets
    are never leaked into log sinks or error traces.
    """
    msg_str = str(message)
    # Redact common credential patterns
    cleaned = re.sub(
        r"(password|token|secret|jwt|auth_token)\s*[:=]\s*['\"]?[^'\s,}\]]+['\"]?",
        r"\1='[REDACTED]'",
        msg_str,
        flags=re.IGNORECASE,
    )
    return cleaned


class WorkerConsumer:
    """
    Worker consumer instance connected to Redis Streams consumer group.
    Operates with strict in-memory credential isolation and explicit failure policies.
    """

    def __init__(
        self,
        consumer_name: Optional[str] = None,
        stream_name: str = DEFAULT_STREAM_NAME,
        group_name: str = DEFAULT_CONSUMER_GROUP,
        dlq_stream_name: str = DEFAULT_DLQ_STREAM_NAME,
        redis_client: Optional[redis.Redis] = None,
        idle_timeout_ms: int = DEFAULT_PENDING_JOB_IDLE_TIMEOUT_MS,
        max_concurrent_jobs: Optional[int] = None,
    ):
        self.consumer_name = consumer_name or generate_consumer_name()
        self.stream_name = stream_name
        self.group_name = group_name
        self.dlq_stream_name = dlq_stream_name
        self.redis_client = redis_client or get_redis_client()
        self.idle_timeout_ms = idle_timeout_ms
        self.max_concurrent_jobs = max_concurrent_jobs or getattr(settings, "WORKER_MAX_CONCURRENT_JOBS", 1)
        self.is_running = False
        self._stop_event = asyncio.Event()
        self._active_tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(self.max_concurrent_jobs)
        self._task_exceptions: list[Exception] = []
        logger.info(
            f"WorkerConsumer: Initialized instance '{self.consumer_name}' "
            f"for stream '{self.stream_name}', group '{self.group_name}' "
            f"(max_concurrent_jobs={self.max_concurrent_jobs}, dlq={self.dlq_stream_name})."
        )

    def _sync_timetable_status(
        self,
        user_id: int,
        class_metadata: dict[str, Any],
        new_status: str,
    ) -> bool:
        """
        Updates the matching class entry in SQLite user_timetables with the terminal status.
        Preserves the exact terminal status (COMPLETED, FACULTY_NOT_STARTED, FAILED, MISSED)
        without altering the schema or inventing new statuses.
        Includes bounded retry (3 attempts, 0.1s backoff) for transient SQLite 'database is locked'.
        """
        import time
        import sqlite3
        max_retries = 3
        backoff = 0.1

        for attempt in range(1, max_retries + 1):
            try:
                from .database import update_user_class_status
                subject_code = class_metadata.get("subject_code") or class_metadata.get("subject", "")
                timings = class_metadata.get("timings")

                date_str = class_metadata.get("date")
                if not date_str and class_metadata.get("start_time"):
                    start_val = str(class_metadata["start_time"])
                    if " " in start_val:
                        date_str = start_val.split(" ")[0]
                    elif "T" in start_val:
                        date_str = start_val.split("T")[0]
                if not date_str:
                    from .utils import get_now_ist
                    date_str = get_now_ist().strftime("%Y-%m-%d")

                updated = update_user_class_status(
                    user_id=user_id,
                    date_str=date_str,
                    subject_code=subject_code,
                    timings=timings,
                    new_status=new_status,
                )
                if not updated and timings:
                    # Fallback to match on subject_code without strict timings match
                    updated = update_user_class_status(
                        user_id=user_id,
                        date_str=date_str,
                        subject_code=subject_code,
                        timings=None,
                        new_status=new_status,
                    )

                if updated:
                    logger.info(
                        f"WorkerConsumer: Synchronized timetable status to '{new_status}' "
                        f"for user {user_id}, subject '{subject_code}', date '{date_str}'."
                    )
                else:
                    logger.debug(
                        f"WorkerConsumer: No matching timetable row found or terminal overwrite blocked "
                        f"for user {user_id}, subject '{subject_code}', date '{date_str}'."
                    )
                return updated
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < max_retries:
                    logger.warning(
                        f"WorkerConsumer: SQLite database locked (attempt {attempt}/{max_retries}) "
                        f"updating timetable for user {user_id}, retrying in {backoff}s..."
                    )
                    time.sleep(backoff)
                    continue
                logger.warning(
                    f"WorkerConsumer: Failed updating user_timetables for user {user_id}: {e}"
                )
                return False
            except Exception as e:
                logger.warning(
                    f"WorkerConsumer: Failed updating user_timetables for user {user_id}: {e}"
                )
                return False
        return False

    async def process_single_job(
        self,
        job: IngestionJob,
        process_class_fn: Optional[Callable] = None,
        is_reclaimed: bool = False,
        lock_ttl_seconds: int = 60,
        lock_heartbeat_interval: float = 15.0,
        drain_stale_jobs: bool = True,
    ) -> bool:
        """
        Processes an individual IngestionJob adhering strictly to failure & ACK policies:

        1. Concurrency Lock: Prevents simultaneous execution of the same class for the same user.
           Uses dynamic heartbeat keep-alive so lock cannot expire while worker is actively running.
           - If lock is held and is_reclaimed is False: True duplicate in stream -> ACK to drain redundant job.
           - If lock is held and is_reclaimed is True: Legitimate retry/recovery -> DO NOT ACK! Stays in PEL.
        2. Delivery Count & Poison Pill Quarantine (authoritative Redis PEL times_delivered):
           If job.delivery_count > QUEUE_MAX_DELIVERY_ATTEMPTS:
           - Quarantines job to DLQ (cloudnote:jobs:dlq).
           - Updates SQLite timetable status to FAILED.
           - Emits XACK to clear the message from the main stream PEL.
           - Does NOT execute process_class again.
        3. Stale Redis-Job Drain (now > end_dt + 5 minutes):
           - Drains stale job without launching browser/Playwright.
           - Updates timetable to MISSED if not already terminal.
           - Emits XACK to clear message from stream.
        4. SQLite Credential Lookup: Resolves user profile and decrypts portal password in memory.
        5. Terminal Failures (User not found, Missing credentials):
           Logged safely and ACKed to avoid infinite retries / poisoning; updates timetable to FAILED for missing creds.
        6. Application Outcomes (COMPLETED, FACULTY_NOT_STARTED, FAILED):
           State is recorded authoritatively in user_timetables and session status; job is ACKed.
        7. Unexpected Worker Exception:
           DO NOT ACK. Message remains in PEL for stale recovery until delivery attempts are exhausted.
        """
        subject = job.class_metadata.get("subject_code", "UnknownClass")
        user_id = job.user_id

        # 1. Execution Concurrency Guard with Heartbeat Keep-Alive
        lock_key = get_execution_lock_key(user_id, job.class_metadata)
        exec_lock = ExecutionLock(
            lock_key=lock_key,
            consumer_name=self.consumer_name,
            client=self.redis_client,
            ttl_seconds=lock_ttl_seconds,
            heartbeat_interval=lock_heartbeat_interval,
        )

        acquired = await exec_lock.acquire()
        if not acquired:
            logger.warning(
                f"WorkerConsumer: Concurrency guard blocked execution for user {user_id}, "
                f"subject '{subject}'. Another active consumer holds lock '{lock_key}'."
            )
            # Differentiate:
            # - True duplicate: New stream message arrived while active execution is in progress.
            #   Safe to ACK so redundant duplicate is drained from the queue.
            # - Legitimate retry / recovery: Message reclaimed from PEL while prior worker might still be alive.
            #   Must NOT ACK! Retain in PEL for subsequent recovery after lock expires.
            if not is_reclaimed and job.stream_message_id:
                try:
                    acknowledge_job(
                        message_id=job.stream_message_id,
                        client=self.redis_client,
                        stream_name=self.stream_name,
                        group_name=self.group_name,
                    )
                    logger.info(f"WorkerConsumer: ACKed redundant duplicate message {job.stream_message_id}.")
                except Exception as ack_err:
                    logger.debug(f"WorkerConsumer: Failed to ACK duplicate message: {ack_err}")
            return False

        try:
            # 2. Poison Pill Quarantine Check (authoritative delivery count based on Redis PEL)
            max_delivery_attempts = getattr(settings, "QUEUE_MAX_DELIVERY_ATTEMPTS", 3)
            if job.delivery_count > max_delivery_attempts:
                logger.warning(
                    f"WorkerConsumer: Job '{job.job_id}' exceeded max delivery attempts "
                    f"({job.delivery_count} > {max_delivery_attempts}). Quarantining to DLQ without executing."
                )
                route_to_dead_letter_queue(
                    job=job,
                    error_message=f"Exceeded maximum delivery attempts ({job.delivery_count} > {max_delivery_attempts})",
                    failed_by_consumer=self.consumer_name,
                    client=self.redis_client,
                    dlq_stream_name=self.dlq_stream_name,
                    main_stream_name=self.stream_name,
                    group_name=self.group_name,
                )
                self._sync_timetable_status(user_id, job.class_metadata, new_status="FAILED")
                try:
                    from .session_status import update_session_status
                    update_session_status(
                        status="FAILED",
                        subject=subject,
                        error=f"Quarantined to DLQ after exceeding max delivery attempts ({job.delivery_count} > {max_delivery_attempts}).",
                        user_id=user_id,
                    )
                except Exception:
                    pass
                return False

            # 3. Stale Redis-Job Drain Check (now > end_dt + 5 minutes)
            # If the class end time has already elapsed by more than 5 minutes:
            # - do NOT launch browser / Playwright
            # - update timetable to MISSED if not already terminal
            # - ACK the stale Redis message
            # - return without executing process_class
            if drain_stale_jobs:
                from .utils import get_now_ist
                from datetime import timedelta
                now = get_now_ist()
                end_str = job.class_metadata.get("end_time")
                is_stale = False
                if end_str:
                    try:
                        s = str(end_str).strip()
                        end_dt = None
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                            try:
                                end_dt = datetime.strptime(s, fmt)
                                break
                            except ValueError:
                                pass
                        if not end_dt and "T" in s:
                            end_dt = datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)

                        if end_dt and now > (end_dt + timedelta(minutes=5)):
                            is_stale = True
                    except Exception as parse_err:
                        logger.debug(f"WorkerConsumer: Could not parse end_time '{end_str}': {parse_err}")

                if is_stale:
                    logger.warning(
                        f"WorkerConsumer: Job '{job.job_id}' for user {user_id}, subject '{subject}' "
                        f"is stale (end_time {end_str} + 5m passed). Draining without launching browser."
                    )
                    self._sync_timetable_status(user_id, job.class_metadata, new_status="MISSED")
                    try:
                        from .session_status import update_session_status
                        update_session_status(
                            status="MISSED",
                            subject=subject,
                            error=f"Job drained as stale (end_time {end_str} + 5m passed).",
                            user_id=user_id,
                        )
                    except Exception:
                        pass
                    if job.stream_message_id:
                        acknowledge_job(
                            message_id=job.stream_message_id,
                            client=self.redis_client,
                            stream_name=self.stream_name,
                            group_name=self.group_name,
                        )
                    return False

            # 4. Retrieve user and decrypt credentials strictly in memory
            user_creds = get_user_portal_credentials(user_id)

            # Policy B: User does not exist in SQLite -> Terminal Failure -> ACK
            if not user_creds:
                logger.warning(
                    f"WorkerConsumer: Terminal failure — user_id {user_id} not found in database. "
                    f"Acknowledging invalid job {job.stream_message_id} to clear queue."
                )
                if job.stream_message_id:
                    acknowledge_job(
                        message_id=job.stream_message_id,
                        client=self.redis_client,
                        stream_name=self.stream_name,
                        group_name=self.group_name,
                    )
                return False

            portal_pw = user_creds.get("portal_password")
            lpu_username = user_creds.get("lpu_username") or user_creds.get("username")

            # Policy C: Missing or undecryptable credentials -> Terminal Failure -> ACK
            if not portal_pw:
                logger.warning(
                    f"WorkerConsumer: Terminal failure — missing or invalid portal credentials for user_id {user_id}. "
                    f"Acknowledging job {job.stream_message_id} to clear queue."
                )
                self._sync_timetable_status(user_id, job.class_metadata, new_status="FAILED")
                if job.stream_message_id:
                    acknowledge_job(
                        message_id=job.stream_message_id,
                        client=self.redis_client,
                        stream_name=self.stream_name,
                        group_name=self.group_name,
                    )
                return False

            # 5. Dispatch to targeted process_class
            logger.info(
                f"WorkerConsumer: Dispatched job '{job.job_id}' (stream_id: {job.stream_message_id}) "
                f"for user {user_id}, subject '{subject}' to process_class."
            )

            if process_class_fn is not None:
                fn = process_class_fn
            else:
                from .main import process_class
                fn = process_class

            # Execute processing with in-memory credentials only
            result = await fn(
                user_id=user_id,
                lpu_username=lpu_username,
                lpu_password=portal_pw,
                class_metadata=job.class_metadata,
            )

            # Immediate cleanup of plaintext reference in local scope
            del portal_pw

            # 5. Evaluate outcome and ACK
            # Policy A & D: Processed execution (COMPLETED, FACULTY_NOT_STARTED, FAILED)
            if isinstance(result, dict):
                status = result.get("status") or "COMPLETED"
            elif result is True:
                status = "COMPLETED"
            elif result is False:
                status = "FAILED"
            else:
                status = "COMPLETED"

            logger.info(
                f"WorkerConsumer: process_class returned '{status}' for job '{job.job_id}'. "
                f"Acknowledging stream message {job.stream_message_id}."
            )

            self._sync_timetable_status(user_id, job.class_metadata, new_status=status)

            if job.stream_message_id:
                acknowledge_job(
                    message_id=job.stream_message_id,
                    client=self.redis_client,
                    stream_name=self.stream_name,
                    group_name=self.group_name,
                )
            return True

        except Exception as exc:
            # Policy E: Unexpected worker exception -> DO NOT ACK!
            # Message remains in PEL so it can be reclaimed if worker crashes
            safe_error = sanitize_log_message(exc)
            logger.error(
                f"WorkerConsumer: Unexpected exception processing job '{job.job_id}' "
                f"for user {user_id}: {safe_error}. Job remains unacknowledged in PEL."
            )
            raise exc

        finally:
            # Always release execution lock (cancels heartbeat task and drops key)
            await exec_lock.release()

    async def _safe_job_task(
        self,
        job: IngestionJob,
        process_class_fn: Optional[Callable] = None,
        is_reclaimed: bool = False,
        drain_stale_jobs: bool = True,
    ) -> bool:
        """Executes process_single_job under the concurrency semaphore with exception tracking."""
        async with self._semaphore:
            try:
                return await self.process_single_job(
                    job=job,
                    process_class_fn=process_class_fn,
                    is_reclaimed=is_reclaimed,
                    drain_stale_jobs=drain_stale_jobs,
                )
            except asyncio.CancelledError:
                # Task was cancelled during shutdown; process_single_job's finally block
                # has already released the execution lock. Message remains in PEL unacknowledged.
                logger.info(
                    f"WorkerConsumer: Background task cancelled for job '{job.job_id}'. "
                    f"Message remains unacknowledged in PEL."
                )
                raise
            except Exception as exc:
                safe_err = sanitize_log_message(exc)
                logger.error(
                    f"WorkerConsumer: Background task error for job '{job.job_id}': {safe_err}"
                )
                self._task_exceptions.append(exc)
                return False

    async def _wait_for_active_tasks(self, timeout: float = 30.0):
        """
        Awaits running tasks during graceful shutdown.
        If any tasks remain running after the timeout:
        - Cancel remaining tasks so they exit cleanly.
        - DO NOT emit XACK for cancelled/unfinished jobs.
        - Rely on process_single_job's finally block to release the execution lock
          so another worker can reclaim the job via XAUTOCLAIM once idle.
        - Preserve PEL retention for unacknowledged jobs.
        """
        self._active_tasks = {t for t in self._active_tasks if not t.done()}
        if not self._active_tasks:
            return

        logger.info(
            f"WorkerConsumer: Waiting up to {timeout}s for {len(self._active_tasks)} active job(s) to complete..."
        )
        done, pending = await asyncio.wait(self._active_tasks, timeout=timeout)
        if pending:
            logger.warning(
                f"WorkerConsumer: {len(pending)} active job(s) did not complete within {timeout}s. "
                f"Aborting jobs without ACK so they remain in PEL for stale recovery."
            )
            for t in pending:
                t.cancel()
            # Allow cancellation to propagate so finally blocks (which release execution locks) run
            await asyncio.gather(*pending, return_exceptions=True)

    async def wait_for_active_tasks(self, timeout: float = 30.0):
        """Public interface to await in-flight background job tasks during graceful shutdown."""
        return await self._wait_for_active_tasks(timeout=timeout)

    async def poll_and_process(
        self,
        count: int = 1,
        block_ms: int = 2000,
        process_class_fn: Optional[Callable] = None,
        check_stale: bool = True,
        async_dispatch: Optional[bool] = None,
    ) -> int:
        """
        Single polling iteration:
        1. Checks capacity: does not poll if all concurrent slots are occupied.
        2. Checks for and reclaims stale pending jobs via XAUTOCLAIM.
        3. Consumes new jobs from consumer group via XREADGROUP.
        4. Processes each retrieved job (synchronously or dispatched as background task).
        Returns total jobs processed or dispatched.
        """
        use_async = (self.max_concurrent_jobs > 1) if async_dispatch is None else async_dispatch

        # Clean up any finished tasks and check capacity
        self._active_tasks = {t for t in self._active_tasks if not t.done()}
        available_slots = self.max_concurrent_jobs - len(self._active_tasks)
        if available_slots <= 0:
            # All slots are currently occupied; do not accumulate additional jobs into PEL
            return 0

        # Request at most available_slots to prevent over-accumulating into PEL
        effective_count = min(count, available_slots)
        processed_count = 0

        # Step 1: Stale pending job recovery
        if check_stale:
            try:
                stale_jobs = claim_pending_jobs(
                    consumer_name=self.consumer_name,
                    min_idle_time_ms=self.idle_timeout_ms,
                    count=effective_count,
                    client=self.redis_client,
                    stream_name=self.stream_name,
                    group_name=self.group_name,
                )
                for stale_job in stale_jobs:
                    logger.info(
                        f"WorkerConsumer: Reclaimed stale pending job '{stale_job.job_id}' "
                        f"(stream_id: {stale_job.stream_message_id}). Processing..."
                    )
                    if use_async:
                        task = asyncio.create_task(
                            self._safe_job_task(
                                stale_job,
                                process_class_fn=process_class_fn,
                                is_reclaimed=True,
                                drain_stale_jobs=check_stale,
                            )
                        )
                        self._active_tasks.add(task)
                        task.add_done_callback(self._active_tasks.discard)
                        processed_count += 1
                    else:
                        try:
                            ok = await self.process_single_job(
                                stale_job,
                                process_class_fn=process_class_fn,
                                is_reclaimed=True,
                                drain_stale_jobs=check_stale,
                            )
                            if ok:
                                processed_count += 1
                        except Exception:
                            pass
            except Exception as claim_err:
                logger.debug(f"WorkerConsumer: Error during stale job recovery: {claim_err}")

        # Update remaining available slots after stale reclaims
        if use_async:
            available_slots = self.max_concurrent_jobs - len(self._active_tasks)
            if available_slots <= 0:
                return processed_count
            effective_count = min(count, available_slots)

        # Step 2: Read new jobs
        try:
            new_jobs = consume_jobs(
                consumer_name=self.consumer_name,
                count=effective_count,
                block_ms=block_ms,
                client=self.redis_client,
                stream_name=self.stream_name,
                group_name=self.group_name,
            )
        except Exception as read_err:
            logger.warning(f"WorkerConsumer: Temporary error reading from stream: {read_err}")
            return processed_count

        for job in new_jobs:
            if use_async:
                task = asyncio.create_task(
                    self._safe_job_task(
                        job,
                        process_class_fn=process_class_fn,
                        is_reclaimed=False,
                        drain_stale_jobs=check_stale,
                    )
                )
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                processed_count += 1
            else:
                try:
                    ok = await self.process_single_job(
                        job,
                        process_class_fn=process_class_fn,
                        is_reclaimed=False,
                        drain_stale_jobs=check_stale,
                    )
                    if ok:
                        processed_count += 1
                except Exception:
                    # Exception was already logged in process_single_job; continues loop
                    pass

        return processed_count

    async def run(
        self,
        poll_interval: float = 1.0,
        process_class_fn: Optional[Callable] = None,
    ):
        """
        Starts the worker consumer loop. Runs until stop() is invoked.
        Resilient against temporary Redis outages with automatic backoff.
        """
        self.is_running = True
        self._stop_event.clear()
        logger.info(
            f"WorkerConsumer: Loop started for consumer '{self.consumer_name}' "
            f"(max_concurrent_jobs={self.max_concurrent_jobs})."
        )

        while self.is_running and not self._stop_event.is_set():
            try:
                # Capacity-aware check: If all slots are full, wait without polling Redis
                available_slots = self.max_concurrent_jobs - len(self._active_tasks)
                if available_slots <= 0:
                    await asyncio.sleep(0.2)
                    continue

                await self.poll_and_process(
                    count=available_slots,
                    block_ms=2000,
                    process_class_fn=process_class_fn,
                    check_stale=True,
                )
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                logger.info("WorkerConsumer: Received cancellation signal. Shutting down loop.")
                break
            except Exception as e:
                safe_err = sanitize_log_message(e)
                logger.error(f"WorkerConsumer: Unexpected loop error: {safe_err}. Backing off 3s...")
                await asyncio.sleep(3.0)

        # Graceful shutdown: wait for active tasks
        await self._wait_for_active_tasks(timeout=30.0)
        self.is_running = False
        logger.info(f"WorkerConsumer: Consumer '{self.consumer_name}' stopped cleanly.")

    def stop(self):
        """Signals the consumer loop to terminate gracefully."""
        self.is_running = False
        self._stop_event.set()
        logger.info(f"WorkerConsumer: Stop signal sent to '{self.consumer_name}'.")
