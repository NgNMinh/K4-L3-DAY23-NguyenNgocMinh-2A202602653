# Runbook - Region chính down

## Phạm vi và điều kiện

Stack chạy local, snapshot backend `fs`. Xác nhận Region B còn sống và snapshot mới nhất đã replicate trước khi xử lý. Trên máy Windows của drill này, MiniTool ShadowMaker đang chiếm port `8080`; dùng A `http://127.0.0.1:8003`, B `http://127.0.0.1:8004`, edge `http://127.0.0.1:8081`.

Mở PowerShell tại thư mục repo và đặt URL cho runbook, health checker, failover và chaos script:

```powershell
$env:REGION_A_URL = "http://127.0.0.1:8003"
$env:REGION_B_URL = "http://127.0.0.1:8004"
```

## 7 bước xử lý

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner | Điều kiện / hành động rollback |
|---|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | JSON cho thấy `a.alive=false`; `b.alive=true`. Lặp lại sau 5 giây để xác nhận, không kết luận từ một probe lỗi. | On-call | Nếu A vẫn alive hoặc B không alive, dừng failover; xác minh outage và khôi phục B trước. |
| 2 | Mở incident và bắt đầu clock | `python dr/runbook.py --primary a --target b --backend fs` | Script log `thong_bao_incident`, sau đó hỏi `y/N`. Chỉ nhập `y` sau khi đã xác nhận outage; `n` sẽ hủy failover. | Incident commander | Nếu chưa xác nhận được outage hoặc operator chọn `n`, giữ nguyên active region và tiếp tục điều tra. |
| 3 | Restore snapshot, scale pool và đợi ready | `Get-Content reports/failover-events.jsonl -Tail 5` | Cùng một lần chạy bước 2, log có đúng thứ tự `1_verify_target` đến `5_dns_cutover`; `4_wait_ready` phải `ok=true` trước cutover. | On-call / Platform | Nếu restore, scale hoặc `/readyz` timeout, không cutover. Giữ nguyên traffic và xử lý target; không gọi failover lần hai để thử. |
| 4 | Xác minh replica | `python -c "import json; print([e for e in (json.loads(x) for x in open('reports/runbook-run.jsonl', encoding='utf-8')) if e.get('name') == 'verify_state_replica'])"` | Event `ok=true`, có `rpo_seconds`, `docs_lost`, `embed_model_version`; `steps_completed` có restore và scale. | Data on-call | Nếu bằng chứng thiếu/sai, ghi nhận data gap, tạm dừng ghi thêm dữ liệu và báo Incident Commander; không tự động failback. |
| 5 | Kiểm tra DNS/LB cutover | `Invoke-RestMethod http://127.0.0.1:8081/edge/state` | `active_region` là `b`; log `5_dns_cutover` có `ok=true`. | On-call | Nếu edge không route đến B hoặc B không ready, giữ incident mở; chỉ failback sau khi A ready và Incident Commander cho phép. |
| 6 | Kiểm tra golden signals | `python -c "import json; print([e for e in (json.loads(x) for x in open('reports/runbook-run.jsonl', encoding='utf-8')) if e.get('name') == 'verify_golden_signals'])"` | Đủ 10 request inference; `successes=10`, `error_rate=0`; xem `p95_latency_ms`. | Service owner | Nếu có request lỗi, tiếp tục incident trên B và báo Incident Commander; không tự động chuyển traffic qua lại giữa hai region. |
| 7 | Đo RTO/RPO và chốt postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid=true`, `warnings=[]`, `rto_verdict=PASS`; cập nhật số và evidence vào hai report. | Incident commander | Nếu log invalid hoặc không đo được RTO, giữ nguyên raw logs, chưa nộp số liệu; tạo drill mới có log riêng sau khi sửa nguyên nhân. |

Bước 3-7 được runbook ghi trong cùng một lần chạy bước 2. Các lệnh ở các bước này chỉ đọc kết quả. Không chạy lại bước 2 để "thử" sau khi đã cutover.

## Quyền và điều kiện failback

Nếu Region B không đạt `/readyz` trước timeout, giữ nguyên `edge/active_region`; failover code sẽ không cutover. On-call tiếp tục xử lý incident và khôi phục region. Chỉ chuyển traffic từ B về A khi A đã khôi phục, `/readyz` trả 200 ba lần liên tiếp, model version và vector state đã được đối chiếu, và Incident Commander chấp thuận. Không tự động failback: replication trong lab này một chiều từ A sang B, nên phải xác nhận dữ liệu trước khi đổi traffic ngược.
