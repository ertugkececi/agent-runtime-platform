# Queue scaling decision

**Decision date:** 2026-09-26 18:32 UTC

**Status:** Keep the current SQLite queue and one worker on the application host. Revisit scaling only when measured workload or an explicit operating requirement calls for it.

## Evidence at decision time

A read-only query of the live SQLite `queue_jobs` table found 6 jobs: 5 completed, 1 failed, and 0 pending or running. The deployment uses one host and one worker. This small snapshot describes the current deployment only; it does not establish production-scale demand, latency percentiles, or sustained throughput. No latency statistics are inferred from it.

There is no measured load case here that justifies Redis Streams, NATS, a second worker, or another queue service. Adding one now would introduce operating and recovery costs without evidence that it addresses a current constraint. The existing queue behavior and limits are described in the [README](../README.md).

## Re-evaluation gate

Reopen the decision when any of these is observed or required:

- Real, regular use produces a persistent backlog or queue wait that misses an explicitly agreed service target.
- A measured load test shows the single worker cannot meet the agreed throughput or queue-wait target at expected peak volume.
- Availability or deployment requirements call for work to continue across host loss, or for workers on multiple hosts.
- The single-host operating boundary otherwise blocks a concrete product or operations requirement.

Before choosing a replacement, collect measurements over representative real use and run a workload test. Record at least:

- Queue depth over time, including pending and running jobs, oldest pending age, and backlog duration.
- Enqueue-to-start wait time percentiles and end-to-end completion time percentiles, with the observation window and workload stated.
- Enqueue and completion rates / throughput, including peak periods and job mix.
- Worker utilization and concurrency, plus processing duration by job type where available.
- Retry counts, attempt counts, failure rate, terminal failures, and recovery time after worker interruption.

Set the service target and the observation window before interpreting these metrics. Scale only if representative measurements miss that target or a stated multi-host/high-availability requirement cannot be met by the current design. Compare candidate systems on delivery and retry semantics, persistence and recovery, deployment complexity, operational ownership, and total cost; select Redis Streams, NATS, or another option only after that comparison. Do not promote a single day's handful of jobs into a latency or capacity claim.

## Measurement report

`agent-runtime-queue-metrics` reads the persisted `queue_jobs` rows and the run
event traces and prints the quantities above as JSON over an observation window
(`--window-hours`, default 24; alongside the API and worker, from the same
database configuration):

    uv run --locked agent-runtime-queue-metrics --window-hours 168 > queue-metrics.json

The report contains queue depth samples with oldest pending age, enqueue-to-start
wait, processing and end-to-end duration percentiles, throughput and job mix,
attempts, retries, recovery and resume waits, and model-call durations and error
counts. It is read-only. It carries counts, statuses, timings, provider/phase
names and coarse error categories only — never message content, prompts, tool
arguments, provider configuration, credentials or error messages. Durations
describe jobs that reached a terminal state inside the window; model-call
durations describe calls started inside it. Each report states its window and
sample counts, so no handful of jobs is promoted into a latency claim by itself.

## Next implementation step

The measurement report above is the last piece of the security and assurance
epic (**#69**). The auth/principal and offline tenant-migration slices have
since landed default-off; their live OIDC, PostgreSQL and restore verification
remain [release gates](release-gates.md). This decision still makes no runtime
or schema changes.
