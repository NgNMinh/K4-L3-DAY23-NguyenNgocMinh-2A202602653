"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import datetime
import json
import os
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {
    "a": os.environ.get("REGION_A_URL", "http://127.0.0.1:8001"),
    "b": os.environ.get("REGION_B_URL", "http://127.0.0.1:8002"),
}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append một sự kiện JSONL có timestamp vào log và in ra stdout."""
    event = {
        "ts": time.time(),
        "iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False))
    return event


def state_of(region: str, timeout: float = 5.0) -> dict:
    """Đọc trạng thái hiện tại của một region qua /v1/state."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=timeout)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Thực hiện verify, restore, scale, readiness check rồi mới cutover."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unknown snapshot backend: {backend}")
    if wait < 0:
        raise ValueError("wait must be non-negative")

    completed = []

    def result(ok: bool, **extra) -> dict:
        return {
            "ok": ok,
            "rpo_seconds": extra.pop("rpo_seconds", None),
            "docs_lost": extra.pop("docs_lost", None),
            "steps_completed": list(completed),
            "steps": list(completed),
            **extra,
        }

    # Step 1: capture current state of target region.
    try:
        target_state = state_of(target)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        reason = f"could not verify target: {exc}"
        emit(step="1_verify_target", region=target, ok=False, reason=reason)
        return result(False, reason=reason)
    emit(step="1_verify_target", region=target, ok=True, state=target_state)
    completed.append("1_verify_target")

    # Step 2: restore the latest replicated snapshot, then measure lost data.
    source = "b" if target == "a" else "a"
    try:
        restored = snapshot.get(target, backend)
        rpo_info = snapshot.rpo(
            pathlib.Path(f"state/region-{source}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        rpo_seconds = rpo_info.get("rpo_seconds")
        docs_lost = rpo_info.get("docs_lost")
        embed_model_version = restored.get("embed_model_version")
    except (Exception, SystemExit) as exc:
        reason = f"snapshot restore failed: {exc}"
        emit(step="2_restore_snapshot", region=target, backend=backend,
             ok=False, reason=reason)
        return result(False, reason=reason)
    emit(step="2_restore_snapshot", region=target, backend=backend, ok=True,
         rpo_seconds=rpo_seconds, docs_lost=docs_lost,
         embed_model_version=embed_model_version)
    completed.append("2_restore_snapshot")

    # Step 3: mark the target pool as full. The serving process observes this file.
    pool_state = pathlib.Path(f"state/region-{target}/pool_state")
    try:
        pool_state.parent.mkdir(parents=True, exist_ok=True)
        pool_state.write_text("full\n", encoding="utf-8")
    except OSError as exc:
        reason = f"could not scale target pool: {exc}"
        emit(step="3_scale_pool", region=target, ok=False, reason=reason)
        return result(False, rpo_seconds=rpo_seconds, docs_lost=docs_lost,
                      reason=reason)
    emit(step="3_scale_pool", region=target, ok=True, pool_state="full")
    completed.append("3_scale_pool")

    # Step 4: readiness must be confirmed before changing active_region.
    ready_started = time.monotonic()
    ready_deadline = ready_started + wait
    ready = False
    ready_reason = "timeout waiting for /readyz"
    while time.monotonic() < ready_deadline:
        remaining = ready_deadline - time.monotonic()
        try:
            response = httpx.get(
                f"{URL[target]}/readyz", timeout=remaining)
            if response.status_code == 200:
                ready = True
                ready_reason = "HTTP 200"
                break
            ready_reason = f"HTTP {response.status_code}"
        except httpx.TimeoutException as exc:
            ready_reason = f"request timeout: {exc}"
        except httpx.RequestError as exc:
            ready_reason = f"request error: {exc}"

        remaining = ready_deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))

    if not ready:
        reason = f"wait_ready timed out: {ready_reason}"
        waited_s = round(time.monotonic() - ready_started, 2)
        emit(step="4_wait_ready", region=target, ok=False, reason=reason,
             timeout_s=wait, waited_s=waited_s)
        return result(False, rpo_seconds=rpo_seconds, docs_lost=docs_lost,
                      waited_s=waited_s, reason=reason)
    waited_s = round(time.monotonic() - ready_started, 2)
    emit(step="4_wait_ready", region=target, ok=True, reason=ready_reason,
         timeout_s=wait, waited_s=waited_s)
    completed.append("4_wait_ready")

    # Step 5: cut traffic over only after readiness is confirmed.
    active_region = pathlib.Path("edge/active_region")
    try:
        active_region.parent.mkdir(parents=True, exist_ok=True)
        active_region.write_text(target, encoding="utf-8")
    except OSError as exc:
        reason = f"DNS cutover failed: {exc}"
        emit(step="5_dns_cutover", region=target, ok=False, reason=reason)
        return result(False, rpo_seconds=rpo_seconds, docs_lost=docs_lost,
                      reason=reason)
    emit(step="5_dns_cutover", region=target, ok=True)
    completed.append("5_dns_cutover")
    return result(True, rpo_seconds=rpo_seconds, docs_lost=docs_lost,
                  target_state=target_state, embed_model_version=embed_model_version,
                  waited_s=waited_s)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
