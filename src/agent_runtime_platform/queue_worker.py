from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import select, update

from agent_runtime_platform.api import create_app
from agent_runtime_platform.models import HumanChatRun, QueueJob, Run, Task
from agent_runtime_platform.runtime import _append_event, _append_human_chat_event

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
POLL_SECONDS = float(os.getenv("AGENT_RUNTIME_QUEUE_POLL_SECONDS", "1"))


def recover_interrupted_jobs(runtime) -> None:
    """Return interrupted claims to the queue; attempts still bound total executions."""
    with runtime.database.session() as session:
        jobs = session.scalars(select(QueueJob).where(QueueJob.status == "running")).all()
        for job in jobs:
            run = session.get(Run, job.run_id)
            chat_run = session.get(HumanChatRun, job.run_id)
            if job.attempts >= job.max_attempts:
                job.status = "failed"
                job.last_error = "worker_interrupted"
                if run is not None:
                    run.status = "failed"
                    run.error_code = "worker_interrupted"
                    run.completed_at = datetime.now(timezone.utc)
                    _append_event(session, run, "run_failed", {"error_code": run.error_code})
                elif chat_run is not None:
                    chat_run.status = "failed"
                    chat_run.error_code = "worker_interrupted"
                    chat_run.completed_at = datetime.now(timezone.utc)
                    tasks = session.scalars(
                        select(Task).where(Task.root_run_id == chat_run.id, Task.status == "running")
                    ).all()
                    for task in tasks:
                        task.status = "failed"
                        task.error_code = chat_run.error_code
                        task.completed_at = chat_run.completed_at
                    _append_human_chat_event(session, chat_run, "run_failed", {"error_code": chat_run.error_code})
            else:
                job.status = "pending"
                job.last_error = "worker_interrupted"
                if run is not None and run.status != "completed":
                    run.status = "queued"
                    _append_event(session, run, "worker_recovered", {"attempt": job.attempts})
                elif chat_run is not None and chat_run.status != "completed":
                    chat_run.status = "queued"
                    _append_human_chat_event(session, chat_run, "worker_recovered", {"attempt": job.attempts})
        session.commit()


def claim_one(runtime) -> str | None:
    with runtime.database.session() as session:
        job_id = session.scalar(
            select(QueueJob.run_id).where(QueueJob.status == "pending")
            .order_by(QueueJob.created_at, QueueJob.run_id).limit(1)
        )
        if job_id is None:
            return None
        claimed = session.execute(
            update(QueueJob).where(QueueJob.run_id == job_id, QueueJob.status == "pending")
            .values(status="running", attempts=QueueJob.attempts + 1, updated_at=datetime.now(timezone.utc))
        )
        if claimed.rowcount != 1:
            session.rollback()
            return None
        job = session.get(QueueJob, job_id)
        run = session.get(Run, job_id)
        chat_run = session.get(HumanChatRun, job_id)
        if run is not None and run.status == "queued":
            run.status = "running"
            _append_event(session, run, "run_started" if job.attempts == 1 else "worker_attempt_started", {"attempt": job.attempts})
        elif chat_run is not None and chat_run.status == "queued":
            chat_run.status = "running"
            _append_human_chat_event(session, chat_run, "run_started" if job.attempts == 1 else "worker_attempt_started", {"attempt": job.attempts})
        session.commit()
        return job_id


def main() -> None:
    app = create_app()
    runtime = app.state.runtime
    recover_interrupted_jobs(runtime)
    logger.info("Persistent queue worker started")
    while True:
        run_id = claim_one(runtime)
        if run_id is None:
            time.sleep(POLL_SECONDS)
            continue
        try:
            runtime.execute_queued_run(run_id)
        except Exception:
            logger.exception("Unexpected queue worker error for run %s", run_id)


if __name__ == "__main__":
    main()
