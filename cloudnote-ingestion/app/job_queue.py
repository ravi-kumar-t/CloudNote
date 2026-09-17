"""
CloudNote Ingestion Job Queue
-----------------------------
Redis Streams-based job queue infrastructure for multi-user class ingestion.
Designed for high resilience, deterministic serialization, duplicate protection,
and strict zero-credential isolation.
"""

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any, Optional
import uuid
import redis

from .config import settings
from .logger import logger

# Constants & Defaults
DEFAULT_STREAM_NAME = "cloudnote:jobs:stream"
DEFAULT_CONSUMER_GROUP = "cloudnote:ingestion:workers"
IDEMPOTENCY_KEY_PREFIX = "cloudnote:job:idempotency:"
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 86400  # 24 hours

# Security: Strict blacklist of sensitive keys forbidden anywhere in payloads
FORBIDDEN_PAYLOAD_KEYS = {
    "lpu_password",
    "encrypted_portal_password",
    "password",
    "jwt_secret",
    "gemini_api_key",
    "secret",
    "token",
    "auth_token",
    "portal_password",
}


class JobQueueSecurityError(ValueError):
    """Raised when an attempt is made to enqueue sensitive credentials into Redis."""
    pass


class JobQueueMalformedError(ValueError):
    """Raised when an incoming job payload is structurally invalid or unparseable."""
    pass


def _check_sensitive_keys(data: Any) -> Optional[str]:
    """
    Recursively scans nested dictionaries and lists to ensure no sensitive credentials
    are present in the job payload. Returns the offending key name if found, else None.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            key_str = str(key).lower().strip()
            for forbidden in FORBIDDEN_PAYLOAD_KEYS:
                if forbidden == key_str or forbidden in key_str:
                    return str(key)
            nested_result = _check_sensitive_keys(value)
            if nested_result:
                return nested_result
    elif isinstance(data, (list, tuple, set)):
        for item in data:
            nested_result = _check_sensitive_keys(item)
            if nested_result:
                return nested_result
    return None


@dataclass
class IngestionJob:
    """
    Non-secret, deterministic representation of a targeted class ingestion job.
    """
    job_id: str
    user_id: int
    class_metadata: dict[str, Any]
    created_at: str
    idempotency_key: Optional[str] = None
    stream_message_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Converts job to a serializable dictionary (omits stream_message_id)."""
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "class_metadata": self.class_metadata,
            "created_at": self.created_at,
            "idempotency_key": self.idempotency_key,
        }

    def to_json(self) -> str:
        """Deterministic JSON serialization with sorted keys."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any], stream_message_id: Optional[str] = None) -> "IngestionJob":
        """Reconstitutes an IngestionJob from a dictionary with strict validation."""
        if not isinstance(data, dict):
            raise JobQueueMalformedError("Job payload must be a JSON object dictionary.")

        job_id = data.get("job_id")
        if not job_id or not isinstance(job_id, str):
            raise JobQueueMalformedError("Job payload missing required string 'job_id'.")

        user_id = data.get("user_id")
        if user_id is None:
            raise JobQueueMalformedError("Job payload missing required 'user_id'.")
        try:
            user_id = int(user_id)
            if user_id <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise JobQueueMalformedError(f"Invalid user_id '{user_id}'; must be a positive integer.")

        class_metadata = data.get("class_metadata")
        if not isinstance(class_metadata, dict):
            raise JobQueueMalformedError("Job payload missing required dictionary 'class_metadata'.")

        created_at = data.get("created_at")
        if not created_at or not isinstance(created_at, str):
            raise JobQueueMalformedError("Job payload missing required string 'created_at'.")

        # Strict security audit on unpickled payload
        offending_key = _check_sensitive_keys(data)
        if offending_key:
            raise JobQueueSecurityError(
                f"Security Violation: Payload contains forbidden credential field '{offending_key}'."
            )

        return cls(
            job_id=job_id,
            user_id=user_id,
            class_metadata=class_metadata,
            created_at=created_at,
            idempotency_key=data.get("idempotency_key"),
            stream_message_id=stream_message_id,
        )

    @classmethod
    def from_json(cls, raw_json: str, stream_message_id: Optional[str] = None) -> "IngestionJob":
        """Parses a JSON string into an IngestionJob."""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as err:
            raise JobQueueMalformedError(f"Failed to decode job JSON payload: {err}") from err
        return cls.from_dict(data, stream_message_id=stream_message_id)


def get_redis_client(redis_url: Optional[str] = None) -> redis.Redis:
    """
    Returns a connected redis.Redis client configured with timeouts.
    Uses the existing REDIS_URL configuration mechanism.
    """
    url = redis_url or settings.REDIS_URL or "redis://redis:6379/0"
    return redis.Redis.from_url(
        url,
        socket_timeout=3.0,
        socket_connect_timeout=3.0,
        decode_responses=True,
    )


def ensure_consumer_group(
    client: Optional[redis.Redis] = None,
    stream_name: str = DEFAULT_STREAM_NAME,
    group_name: str = DEFAULT_CONSUMER_GROUP,
) -> bool:
    """
    Ensures that the Redis Stream and Consumer Group exist.
    Uses '0' as the starting ID so historical or newly enqueued messages are visible.
    Gracefully handles BUSYGROUP if the consumer group already exists.
    """
    r = client or get_redis_client()
    try:
        r.xgroup_create(stream_name, group_name, id="0", mkstream=True)
        logger.info(f"JobQueue: Created consumer group '{group_name}' on stream '{stream_name}'.")
        return True
    except redis.exceptions.ResponseError as err:
        if "BUSYGROUP" in str(err):
            # Consumer group already exists; normal expected state
            return True
        logger.error(f"JobQueue: Error creating consumer group '{group_name}': {err}")
        raise


def enqueue_job(
    user_id: int,
    class_metadata: dict[str, Any],
    idempotency_key: Optional[str] = None,
    client: Optional[redis.Redis] = None,
    stream_name: str = DEFAULT_STREAM_NAME,
    ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
) -> tuple[Optional[IngestionJob], bool]:
    """
    Enqueues a targeted ingestion job onto the Redis Stream.

    Returns:
        tuple[Optional[IngestionJob], bool]:
            (job, True) if newly enqueued.
            (None, False) if duplicate prevented via idempotency key.

    Raises:
        JobQueueSecurityError: If any sensitive credentials/keys are found in class_metadata.
        JobQueueMalformedError: If input parameters fail validation.
    """
    # 1. Validation & Security Audit
    try:
        uid = int(user_id)
        if uid <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise JobQueueMalformedError(f"Invalid user_id '{user_id}'; must be a positive integer.")

    if not isinstance(class_metadata, dict):
        raise JobQueueMalformedError("class_metadata must be a dictionary.")

    offending_key = _check_sensitive_keys(class_metadata)
    if offending_key:
        raise JobQueueSecurityError(
            f"Security Violation: class_metadata contains forbidden credential field '{offending_key}'. "
            "Credentials must never be placed into Redis queues."
        )

    r = client or get_redis_client()

    # 2. Idempotency Check & Reservation
    job_id = uuid.uuid4().hex
    if idempotency_key:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise JobQueueMalformedError("idempotency_key must be a non-empty string when provided.")
        idemp_redis_key = f"{IDEMPOTENCY_KEY_PREFIX}{idempotency_key.strip()}"
        # Atomic reservation using SET NX EX
        acquired = r.set(idemp_redis_key, job_id, nx=True, ex=ttl_seconds)
        if not acquired:
            logger.info(f"JobQueue: Duplicate job detected for idempotency_key '{idempotency_key}'. Skipping enqueue.")
            return None, False

    # 3. Construct and Serialize Job
    created_at = datetime.now(timezone.utc).isoformat()
    job = IngestionJob(
        job_id=job_id,
        user_id=uid,
        class_metadata=class_metadata,
        created_at=created_at,
        idempotency_key=idempotency_key.strip() if idempotency_key else None,
    )

    # 4. Stream Enqueue (XADD)
    try:
        stream_message_id = r.xadd(stream_name, {"payload": job.to_json()})
        job.stream_message_id = stream_message_id
        logger.info(
            f"JobQueue: Successfully enqueued job '{job.job_id}' (stream_id: {stream_message_id}) "
            f"for user {uid} on '{stream_name}'."
        )
        return job, True
    except Exception as e:
        # If stream append failed, release the idempotency key so retry is possible
        if idempotency_key:
            try:
                r.delete(f"{IDEMPOTENCY_KEY_PREFIX}{idempotency_key.strip()}")
            except Exception:
                pass
        logger.error(f"JobQueue: Failed to enqueue job '{job_id}' to stream '{stream_name}': {e}")
        raise


def consume_jobs(
    consumer_name: str,
    count: int = 1,
    block_ms: int = 2000,
    client: Optional[redis.Redis] = None,
    stream_name: str = DEFAULT_STREAM_NAME,
    group_name: str = DEFAULT_CONSUMER_GROUP,
) -> list[IngestionJob]:
    """
    Reads new unacknowledged jobs from the consumer group stream using XREADGROUP.
    Ensures the consumer group exists before attempting read.
    """
    if not consumer_name or not isinstance(consumer_name, str):
        raise ValueError("consumer_name must be a non-empty string.")

    r = client or get_redis_client()
    ensure_consumer_group(client=r, stream_name=stream_name, group_name=group_name)

    try:
        # ">" means only messages never delivered to other consumers in this group
        entries = r.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={stream_name: ">"},
            count=count,
            block=block_ms,
        )
    except Exception as e:
        logger.error(f"JobQueue: Failed reading from stream '{stream_name}' via group '{group_name}': {e}")
        raise

    jobs: list[IngestionJob] = []
    if not entries:
        return jobs

    for stream, message_list in entries:
        for msg_id, fields in message_list:
            raw_payload = fields.get("payload")
            if not raw_payload:
                logger.warning(f"JobQueue: Message {msg_id} missing 'payload' field. Skipping.")
                continue
            try:
                job = IngestionJob.from_json(raw_payload, stream_message_id=msg_id)
                jobs.append(job)
            except Exception as err:
                logger.error(f"JobQueue: Failed to parse job payload for message {msg_id}: {err}")

    return jobs


def acknowledge_job(
    message_id: str,
    client: Optional[redis.Redis] = None,
    stream_name: str = DEFAULT_STREAM_NAME,
    group_name: str = DEFAULT_CONSUMER_GROUP,
) -> bool:
    """
    Acknowledges receipt and completion of a job using XACK.
    Removes the message from the Consumer Group Pending Entries List (PEL).
    """
    if not message_id or not isinstance(message_id, str):
        raise ValueError("message_id must be a non-empty string.")

    r = client or get_redis_client()
    try:
        ack_count = r.xack(stream_name, group_name, message_id)
        return ack_count > 0
    except Exception as e:
        logger.error(f"JobQueue: Error acknowledging message '{message_id}' on stream '{stream_name}': {e}")
        raise


def get_pending_jobs(
    client: Optional[redis.Redis] = None,
    stream_name: str = DEFAULT_STREAM_NAME,
    group_name: str = DEFAULT_CONSUMER_GROUP,
    count: int = 10,
) -> list[dict[str, Any]]:
    """
    Inspects pending unacknowledged jobs (PEL) for the consumer group using XPENDING range.
    """
    r = client or get_redis_client()
    try:
        pending_records = r.xpending_range(
            name=stream_name,
            groupname=group_name,
            min="-",
            max="+",
            count=count,
        )
        return [
            {
                "message_id": rec.get("message_id"),
                "consumer": rec.get("consumer"),
                "idle_time_ms": rec.get("idle_time"),
                "delivery_count": rec.get("times_delivered"),
            }
            for rec in pending_records
        ]
    except Exception as e:
        logger.error(f"JobQueue: Error retrieving pending jobs on stream '{stream_name}': {e}")
        raise


def claim_pending_jobs(
    consumer_name: str,
    min_idle_time_ms: int = 300000,
    count: int = 5,
    client: Optional[redis.Redis] = None,
    stream_name: str = DEFAULT_STREAM_NAME,
    group_name: str = DEFAULT_CONSUMER_GROUP,
) -> list[IngestionJob]:
    """
    Reclaims pending jobs that have been idle for at least min_idle_time_ms
    using Redis XAUTOCLAIM. Automatically transfers ownership to consumer_name.
    """
    if not consumer_name or not isinstance(consumer_name, str):
        raise ValueError("consumer_name must be a non-empty string.")

    r = client or get_redis_client()
    ensure_consumer_group(client=r, stream_name=stream_name, group_name=group_name)

    try:
        res = r.xautoclaim(
            name=stream_name,
            groupname=group_name,
            consumername=consumer_name,
            min_idle_time=min_idle_time_ms,
            start_id="0-0",
            count=count,
        )
        claimed_entries = res[1] if len(res) > 1 else []
    except Exception as e:
        logger.error(f"JobQueue: Failed to auto-claim pending jobs on '{stream_name}': {e}")
        return []

    jobs: list[IngestionJob] = []
    for item in claimed_entries:
        if isinstance(item, tuple) and len(item) == 2:
            msg_id, fields = item
        elif isinstance(item, dict):
            msg_id = item.get("id") or item.get("message_id")
            fields = item.get("fields", item)
        else:
            continue

        raw_payload = fields.get("payload") if isinstance(fields, dict) else None
        if not raw_payload:
            continue
        try:
            job = IngestionJob.from_json(raw_payload, stream_message_id=str(msg_id))
            jobs.append(job)
        except Exception as err:
            logger.error(f"JobQueue: Failed to parse reclaimed job payload for {msg_id}: {err}")

    return jobs


# Execution Concurrency Lock Helpers
EXEC_LOCK_PREFIX = "cloudnote:lock:exec:"
DEFAULT_EXEC_LOCK_TTL_SECONDS = 60  # 60 seconds with heartbeat keep-alive
DEFAULT_EXEC_LOCK_HEARTBEAT_SECONDS = 15.0  # Renew every 15s while processing


def get_execution_lock_key(user_id: int, class_metadata: dict[str, Any]) -> str:
    """
    Generates a deterministic Redis key for execution concurrency control (no credentials).
    Combines user_id, subject_code, and date/timing to prevent cross-class collisions.
    """
    subject = str(class_metadata.get("subject_code", "class")).strip()
    start_time = class_metadata.get("start_time")
    date_val = class_metadata.get("date")
    timings_val = class_metadata.get("timings")

    if start_time:
        slot = str(start_time)
    elif date_val and timings_val:
        slot = f"{date_val}_{timings_val}"
    elif timings_val:
        slot = str(timings_val)
    elif date_val:
        slot = str(date_val)
    else:
        slot = "active"

    safe_slot = "".join(c if c.isalnum() or c in "-_:" else "_" for c in slot)
    return f"{EXEC_LOCK_PREFIX}user:{user_id}:class:{subject}:{safe_slot}"


class ExecutionLock:
    """
    Distributed execution lock with background heartbeat renewal.
    Maintains mutual exclusion as long as the worker is alive, and automatically
    expires shortly after an unexpected worker crash (within ttl_seconds).
    """

    def __init__(
        self,
        lock_key: str,
        consumer_name: str,
        client: Optional[redis.Redis] = None,
        ttl_seconds: int = DEFAULT_EXEC_LOCK_TTL_SECONDS,
        heartbeat_interval: float = DEFAULT_EXEC_LOCK_HEARTBEAT_SECONDS,
    ):
        self.lock_key = lock_key
        self.consumer_name = consumer_name
        self.client = client or get_redis_client()
        self.ttl_seconds = ttl_seconds
        self.heartbeat_interval = heartbeat_interval
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._acquired = False

    async def acquire(self) -> bool:
        """Acquires lock in Redis and spawns heartbeat task to keep it alive."""
        try:
            acquired = bool(
                self.client.set(
                    self.lock_key,
                    self.consumer_name,
                    nx=True,
                    ex=self.ttl_seconds,
                )
            )
            if acquired:
                self._acquired = True
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            return acquired
        except Exception as e:
            logger.error(f"ExecutionLock: Failed to acquire '{self.lock_key}': {e}")
            return False

    async def _heartbeat_loop(self):
        """Periodically extends the lock TTL as long as processing is active."""
        lua_renew = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        try:
            while self._acquired:
                await asyncio.sleep(self.heartbeat_interval)
                if not self._acquired:
                    break
                renewed = self.client.eval(lua_renew, 1, self.lock_key, self.consumer_name, self.ttl_seconds)
                if not renewed:
                    logger.warning(f"ExecutionLock: Lock '{self.lock_key}' ownership lost during renewal.")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"ExecutionLock: Heartbeat error for '{self.lock_key}': {e}")

    async def release(self) -> bool:
        """Stops heartbeat task and deletes lock key atomically via Lua."""
        self._acquired = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        lua_release = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            res = self.client.eval(lua_release, 1, self.lock_key, self.consumer_name)
            return bool(res)
        except Exception as e:
            logger.error(f"ExecutionLock: Error releasing '{self.lock_key}': {e}")
            return False

    async def __aenter__(self):
        acquired = await self.acquire()
        if not acquired:
            raise RuntimeError(f"Could not acquire execution lock for '{self.lock_key}'")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


def acquire_execution_lock(
    user_id: int,
    class_metadata: dict[str, Any],
    consumer_name: str,
    ttl_seconds: int = DEFAULT_EXEC_LOCK_TTL_SECONDS,
    client: Optional[redis.Redis] = None,
) -> tuple[bool, str]:
    """
    Synchronous / simple lock acquisition fallback.
    Returns (acquired: bool, lock_key: str).
    """
    r = client or get_redis_client()
    lock_key = get_execution_lock_key(user_id, class_metadata)
    try:
        acquired = bool(r.set(lock_key, consumer_name, nx=True, ex=ttl_seconds))
        return acquired, lock_key
    except Exception as e:
        logger.error(f"JobQueue: Error acquiring execution lock '{lock_key}': {e}")
        return False, lock_key


def release_execution_lock(
    lock_key: str,
    consumer_name: str,
    client: Optional[redis.Redis] = None,
) -> bool:
    """
    Safely releases the execution lock only if the current consumer still holds it.
    """
    r = client or get_redis_client()
    lua_release = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """
    try:
        res = r.eval(lua_release, 1, lock_key, consumer_name)
        return bool(res)
    except Exception as e:
        logger.error(f"JobQueue: Error releasing execution lock '{lock_key}': {e}")
        return False


def generate_class_idempotency_key(user_id: int, class_metadata: dict[str, Any]) -> str:
    """
    Generates a deterministic idempotency key for a scheduled class session.
    Combines user_id, subject_code, and date/start_time to ensure uniqueness per session.
    """
    subject = str(class_metadata.get("subject_code", "class")).strip()
    start_time = class_metadata.get("start_time")
    date_val = class_metadata.get("date")
    timings_val = class_metadata.get("timings")

    if start_time:
        slot = str(start_time)
    elif date_val and timings_val:
        slot = f"{date_val}_{timings_val}"
    elif timings_val:
        slot = str(timings_val)
    elif date_val:
        slot = str(date_val)
    else:
        slot = "active"

    safe_slot = "".join(c if c.isalnum() or c in "-_:" else "_" for c in slot)
    return f"user:{user_id}:class:{subject}:{safe_slot}"


def schedule_class_to_queue(
    class_metadata: dict[str, Any],
    user_id: Optional[int] = None,
    client: Optional[redis.Redis] = None,
    stream_name: str = DEFAULT_STREAM_NAME,
) -> tuple[Optional[IngestionJob], bool]:
    """
    Helper function for schedulers to enqueue a targeted class ingestion job
    with a deterministic idempotency key and safe non-secret payload.
    """
    if user_id is None:
        from .database import get_or_create_ingestion_user
        user_id = get_or_create_ingestion_user(settings.LPU_USERNAME)

    idempotency_key = generate_class_idempotency_key(user_id, class_metadata)
    return enqueue_job(
        user_id=user_id,
        class_metadata=class_metadata,
        idempotency_key=idempotency_key,
        client=client,
        stream_name=stream_name,
    )
