# RTO/RPO Evidence - Lab 23

All figures below come from the 2026-10-09 drill logs. Baseline did not recover; the DR drill recovered in 31.7 seconds with RPO 6.0 seconds.

## 1. Drill 1 - no DR (baseline)

| Metric | Value | Measurement | Evidence |
|---|---:|---|---|
| t_outage | 2026-10-09T09:59:42Z | `action: kill`, Region A | `chaos/chaos-events.jsonl:1` |
| First failed request | +0.6 s | First failed request after outage | `reports/drill-1-nodr.jsonl:18` |
| Later successful request | None | No OK request after outage | `reports/measure-drill-1.json` |
| RTO | NO_RECOVERY | No recovery during loadgen window | `reports/measure-drill-1.json` |

## 2. Drill 2 - with DR

| Milestone | Seconds from t_outage | Measurement | Evidence |
|---|---:|---|---|
| t_outage | 0.0 s | `action: kill`, Region A; mode `stop` | `chaos/chaos-events.jsonl:2` |
| First user-visible error | 0.1 s | First failed request | `reports/drill-2-withdr.jsonl:24` |
| Health checker detects A | 15.6 s | 5 s interval x 3 consecutive failures | `reports/health-events.jsonl:2` |
| Snapshot restore complete | +21.7 s | RPO 6.0 s, 3 docs lost | `reports/failover-events.jsonl:2` |
| Region B ready | +27.9 s | `/readyz` returns HTTP 200; warm-up about 6.2 s | `reports/failover-events.jsonl:4` |
| DNS/LB cutover | +27.9 s | Done only after B is ready | `reports/failover-events.jsonl:5` |
| First successful request from B | +31.7 s | RTO from request log, `served_by=b` | `reports/drill-2-withdr.jsonl:38` |

| Metric | Measured | Target | Result |
|---|---:|---:|---|
| RTO - Inference API | 31.7 s | 300 s | PASS; 268.3 s below target |
| RPO - Vector DB | 6.0 s / 3 docs | 300 s | PASS; 3 newest documents absent from snapshot |

## 3. RTO breakdown

| Component | Seconds | Evidence | Possible improvement |
|---|---:|---|---|
| Health-check detection | 15.6 s | `interval_s=5`, `threshold=3`; `reports/health-events.jsonl:2` | Reduce interval if more frequent probes are acceptable |
| Alert to runbook start/confirmation | 6.1 s | `verify_target` after health alert: `reports/failover-events.jsonl:1` | Shorten confirmation and runbook startup |
| Snapshot restore to scale | 0.0 s | Step 2 to 3 timestamp delta is below 0.01 s: `reports/failover-events.jsonl:2` and `:3` | Optimize snapshot size for larger data |
| GPU pool warm-up | 6.2 s | Step 3 to 4 timestamp delta: `reports/failover-events.jsonl:3` and `:4` | Keep B warm/full if compute cost allows |
| DNS/LB cache after cutover | 3.8 s | RTO 31.7 s minus cutover 27.9 s; `reports/measure-drill-2.json` | Reduce TTL/cache duration |
| **Total RTO** | **31.7 s** | `reports/measure-drill-2.json` | - |

Full RTO/RPO measurements and validation status are in `reports/measure-drill-1.json` and `reports/measure-drill-2.json`. The drill used `--mode stop` because Windows does not support POSIX `SIGSTOP`; both kills record `other_alive: true`, so neither caused a double outage.
