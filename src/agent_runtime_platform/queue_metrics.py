"""Read-only queue and model-call measurements for docs/queue-scaling-decision.md.

The report aggregates the persisted ``queue_jobs`` rows and the run event traces
into the quantities the queue-scaling decision gate names: queue depth over
time, oldest pending age, enqueue-to-start wait, processing and end-to-end
durations, throughput and job mix, attempts, retries, recovery, and model-call
durations and error counts.

It carries counts, statuses, timings and coarse error categories only. Message
content, prompts, tool arguments, provider configuration, credentials and error
messages never enter the report.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import select

from agent_runtime_platform.database import Database
from agent_runtime_platform.models import (
    HumanChatRun,
    HumanChatRunEvent,
    QueueJob,
    RoomRun,
    RoomRunEvent,
    Run,
    RunEvent,
)

# Lifecycle events that move a queued job between pending and running.
CLAIM_EVENT_TYPES = frozenset({"run_started", "worker_attempt_started", "room_run_started"})
WAIT_EVENT_TYPES = frozenset({"retry_scheduled", "worker_recovered"})
TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed"})
RECOVERY_EVENT_TYPES = frozenset({"worker_recovered"})
# Model calls appear as generic calls (messaging runs) or as room turns.
MODEL_CALL_START_TYPES = frozenset({"model_call_started", "room_turn_started"})
MODEL_CALL_END_TYPES = frozenset({"model_call_completed", "room_turn_completed"})
PARENT_MODELS = {
    "run": (Run, RunEvent),
    "human_chat": (HumanChatRun, HumanChatRunEvent),
    "room": (RoomRun, RoomRunEvent),
}
PERCENTILES = (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99))
DEFAULT_DATABASE_URL = "sqlite:///./data/agent_runtime.db"


@dataclass(frozen=True)
class _Event:
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    sequence: int


def _utc(value: datetime) -> datetime:
    """SQLite returns naive timestamps; this repository stores UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, **{name: None for name, _ in PERCENTILES}, "max": None}
    summary: dict[str, Any] = {"count": len(ordered)}
    for name, percentile in PERCENTILES:
        index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
        summary[name] = round(ordered[index], 3)
    summary["max"] = round(ordered[-1], 3)
    return summary


def _merged_seconds(
    spans: list[tuple[datetime, datetime]], window_start: datetime, window_end: datetime
) -> float:
    """Length of the union of the spans, clipped to the observation window."""
    clipped = sorted(
        (max(start, window_start), min(end, window_end)) for start, end in spans if end > start
    )
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for start, end in clipped:
        if end <= start:
            continue
        if current_end is None or start > current_end:
            if current_end is not None and current_start is not None:
                total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None and current_start is not None:
        total += (current_end - current_start).total_seconds()
    return total


def _peak_concurrency(
    spans: list[tuple[datetime, datetime]], window_start: datetime, window_end: datetime
) -> int:
    """Highest number of overlapping spans inside the observation window."""
    boundaries: list[tuple[datetime, int]] = []
    for start, end in spans:
        start, end = max(start, window_start), min(end, window_end)
        if end <= start:
            continue
        boundaries.append((start, 1))
        boundaries.append((end, -1))
    boundaries.sort()
    peak = current = 0
    for _at, delta in boundaries:
        current += delta
        peak = max(peak, current)
    return peak


def _lifecycle(
    events: list[_Event], created_at: datetime, end: datetime
) -> list[tuple[str, datetime, datetime]]:
    """Pending/running spans for one queued job, in sequence order.

    A claimed span that ends in ``worker_recovered`` includes the worker's down
    time until recovery; this is the claimed time a single worker had work.
    """
    spans: list[tuple[str, datetime, datetime]] = []
    state, since = "pending", created_at
    for event in events:
        if event.event_type in CLAIM_EVENT_TYPES:
            if state != "pending":
                continue
            spans.append((state, since, event.created_at))
            state, since = "running", event.created_at
        elif event.event_type in WAIT_EVENT_TYPES:
            if state != "running":
                continue
            spans.append((state, since, event.created_at))
            state, since = "pending", event.created_at
        elif event.event_type in TERMINAL_EVENT_TYPES:
            spans.append((state, since, event.created_at))
            return spans
    spans.append((state, since, end))
    return spans


def _pending_since(spans: list[tuple[str, datetime, datetime]], created_at: datetime) -> datetime:
    for state, start, _end in reversed(spans):
        if state == "pending":
            return start
    return created_at


def _model_bucket() -> dict[str, Any]:
    return {"completed": 0, "errors": 0, "interrupted": 0, "in_flight": 0, "durations": []}


def collect_queue_metrics(
    database: Database, *, window_hours: float = 24.0, now: datetime | None = None
) -> dict[str, Any]:
    """Aggregate queue and model-call measurements from the persisted traces."""
    if window_hours <= 0:
        raise ValueError("window_hours must be positive")
    window_end = _utc(now) if now is not None else datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=window_hours)
    window_seconds = (window_end - window_start).total_seconds()

    with database.session() as session:
        jobs = list(session.scalars(select(QueueJob)))
        parents: dict[str, dict[str, Any]] = {}
        events: dict[str, dict[str, list[_Event]]] = {}
        for kind, (parent_model, event_model) in PARENT_MODELS.items():
            parents[kind] = {row.id: row for row in session.scalars(select(parent_model))}
            grouped: dict[str, list[_Event]] = {}
            for row in session.scalars(
                select(event_model).order_by(event_model.run_id, event_model.sequence)
            ):
                grouped.setdefault(row.run_id, []).append(
                    _Event(row.event_type, row.payload or {}, _utc(row.created_at), row.sequence)
                )
            events[kind] = grouped

    kind_by_run = {run_id: kind for kind, rows in parents.items() for run_id in rows}
    analysis: list[dict[str, Any]] = []
    for job in jobs:
        kind = kind_by_run.get(job.run_id)
        run = parents[kind].get(job.run_id) if kind else None
        job_events = events.get(kind, {}).get(job.run_id, []) if kind else []
        created_at = _utc(job.created_at)
        terminal_at = next(
            (event.created_at for event in job_events if event.event_type in TERMINAL_EVENT_TYPES),
            None,
        )
        if terminal_at is None and job.status in {"completed", "failed"}:
            terminal_at = _utc(job.updated_at)
        spans = _lifecycle(job_events, created_at, terminal_at or window_end)
        first_run = next((start for state, start, _end in spans if state == "running"), None)
        processing = sum(
            (end - start).total_seconds() for state, start, end in spans if state == "running"
        )
        analysis.append({
            "kind": kind or "unattributed",
            "status": job.status,
            "error_code": (run.error_code if run is not None else None) or job.last_error,
            "created_at": created_at,
            "terminal_at": terminal_at,
            "wait": (first_run - created_at).total_seconds() if first_run else None,
            "processing": processing,
            "end_to_end": (terminal_at - created_at).total_seconds() if terminal_at else None,
            "attempts": job.attempts,
            "spans": spans,
            "events": job_events,
        })

    duration_sample = [
        item for item in analysis
        if item["terminal_at"] is not None and window_start <= item["terminal_at"] <= window_end
    ]
    enqueued_sample = [
        item for item in analysis if window_start <= item["created_at"] <= window_end
    ]
    open_jobs = [item for item in analysis if item["status"] in {"pending", "running"}]
    pending_jobs = [item for item in open_jobs if item["status"] == "pending"]
    running_jobs = [item for item in open_jobs if item["status"] == "running"]

    by_type: dict[str, dict[str, int]] = {}
    for item in enqueued_sample:
        by_type.setdefault(item["kind"], {"enqueued": 0, "completed": 0, "failed": 0})["enqueued"] += 1
    completed = [item for item in duration_sample if item["status"] == "completed"]
    failed = [item for item in duration_sample if item["status"] == "failed"]
    for item in duration_sample:
        bucket = by_type.setdefault(item["kind"], {"enqueued": 0, "completed": 0, "failed": 0})
        if item["status"] == "completed":
            bucket["completed"] += 1
        elif item["status"] == "failed":
            bucket["failed"] += 1
    terminal_count = len(completed) + len(failed)

    failures_by_error: dict[str, int] = {}
    for item in failed:
        error_code = item["error_code"] or "unknown"
        failures_by_error[error_code] = failures_by_error.get(error_code, 0) + 1

    recovery_waits: list[float] = []
    recovered_jobs = 0
    for item in duration_sample:
        recovered_here = False
        for index, event in enumerate(item["events"]):
            if event.event_type not in RECOVERY_EVENT_TYPES:
                continue
            recovered_here = True
            next_claim = next(
                (
                    candidate for candidate in item["events"][index + 1:]
                    if candidate.event_type in CLAIM_EVENT_TYPES
                ),
                None,
            )
            if next_claim is not None:
                recovery_waits.append((next_claim.created_at - event.created_at).total_seconds())
        recovered_jobs += int(recovered_here)

    claimed_spans = [
        (start, end) for item in analysis for state, start, end in item["spans"] if state == "running"
    ]
    pending_spans = [
        (start, end) for item in analysis for state, start, end in item["spans"] if state == "pending"
    ]
    backlog_spans = [
        (item["created_at"], item["terminal_at"] or window_end) for item in analysis
    ]
    backlog_seconds = _merged_seconds(backlog_spans, window_start, window_end)
    claimed_seconds = _merged_seconds(claimed_spans, window_start, window_end)

    step = max((window_end - window_start) / 24, timedelta(minutes=1))
    sample_count = max(1, math.ceil(window_seconds / step.total_seconds()))
    depth_series: list[dict[str, Any]] = []
    for index in range(sample_count + 1):
        at = min(window_start + index * step, window_end)
        pending_at = [
            start for item in analysis for state, start, end in item["spans"]
            if state == "pending" and start <= at < end
        ]
        running_at = [
            start for item in analysis for state, start, end in item["spans"]
            if state == "running" and start <= at < end
        ]
        oldest = max((at - start).total_seconds() for start in pending_at) if pending_at else None
        depth_series.append({
            "at": at.isoformat(),
            "pending": len(pending_at),
            "running": len(running_at),
            "oldest_pending_age_seconds": round(oldest, 3) if oldest is not None else None,
        })
        if at >= window_end:
            break

    bucket_count = max(1, math.ceil(window_seconds / 3600))
    enqueue_buckets = [0] * bucket_count
    completion_buckets = [0] * bucket_count
    for item in enqueued_sample:
        index = min(bucket_count - 1, int((item["created_at"] - window_start).total_seconds() // 3600))
        enqueue_buckets[index] += 1
    for item in completed:
        index = min(bucket_count - 1, int((item["terminal_at"] - window_start).total_seconds() // 3600))
        completion_buckets[index] += 1

    model_overall = _model_bucket()
    model_by_provider: dict[str, dict[str, Any]] = {}
    model_by_phase: dict[str, dict[str, Any]] = {}

    def record_model(start: _Event, outcome: str, duration: float | None = None) -> None:
        provider = str(start.payload.get("provider") or "unknown")
        phase = str(start.payload.get("phase") or "unknown")
        for bucket in (
            model_overall,
            model_by_provider.setdefault(provider, _model_bucket()),
            model_by_phase.setdefault(phase, _model_bucket()),
        ):
            bucket[outcome] += 1
            if duration is not None:
                bucket["durations"].append(duration)

    for kind, grouped in events.items():
        for run_id, run_events in grouped.items():
            run = parents[kind].get(run_id)
            stack: list[_Event] = []
            for event in run_events:
                if event.event_type in MODEL_CALL_START_TYPES:
                    if window_start <= event.created_at <= window_end:
                        stack.append(event)
                elif event.event_type in MODEL_CALL_END_TYPES:
                    start = stack.pop() if stack else None
                    if start is not None:
                        record_model(start, "completed", (event.created_at - start.created_at).total_seconds())
            for start in stack:
                if run is not None and run.status == "running":
                    record_model(start, "in_flight")
                elif (run is not None and run.error_code == "worker_interrupted") or any(
                    event.event_type in RECOVERY_EVENT_TYPES and event.created_at >= start.created_at
                    for event in run_events
                ):
                    record_model(start, "interrupted")
                else:
                    record_model(start, "errors")

    def finish_model(bucket: dict[str, Any]) -> dict[str, Any]:
        return {
            "completed": bucket["completed"],
            "errors": bucket["errors"],
            "interrupted": bucket["interrupted"],
            "in_flight": bucket["in_flight"],
            "duration_seconds": _summary(bucket["durations"]),
        }

    return {
        "generated_at": window_end.isoformat(),
        "observation_window": {
            "hours": window_hours,
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "queue": {
            "current": {
                "pending": len(pending_jobs),
                "running": len(running_jobs),
                "oldest_pending_age_seconds": (
                    round(
                        max(
                            (window_end - _pending_since(item["spans"], item["created_at"])).total_seconds()
                            for item in pending_jobs
                        ),
                        3,
                    )
                    if pending_jobs
                    else None
                ),
            },
            "workload": {
                "enqueued": len(enqueued_sample),
                "completed": len(completed),
                "failed": len(failed),
                "terminal_failure_rate": (
                    round(len(failed) / terminal_count, 3) if terminal_count else None
                ),
                "by_type": by_type,
            },
            "wait_seconds": _summary([item["wait"] for item in duration_sample if item["wait"] is not None]),
            "processing_seconds": _summary(
                [item["processing"] for item in duration_sample if item["wait"] is not None]
            ),
            "end_to_end_seconds": _summary(
                [item["end_to_end"] for item in duration_sample if item["end_to_end"] is not None]
            ),
            "attempts": {
                "attempted_jobs": len(duration_sample),
                "total_attempts": sum(item["attempts"] for item in duration_sample),
                "retried_jobs": sum(1 for item in duration_sample if item["attempts"] > 1),
            },
            "recovery": {
                "recovered_jobs": recovered_jobs,
                "resume_wait_seconds": _summary(recovery_waits),
                "exhausted_jobs": sum(
                    1 for item in failed if item["error_code"] == "worker_interrupted"
                ),
            },
            "failures": {"by_error": failures_by_error},
            "backlog": {
                "seconds": round(backlog_seconds, 3),
                "peak_pending": _peak_concurrency(pending_spans, window_start, window_end),
                "peak_running": _peak_concurrency(claimed_spans, window_start, window_end),
            },
            "throughput": {
                "enqueued_per_hour": round(len(enqueued_sample) / window_hours, 3),
                "completed_per_hour": round(len(completed) / window_hours, 3),
                "peak_enqueued_per_hour": max(enqueue_buckets, default=0),
                "peak_completed_per_hour": max(completion_buckets, default=0),
            },
            "worker": {
                "concurrency": 1,
                "claimed_seconds": round(claimed_seconds, 3),
                "utilization": round(claimed_seconds / window_seconds, 6),
            },
            "depth_series": depth_series,
        },
        "model_calls": {
            **finish_model(model_overall),
            "by_provider": {
                name: finish_model(bucket) for name, bucket in sorted(model_by_provider.items())
            },
            "by_phase": {
                name: finish_model(bucket) for name, bucket in sorted(model_by_phase.items())
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(
        description="Print queue and model-call measurements as JSON (read-only)."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=f"Defaults to AGENT_RUNTIME_DATABASE_URL or {DEFAULT_DATABASE_URL}.",
    )
    parser.add_argument(
        "--window-hours", type=float, default=24.0, help="Observation window in hours (default 24)."
    )
    args = parser.parse_args(argv)
    database = Database(args.database_url or os.getenv("AGENT_RUNTIME_DATABASE_URL", DEFAULT_DATABASE_URL))
    try:
        report = collect_queue_metrics(database, window_hours=args.window_hours)
    except Exception as exc:
        print(f"queue metrics failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        database.dispose()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
