"""
CloudNote Ingestion Worker Process
----------------------------------
Dedicated runtime entrypoint for the WorkerConsumer.
Consumes class ingestion jobs from the Redis Stream consumer group,
acquires distributed execution locks, retrieves user credentials in-memory,
and orchestrates browser sessions via process_class().
Strictly decoupled: Never runs timetable fetching or scheduling logic.
"""

import asyncio
import logging
import signal
import sys
from typing import Optional

from .config import settings
from .logger import logger
from .job_queue import get_redis_client, ensure_consumer_group, DEFAULT_STREAM_NAME, DEFAULT_CONSUMER_GROUP
from .worker_consumer import WorkerConsumer, generate_consumer_name


class StandaloneWorkerService:
    """Manages the lifecycle of a dedicated WorkerConsumer service."""

    def __init__(
        self,
        stream_name: str = DEFAULT_STREAM_NAME,
        group_name: str = DEFAULT_CONSUMER_GROUP,
        consumer_name: Optional[str] = None,
    ):
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name or generate_consumer_name(prefix="worker-node")
        self.consumer: Optional[WorkerConsumer] = None
        self._shutdown_event = asyncio.Event()

    def handle_signal(self, sig, frame=None):
        """Signal handler for graceful container termination."""
        sig_name = signal.Signals(sig).name if hasattr(signal, "Signals") else str(sig)
        logger.info(f"WorkerService: Received shutdown signal {sig_name}. Initiating graceful stop...")
        if self.consumer:
            self.consumer.stop()
        self._shutdown_event.set()

    async def run(self):
        """Main execution loop for dedicated worker container."""
        logger.info("==========================================================")
        logger.info(f"Starting Dedicated CloudNote Ingestion Worker: {self.consumer_name}")
        logger.info("==========================================================")

        # 1. Connectivity & Group initialization with bounded retry
        max_attempts = 5
        retry_delay = 1.0
        connected = False
        r = None

        for attempt in range(1, max_attempts + 1):
            try:
                r = get_redis_client()
                if not r.ping():
                    raise ConnectionError("Redis ping failed.")
                ensure_consumer_group(client=r, stream_name=self.stream_name, group_name=self.group_name)
                logger.info(f"WorkerService: Verified Redis connection and consumer group '{self.group_name}'.")
                connected = True
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt < max_attempts and not self._shutdown_event.is_set():
                    logger.warning(
                        f"WorkerService: Redis startup check failed (attempt {attempt}/{max_attempts}): {e}. "
                        f"Retrying in {retry_delay}s..."
                    )
                    try:
                        await asyncio.sleep(retry_delay)
                    except asyncio.CancelledError:
                        raise
                else:
                    logger.error(
                        f"WorkerService: Failed to connect to Redis or initialize group after {max_attempts} attempts: {e}"
                    )

        if not connected:
            sys.exit(1)

        # 2. Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.handle_signal, sig)
            except (NotImplementedError, AttributeError):
                signal.signal(sig, self.handle_signal)

        # 3. Instantiate and run consumer
        self.consumer = WorkerConsumer(
            consumer_name=self.consumer_name,
            stream_name=self.stream_name,
            group_name=self.group_name,
            redis_client=r,
        )

        try:
            await self.consumer.run()
        except asyncio.CancelledError:
            logger.info("WorkerService: Task cancelled. Stopping consumer...")
            if self.consumer:
                self.consumer.stop()
        except Exception as e:
            logger.error(f"WorkerService: Fatal consumer loop error: {e}")
        finally:
            logger.info("WorkerService: Consumer loop ended. Clean shutdown complete.")


async def run_worker():
    service = StandaloneWorkerService()
    await service.run()


if __name__ == "__main__":
    asyncio.run(run_worker())
