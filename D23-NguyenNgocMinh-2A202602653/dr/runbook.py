"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import datetime
import json
import math
import os
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {
    "a": os.environ.get("REGION_A_URL", "http://127.0.0.1:8001"),
    "b": os.environ.get("REGION_B_URL", "http://127.0.0.1:8002"),
}


def step(n, name, **kw):
    """Append một bước runbook vào JSONL với timestamp Unix và ISO-8601."""
    event = {
        "ts": time.time(),
        "iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False))
    return event


def confirm(auto: bool, msg: str) -> bool:
    """Tự xác nhận trong CI; nếu không thì yêu cầu operator trả lời y/N."""
    if auto:
        return True
    print(msg)
    try:
        answer = input("Tiếp tục failover? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _latest_outage(primary: str):
    """Tìm mốc kill gần nhất của primary trong log chaos, nếu có."""
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "kill" and event.get("region") == primary:
            return event
    return None


def _golden_signals(target: str) -> dict:
    """Gửi 10 request inference thật trực tiếp tới region đích."""
    latencies_ms = []
    failures = []
    for index in range(10):
        started = time.perf_counter()
        try:
            response = httpx.get(
                f"{URL[target]}/v1/infer",
                params={"q": f"golden signal request {index + 1}"},
                timeout=3.0,
            )
            latencies_ms.append((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                failures.append({"request": index + 1,
                                 "reason": f"HTTP {response.status_code}"})
            else:
                try:
                    body = response.json()
                except ValueError:
                    failures.append({"request": index + 1,
                                     "reason": "invalid JSON response"})
                else:
                    if body.get("error"):
                        failures.append({"request": index + 1,
                                         "reason": body["error"]})
        except httpx.RequestError as exc:
            latencies_ms.append((time.perf_counter() - started) * 1000)
            failures.append({"request": index + 1,
                             "reason": f"{type(exc).__name__}: {exc}"})

    ordered = sorted(latencies_ms)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None
    return {
        "requests_expected": 10,
        "requests_completed": len(latencies_ms),
        "successes": 10 - len(failures),
        "errors": len(failures),
        "error_rate": len(failures) / 10,
        "p95_latency_ms": round(p95, 1) if p95 is not None else None,
        "latencies_ms": [round(value, 1) for value in latencies_ms],
        "failures": failures,
    }


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực hiện runbook incident response bán tự động qua 7 bước."""
    if primary not in URL or target not in URL:
        raise ValueError("primary and target must be 'a' or 'b'")
    if primary == target:
        raise ValueError("primary and target must be different regions")
    if backend not in {"fs", "minio"}:
        raise ValueError("backend must be 'fs' or 'minio'")

    # 1. Confirm outage through repeated probes of both regions.
    probe_results = {region: [] for region in (primary, target)}
    for attempt in range(3):
        for region in (primary, target):
            ready, reason = hc.probe(region, timeout=1.0)
            probe_results[region].append({"ready": ready, "reason": reason})
        if attempt < 2:
            time.sleep(0.2)

    primary_outage_confirmed = not any(
        result["ready"] for result in probe_results[primary])
    target_ready_probes = sum(result["ready"] for result in probe_results[target])
    outage_event = _latest_outage(primary)
    outage_ts = outage_event.get("ts") if outage_event else None
    outage_source = "chaos/chaos-events.jsonl" if outage_event else "probe_confirmation"
    if outage_ts is None:
        outage_ts = time.time()

    step(1, "xac_nhan_outage", primary=primary, target=target,
         primary_outage_confirmed=primary_outage_confirmed,
         probe_results=probe_results, target_ready_probes=target_ready_probes,
         outage_ts=outage_ts, outage_source=outage_source)

    # 2. Announce the incident and start/record the RTO clock.
    incident_ts = time.time()
    step(2, "thong_bao_incident", primary=primary, target=target,
         t_outage=outage_ts, outage_source=outage_source,
         incident_ts=incident_ts,
         notification_delay_s=round(max(0.0, incident_ts - outage_ts), 2))

    if not primary_outage_confirmed:
        reason = f"region-{primary} did not fail all three readiness probes"
        for n, name in ((3, "scale_gpu_pool"), (4, "verify_state_replica"),
                        (5, "dns_cutover"), (6, "verify_golden_signals")):
            step(n, name, skipped=True, reason=reason)
        elapsed_s = round(max(0.0, time.time() - outage_ts), 2)
        step(7, "post_incident", ok=False, reason=reason,
             rto_started_ts=outage_ts, incident_ts=incident_ts,
             elapsed_s=elapsed_s,
             measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl")
        return {"ok": False, "reason": reason, "failover": None,
                "golden_signals": None, "elapsed_s": elapsed_s}

    # Semi-automatic gate: no failover action occurs until operator confirms.
    if not confirm(auto, f"Đã xác nhận region-{primary} outage. "
                         f"Chuẩn bị failover sang region-{target}."):
        reason = "operator declined or did not confirm failover"
        for n, name in ((3, "scale_gpu_pool"), (4, "verify_state_replica"),
                        (5, "dns_cutover"), (6, "verify_golden_signals")):
            step(n, name, skipped=True, reason=reason)
        elapsed_s = round(max(0.0, time.time() - outage_ts), 2)
        step(7, "post_incident", ok=False, reason=reason,
             rto_started_ts=outage_ts, incident_ts=incident_ts,
             elapsed_s=elapsed_s,
             measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl")
        return {"ok": False, "reason": reason, "failover": None,
                "golden_signals": None, "elapsed_s": elapsed_s}

    # 3. Trigger the five-step failover exactly once.
    try:
        failover_result = fo.failover(target, backend, wait=60.0)
    except Exception as exc:
        failover_result = {"ok": False, "reason": f"{type(exc).__name__}: {exc}",
                           "steps_completed": []}
    step(3, "scale_gpu_pool", failover_ok=bool(failover_result.get("ok")),
         failover_result=failover_result)

    # 4. Verify using only failover()'s returned evidence; do not invoke it again.
    completed_steps = failover_result.get(
        "steps_completed", failover_result.get("steps", []))
    state_replica_ok = (
        bool(failover_result.get("ok"))
        and "2_restore_snapshot" in completed_steps
        and "3_scale_pool" in completed_steps
    )
    replica_evidence = {
        "rpo_seconds": failover_result.get("rpo_seconds"),
        "docs_lost": failover_result.get("docs_lost"),
        "embed_model_version": failover_result.get("embed_model_version"),
        "waited_s": failover_result.get("waited_s"),
        "steps_completed": completed_steps,
    }
    step(4, "verify_state_replica", ok=state_replica_ok,
         evidence=replica_evidence)

    # 5. Trust the completed cutover step reported by failover(), without rerunning it.
    dns_cutover_ok = (
        bool(failover_result.get("ok"))
        and "5_dns_cutover" in completed_steps
    )
    step(5, "dns_cutover", ok=dns_cutover_ok, target=target,
         reason=failover_result.get("reason"))

    # 6. Exercise the recovered region with ten real inference requests.
    if dns_cutover_ok:
        golden_signals = _golden_signals(target)
    else:
        golden_signals = {
            "requests_expected": 10, "requests_completed": 0,
            "successes": 0, "errors": 0, "error_rate": None,
            "p95_latency_ms": None, "latencies_ms": [], "failures": [],
            "skipped": True,
        }
    step(6, "verify_golden_signals", **golden_signals)

    run_ok = dns_cutover_ok and golden_signals.get("error_rate") == 0
    elapsed_s = round(max(0.0, time.time() - outage_ts), 2)
    step(7, "post_incident", ok=run_ok, rto_started_ts=outage_ts,
         incident_ts=incident_ts, elapsed_s=elapsed_s,
         measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl")
    return {"ok": run_ok, "failover": failover_result,
            "golden_signals": golden_signals, "elapsed_s": elapsed_s,
            "rto_started_ts": outage_ts, "incident_ts": incident_ts}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
