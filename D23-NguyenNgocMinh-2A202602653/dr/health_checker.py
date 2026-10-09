"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import os
import pathlib
import time

import httpx

URL = {
    "a": os.environ.get("REGION_A_URL", "http://127.0.0.1:8001"),
    "b": os.environ.get("REGION_B_URL", "http://127.0.0.1:8002"),
}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Kiểm tra endpoint /readyz của một region và trả về kết quả cùng lý do."""
    base_url = URL.get(region)
    if base_url is None:
        return False, f"unknown region: {region}"

    try:
        response = httpx.get(f"{base_url}/readyz", timeout=timeout)
    except httpx.TimeoutException as exc:
        return False, f"timeout: {exc}"
    except httpx.RequestError as exc:
        return False, f"request error: {exc}"

    if response.status_code == 200:
        return True, "HTTP 200"
    return False, f"HTTP {response.status_code}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll cả hai region, chống flapping và ghi JSONL khi trạng thái đổi."""
    if interval < 0:
        raise ValueError("interval must be non-negative")
    if threshold < 1:
        raise ValueError("threshold must be at least 1")
    if duration < 0:
        raise ValueError("duration must be non-negative")

    regions = tuple(URL)
    states = {region: "HEALTHY" for region in regions}
    consecutive_fails = {region: 0 for region in regions}
    deadline = time.monotonic() + duration
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    while time.monotonic() < deadline:
        cycle_started = time.monotonic()
        for region in regions:
            ready, reason = probe(region, timeout)

            if ready:
                consecutive_fails[region] = 0
                if states[region] == "UNHEALTHY":
                    states[region] = "HEALTHY"
                    event = {
                        "event": "state_change",
                        "ts": time.time(),
                        "region": region,
                        "to": "HEALTHY",
                        "reason": reason,
                        "interval_s": interval,
                        "threshold": threshold,
                        "consecutive_fails": 0,
                    }
                    with out.open("a", encoding="utf-8") as log:
                        log.write(json.dumps(event, ensure_ascii=False) + "\n")
            else:
                consecutive_fails[region] += 1
                if (states[region] == "HEALTHY"
                        and consecutive_fails[region] >= threshold):
                    states[region] = "UNHEALTHY"
                    event = {
                        "event": "state_change",
                        "ts": time.time(),
                        "region": region,
                        "to": "UNHEALTHY",
                        "reason": reason,
                        "interval_s": interval,
                        "threshold": threshold,
                        "consecutive_fails": consecutive_fails[region],
                    }
                    with out.open("a", encoding="utf-8") as log:
                        log.write(json.dumps(event, ensure_ascii=False) + "\n")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        cycle_elapsed = time.monotonic() - cycle_started
        time.sleep(min(max(0.0, interval - cycle_elapsed), remaining))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
