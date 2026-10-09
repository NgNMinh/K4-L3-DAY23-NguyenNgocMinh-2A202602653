# Postmortem - DR Drill Lab 23

Date: 2026-10-09. This analysis uses the final DR run data and does not assign individual blame.

## 1. Timeline

| ISO time (UTC) | Event | Evidence |
|---|---|---|
| 2026-10-09 10:07:03 | Region A intentionally stopped; Region B stayed alive | `chaos/chaos-events.jsonl:2` |
| 2026-10-09 10:07:03.793 | First request failed | `reports/drill-2-withdr.jsonl:24` |
| 2026-10-09 10:07:19.221 | Health checker marked A `UNHEALTHY` after 3 consecutive failures | `reports/health-events.jsonl:2` |
| 2026-10-09 10:07:25.131 | Runbook opened incident; `--auto` was used, so no human confirmation was needed | `reports/runbook-run.jsonl:2` |
| 2026-10-09 10:07:31.594 | Edge cut over to B after B was ready | `reports/failover-events.jsonl:5` |
| 2026-10-09 10:07:35.330 | First successful request served by B; RTO 31.7 s | `reports/drill-2-withdr.jsonl:38` |

## 2. RTO/RPO against targets

- RTO target: 300 s; measured: **31.7 s**; 268.3 s below target.
- RPO target: 300 s; measured: **6.0 s**, with **3 documents** lost; 294.0 s below target.
- **Largest contributor:** health-check detection at **15.6 s**, from outage until 3 consecutive failures were observed.
- Golden signals after cutover: 10/10 requests succeeded, error rate 0%, p95 312.6 ms (`reports/runbook-run.jsonl:6`).

### RTO breakdown

| Component | Seconds |
|---|---:|
| Health detection | 15.6 |
| Alert until runbook begins target verification | 6.1 |
| Snapshot restore until pool scale | 0.0 |
| Pool warm-up | 6.2 |
| DNS/LB cache after cutover | 3.8 |
| **Total** | **31.7** |

### Gap analysis

The measured RTO is 268.3 seconds below its 300-second target. The measured RPO is 294.0 seconds below its 300-second target. The remaining operational gap is the 3 documents newer than the restored snapshot. Health detection is the largest measured part of the RTO.

## 3. Root cause - 5 whys

1. Why did users see errors? The edge continued routing to Region A after process A stopped.
2. Why did the edge not switch immediately? It uses an active-region pointer that changes only after the runbook confirms Region B is ready.
3. Why did outage confirmation take time? The health checker polls every 5 seconds and requires 3 consecutive failures to reduce flapping.
4. Why was there additional time after the alert? The runbook verifies the target, restores a snapshot, and waits for B pool warm-up (6.2 seconds).
5. Why did requests not succeed immediately after cutover? The edge cache added 3.8 seconds before the first request passed through B.

## 4. Action items

| # | Action | Owner | Deadline | Expected effect |
|---|---|---|---|---|
| 1 | Try a 2-second interval, keep threshold 3, and measure false alerts before adoption | SRE | Next drill | Detection floor can fall from 15 to 6 seconds; up to about 9.6 seconds saved in this run |
| 2 | Evaluate keeping pool B warm; document compute cost | Platform | Before next monthly drill | May save about 6.2 seconds; increases idle resource cost |
| 3 | Test edge TTL of 2 seconds and check that route errors do not increase | Networking | Next drill | May save up to about 1.8 seconds under similar conditions |

## 5. Required questions

1. `interval x threshold = 5 x 3 = 15 s`, about **47.3%** of the 31.7-second RTO.
2. With interval reduced to 1 second, the detection floor becomes `1 x 3 = 3 s`, a theoretical 12-second reduction. The checker would probe 5 times more often and could see more transient failures; threshold 3 still helps reduce flapping.
3. `docs_lost=3` means three documents/tickets written to primary after the latest snapshot were missing from the replica. For customers, those three latest updates need to be resent or reconciled after recovery.
