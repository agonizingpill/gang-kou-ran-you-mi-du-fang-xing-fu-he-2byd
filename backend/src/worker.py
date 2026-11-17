import logging
import os
import time
from pathlib import Path

from redis import Redis

from .plan_service import process_job, process_next_job, recover_jobs, train_next

logging.basicConfig(level=logging.INFO)
r = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
HEARTBEAT = Path(os.getenv("HEARTBEAT_PATH", "/artifacts/bunker-heartbeat"))

recovered = False

while True:
    HEARTBEAT.write_text(str(time.time()))
    if not recovered:
        try:
            recover_jobs()
            recovered = True
        except Exception:
            logging.exception("startup recovery failed, retrying")
            time.sleep(2)
            continue
    try:
        if train_next():
            continue
    except Exception:
        logging.exception("density plan training pass failed")
        time.sleep(2)
        continue
    item = r.blpop("density-check", timeout=2)
    if not item:
        # Also drains jobs committed just before a crash without a queue entry.
        try:
            process_next_job()
        except Exception:
            logging.exception("queued comparison sweep failed")
        continue
    try:
        job_id = int(item[1])
    except (TypeError, ValueError):
        continue
    try:
        process_job(job_id)
    except Exception:
        # A job left running is re-queued by the next boot recovery pass.
        logging.exception("comparison job %s failed, left for recovery", job_id)
