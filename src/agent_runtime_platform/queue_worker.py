from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import signal
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.engine import make_url

from agent_runtime_platform.api import create_app
from agent_runtime_platform.models import HumanChatRun, QueueJob, RoomRun, RoomRunEvent, RoomRunTurn, Run, Task
from agent_runtime_platform.runtime import _append_event, _append_human_chat_event
from agent_runtime_platform.resource_auth import unique_run_parent

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
POLL_SECONDS = float(os.getenv("AGENT_RUNTIME_QUEUE_POLL_SECONDS", "1"))
STOP_REQUESTED = threading.Event()


class WorkerAlreadyRunning(RuntimeError):
    pass


def acquire_worker_lock(runtime) -> int:
    """Use an OS advisory lock so only one local worker can recover/claim this DB."""
    url = make_url(runtime.database.url)
    if url.get_backend_name() == "sqlite" and url.database in (None, "", ":memory:"):
        raise ValueError("The queue worker requires a persistent SQLite database file.")
    if url.get_backend_name() == "sqlite":
        identity = str(Path(url.database).expanduser().resolve())
    else:
        identity = url.render_as_string(hide_password=True)
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"agent-runtime-worker-{os.getuid()}-{key}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise WorkerAlreadyRunning("Another queue worker already owns this database.") from exc
    return descriptor


def _append_room_event(session, run: RoomRun, event_type: str, payload: dict) -> None:
    run.next_event_sequence += 1
    session.add(RoomRunEvent(
        run_id=run.id, sequence=run.next_event_sequence,
        event_type=event_type, payload=payload,
    ))


def recover_interrupted_jobs(runtime) -> None:
    """Return interrupted claims to the queue; attempts still bound total executions."""
    with runtime.database.session() as session:
        jobs = session.scalars(select(QueueJob).where(QueueJob.status == "running")).all()
        for job in jobs:
            parent_kind = unique_run_parent(session, job.run_id)
            if parent_kind is None:
                job.status = "failed"
                job.last_error = "ambiguous_run_parent"
                job.updated_at = datetime.now(timezone.utc)
                continue
            run = session.get(Run, job.run_id) if parent_kind == "run" else None
            chat_run = session.get(HumanChatRun, job.run_id) if parent_kind == "human_chat" else None
            room_run = session.get(RoomRun, job.run_id) if parent_kind == "room" else None
            if job.attempts >= job.max_attempts:
                job.status = "failed"
                job.last_error = "worker_interrupted"
                if run is not None:
                    run.status = "failed"
                    run.error_code = "worker_interrupted"
                    run.completed_at = datetime.now(timezone.utc)
                    _append_event(session, run, "run_failed", {"error_code": run.error_code})
                elif room_run is not None:
                    room_run.status = "failed"
                    room_run.error_code = "worker_interrupted"
                    room_run.completed_at = datetime.now(timezone.utc)
                    for turn in session.scalars(select(RoomRunTurn).where(
                        RoomRunTurn.run_id == room_run.id, RoomRunTurn.status.in_(["queued", "running"])
                    )).all():
                        turn.status = "failed"
                        turn.completed_at = room_run.completed_at
                    job.updated_at = room_run.completed_at
                    _append_room_event(session, room_run, "run_failed", {"error_code": room_run.error_code})
                elif chat_run is not None:
                    chat_run.status = "failed"
                    chat_run.error_code = "worker_interrupted"
                    chat_run.completed_at = datetime.now(timezone.utc)
                    tasks = session.scalars(
                        select(Task).where(
                            Task.root_run_id == chat_run.id, Task.status.in_(["running", "queued"])
                        )
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
                elif room_run is not None and room_run.status != "completed":
                    room_run.status = "queued"
                    for turn in session.scalars(select(RoomRunTurn).where(
                        RoomRunTurn.run_id == room_run.id, RoomRunTurn.status == "running"
                    )).all():
                        turn.status = "queued"
                    _append_room_event(session, room_run, "worker_recovered", {"attempt": job.attempts})
                elif chat_run is not None and chat_run.status != "completed":
                    chat_run.status = "queued"
                    tasks = session.scalars(
                        select(Task).where(Task.root_run_id == chat_run.id, Task.status == "running")
                    ).all()
                    for task in tasks:
                        task.status = "queued"
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
        parent_kind = unique_run_parent(session, job_id)
        if parent_kind is None:
            job.status = "failed"
            job.last_error = "ambiguous_run_parent"
            job.updated_at = datetime.now(timezone.utc)
            session.commit()
            return None
        run = session.get(Run, job_id) if parent_kind == "run" else None
        chat_run = session.get(HumanChatRun, job_id) if parent_kind == "human_chat" else None
        room_run = session.get(RoomRun, job_id) if parent_kind == "room" else None
        if run is not None and run.status == "queued":
            run.status = "running"
            _append_event(session, run, "run_started" if job.attempts == 1 else "worker_attempt_started", {"attempt": job.attempts})
        elif room_run is not None and room_run.status == "queued":
            room_run.status = "running"
            room_run.started_at = room_run.started_at or datetime.now(timezone.utc)
            _append_room_event(session, room_run, "run_started" if job.attempts == 1 else "worker_attempt_started", {"attempt": job.attempts})
        elif chat_run is not None and chat_run.status == "queued":
            chat_run.status = "running"
            tasks = session.scalars(
                select(Task).where(Task.root_run_id == chat_run.id, Task.status == "queued")
            ).all()
            for task in tasks:
                task.status = "running"
            _append_human_chat_event(session, chat_run, "run_started" if job.attempts == 1 else "worker_attempt_started", {"attempt": job.attempts})
        session.commit()
        return job_id


def main() -> None:
    app = create_app()
    runtime = app.state.runtime
    lock_descriptor = acquire_worker_lock(runtime)

    def request_stop(_signum, _frame) -> None:
        STOP_REQUESTED.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        # Recovery is safe only after exclusive process ownership is established.
        recover_interrupted_jobs(runtime)
        logger.info("Persistent queue worker started")
        while not STOP_REQUESTED.is_set():
            run_id = claim_one(runtime)
            if run_id is None:
                STOP_REQUESTED.wait(POLL_SECONDS)
                continue
            # Finish a claimed model call on graceful shutdown. An unexpected error
            # exits the process so systemd restart recovery can reclaim its job.
            try:
                runtime.execute_queued_run(run_id)
            except Exception:
                logger.exception("Unexpected queue worker error for run %s", run_id)
                raise
    finally:
        os.close(lock_descriptor)


if __name__ == "__main__":
    main()
