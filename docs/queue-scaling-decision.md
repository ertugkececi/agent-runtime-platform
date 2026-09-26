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

## Next implementation step

The next implementation slice is **Auth/principal** in the [security design](inbound-a2a-multiuser-security.md): OIDC server-side login/callback/logout, session handling, CSRF protection, and principal/scopes. The security design remains documentation until its implementation slices are delivered; this decision makes no runtime or schema changes.
