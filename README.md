# Hệ thống xếp lịch thi tín chỉ (ExamScheduling)

Phần mềm xếp lịch thi cho trường đại học hệ tín chỉ, hỗ trợ cán bộ phòng đào tạo:
xếp lịch nhanh – đúng ràng buộc – tự tách môn lớn – tự repair khi đổi lịch thủ công.

> **Phiên bản v3 (2026-05)** – pipeline lai 4 lớp:
> **Diagnose → Greedy DSATUR (với load-balance & soft-cap) → LNS move-based → CP-SAT polish.**
> Luôn ra lịch khả thi với dataset 1,000+ môn × 16,000+ sinh viên, peak SV/ca có kiểm soát.

---

## 🎯 Mục lục

1. [Khởi động nhanh](#1-khởi-động-nhanh-cho-cán-bộ-xếp-lịch)
2. [File đầu vào](#2-file-đầu-vào)
3. [Hướng dẫn từng bước](#3-hướng-dẫn-từng-bước-trên-giao-diện)
4. [Hiểu các thông số trong sidebar](#4-hiểu-các-thông-số-trong-sidebar)
5. [Đọc kết quả & 6 tab](#5-đọc-kết-quả--6-tab)
6. [Kịch bản xử lý sự cố thường gặp](#6-kịch-bản-xử-lý-sự-cố-thường-gặp)
7. [Kiến trúc kỹ thuật](#7-kiến-trúc-kỹ-thuật-dành-cho-dev)
8. [Benchmark thực tế](#8-benchmark-thực-tế)
9. [Cấu trúc thư mục](#9-cấu-trúc-thư-mục)
10. [Hướng nâng cấp tiếp](#10-hướng-nâng-cấp-tiếp)

---

## 1. Khởi động nhanh (cho cán bộ xếp lịch)

### Cách A — Script tự động (khuyên dùng sau khi clone)

Cần **Python 3.10+** đã cài sẵn ([python.org](https://www.python.org/downloads/) — Windows nhớ tick *Add python.exe to PATH*).

| Hệ điều hành | Lệnh |
|---|---|
| **macOS / Linux** | `./setup.sh` hoặc `python3 setup.py` |
| **Windows (CMD)** | Double-click `setup.bat` hoặc `setup.bat` trong thư mục repo |
| **Windows / mọi nền** | `python setup.py` hoặc `py -3 setup.py` |

Script tạo `.venv`, nâng cấp `pip`, cài `requirements.txt`, rồi in lệnh chạy Streamlit.

### Cách B — Thủ công

```bash
# Cài đặt 1 lần
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Chạy app
.venv/bin/streamlit run app.py
```

Mở trình duyệt tại `http://localhost:8501`, upload 2 file (kế hoạch thi + danh sách đăng ký),
giữ nguyên các thiết lập mặc định, bấm **🚀 Xếp lịch thi**. Sau ~20–40 giây sẽ có lịch.

> 💡 Nếu lần đầu chưa quen, **chỉ cần upload 2 file rồi bấm chạy** — mọi tham số mặc định
> đã được tinh chỉnh cho dataset 1,000+ môn.

---

## 2. File đầu vào

### 2.1 ✅ Bắt buộc – Kế hoạch thi (`Ke_hoach_thi.xlsx`)

| Cột | Ý nghĩa |
|---|---|
| `Ngày BD` | Ngày bắt đầu đợt thi (datetime) |
| `Ngày kết thúc` | Ngày kết thúc đợt thi (datetime) |

Hệ thống lấy `min(Ngày BD)` → `max(Ngày kết thúc)` làm cửa sổ.

### 2.2 ✅ Bắt buộc – Danh sách SV đăng ký (`DSSV_*.xlsx`)

| Cột | Ý nghĩa |
|---|---|
| `MaHS` | Mã sinh viên |
| `TenSV` | Tên sinh viên |
| `MalopHP` | Mã lớp học phần |
| `TenLopHP` | Tên môn học |
| `SoTC` | Số tín chỉ |

Hệ thống tự suy luận:
- `CourseID` từ 12 ký tự đầu của `MalopHP`.
- `ExamType`: `theory` / `oral` / `computer` từ tên môn (qua từ khoá).
- **Môn thi chung**: bất kỳ môn nào có ≥ 2 lớp học phần → gộp 1 đề thi chung.

### 2.3 ⚙️ Tuỳ chọn – Phòng thi

| Cột | Bắt buộc | Ý nghĩa |
|---|---|---|
| `RoomID` | ✅ | Mã phòng |
| `Location` | ✅ | Vị trí (toà nhà / khu) |
| `Capacity` | ✅ | Sức chứa (số chỗ) |
| `RoomType` | – | `theory` / `computer` / `any` |

### 2.4 ⚙️ Tuỳ chọn – Giám thị

| Cột | Bắt buộc | Ý nghĩa |
|---|---|---|
| `InvigilatorID` | ✅ | Mã giám thị |
| `FullName` | ✅ | Họ tên |
| `MaxSessionsPerDay` | – | Số ca / ngày (mặc định 2) |
| `MaxSessionsTotal` | – | Tổng ca cả đợt (mặc định ∞) |

---

## 3. Hướng dẫn từng bước trên giao diện

### Bước 1: Upload file
1. Mở `http://localhost:8501`.
2. Vùng "1) Kế hoạch thi" → tải `Ke_hoach_thi.xlsx`.
3. Vùng "2) Danh sách SV đăng ký môn" → tải `DSSV_*.xlsx`.
4. (Tuỳ chọn) Upload phòng thi & giám thị.

### Bước 2: Kiểm tra cấu hình sidebar
Mặc định đã tốt — nhưng nên xem qua các expander:
- **Tham số học vụ** – `số ngày ôn / tín chỉ`, `max môn/SV/ngày`.
- **Bộ ca thi theo loại** – các ca cho lý thuyết / vấn đáp / máy tính (giữ mặc định).
- **Tách môn lớn** – ✅ **bật mặc định** với ngưỡng 1,500 SV.
- **Phân bố tải** – chọn mục tiêu tối ưu, soft cap, số vòng LNS.
- **Solver & tối ưu** – thời gian tối đa cho CP-SAT polish.
- **Phòng & giám thị** – số giám thị/phòng (mặc định 2).

### Bước 3: Bấm "🚀 Xếp lịch thi"
- Progress bar cho biết các pha: Greedy → LNS → CP-SAT (nếu instance nhỏ).
- Hoàn tất sẽ tự cuộn xuống phần kết quả.

### Bước 4: Kiểm tra kết quả qua 6 tab → xuất Excel
Xem mục [§5](#5-đọc-kết-quả--6-tab).

### Bước 5 (nếu cần): Đổi lịch thủ công
- Vào tab **✍️ Đổi lịch thủ công**.
- Chọn môn cần đổi → chọn ngày/ca mới → bấm **Áp dụng đổi lịch & repair**.
- Hệ thống chạy lại pipeline với ràng buộc `fixed_slots = {môn đổi: slot mới}`,
  giữ các môn còn lại gần lịch cũ nhất có thể.

---

## 4. Hiểu các thông số trong sidebar

### Tham số học vụ

| Thông số | Mặc định | Ý nghĩa |
|---|---|---|
| Số ngày ôn / 1 tín chỉ | 0.6 | Môn `n` TC cần ~`0.6n` ngày giữa môn trước. |
| Số ngày ôn tối thiểu (cứng) | 0.0 | Nếu >0 → ép cứng `\|day_i − day_j\| ≥ ⌈x⌉` cho cặp xung đột. |
| Max môn / SV / ngày | 2 | Số đề thi tối đa 1 SV được phép thi/ngày. |

### Tách môn lớn (auto-split) ✨

| Thông số | Mặc định | Ý nghĩa |
|---|---|---|
| Bật tách môn lớn | ✅ | Khuyến nghị bật cho trường có môn chung lớn (>1,500 SV). |
| Ngưỡng SV tối đa / 1 ca thi | 1,500 | Môn vượt ngưỡng → tự chia thành `⌈size/ngưỡng⌉` ca khác nhau, mỗi ca có **đề riêng**. |

> 📌 Khi tách, hệ thống chia `MalopHP` thành các nhóm bằng FFD (cân kích thước),
> đặt tên mới là `<Tên môn> (đề k/N)`. Mỗi đề có tập SV riêng (không trùng), thi ca khác nhau.

### Phân bố tải

| Thông số | Mặc định | Ý nghĩa |
|---|---|---|
| Mục tiêu phân bố | "Ưu tiên prep-day" | 4 preset: prep-day / cân bằng / cân bằng mạnh / nén lịch. |
| Soft cap SV / ca | 1,500 (0 = auto theo phòng) | Phạt mạnh khi ca có > X SV. |
| Số vòng LNS | 3 | Mỗi vòng di chuyển ~120 môn vi phạm nhất sang slot tốt hơn. 0 = tắt. |

### Solver & tối ưu

| Thông số | Mặc định | Ý nghĩa |
|---|---|---|
| Thời gian tối đa (s) | 120 | Tổng thời gian solver. Greedy thường xong < 30s; LNS thêm ~20s. |
| Bật tối ưu (LNS + CP-SAT polish) | ✅ | Tắt = chỉ chạy greedy (cực nhanh nhưng vi phạm prep cao). |
| Tự nới ràng buộc khi vô nghiệm | ✅ | Cascade-relax tự động: `min_prep_days` → `max_exams/ngày` → cho phép xung đột tối thiểu. |

> ⚠️ Hệ thống **tự bỏ CP-SAT** khi `số môn × số slot > 500,000` hoặc `số xung đột > 60,000`
> để tránh dựng model 10M+ vars (timeout chắc chắn). Bù lại bằng LNS.

---

## 5. Đọc kết quả & 6 tab

### Tab 1 – 📊 Tổng quan & KPI

8 metric chính:

| Metric | Nghĩa | Mong muốn |
|---|---|---|
| Môn đã xếp / tổng | – | 100% |
| Sinh viên | – | – |
| Slot dùng / tổng | – | Càng cao càng phân bố đều |
| Vi phạm prep-day | Số cặp SV-môn không đủ ngày ôn | Càng thấp càng tốt |
| Thuật toán | `greedy` / `greedy+lns` / `greedy+lns+cpsat` | `greedy+lns` đạt chất lượng tốt nhất cho instance lớn |
| Thời gian giải | – | < 60s |
| Tỉ lệ lấp slot | – | 30–60% |
| PBL position (0..1) | Trung bình vị trí PBL trong đợt (1 = cuối) | > 0.7 |

Phía dưới có **biểu đồ tải SV theo ngày** + **heatmap (Ngày × Ca)**.

### Tab 2 – 🔍 Chẩn đoán

- Cặp xung đột & mật độ.
- **Cảnh báo cứng** (đỏ): khi giải KHÔNG THỂ thoả hết (cần điều chỉnh đầu vào).
- **Cảnh báo mềm** (vàng): ví dụ "có 17 môn > 1,000 SV — bật tách môn lớn".
- Top 15 môn có nhiều vi phạm prep nhất.

### Tab 3 – 📅 Lịch theo môn

- Filter theo tên môn / loại môn (theory/oral/computer) / ngày.
- Cột `Sections` hiện danh sách lớp gộp.

### Tab 4 – 👤 Lịch theo SV

- Search theo mã SV / tên SV.
- Liệt kê đầy đủ lịch của SV qua các ngày.

### Tab 5 – 🏫 Phòng & giám thị

- Số phòng đang dùng / tổng.
- Cảnh báo môn quá tải so với phòng.
- Bar chart tải phòng + bảng tải giám thị.

### Tab 6 – ✍️ Đổi lịch thủ công

- Chọn môn → chọn ngày/ca mới → bấm **Áp dụng**.
- Hệ thống auto-repair toàn lịch, giữ các môn khác gần lịch cũ.

### Xuất Excel

Cuối trang có nút **⬇️ Tải xuống `ket_qua_xep_lich_thi.xlsx`** với 4 sheet:
- `LichThi` – cột chuẩn.
- `ViPhamNgayOn` – chi tiết vi phạm prep.
- `TheoSinhVien` – view per-student (~100k dòng cho dataset thật).
- `KPI` – bảng số tổng hợp.

---

## 6. Kịch bản xử lý sự cố thường gặp

### "Vô nghiệm" / treo quá lâu

| Triệu chứng | Cách xử lý |
|---|---|
| Tab Chẩn đoán có **lỗi đỏ** | Đọc message: thường là "ngày thi không đủ cho số môn của 1 SV" → mở rộng cửa sổ thi hoặc tăng `max môn/ngày`. |
| Báo "Hết thời gian tìm nghiệm" (hiếm gặp với v3) | Tăng `Thời gian tối đa` lên 300s; hoặc tắt CP-SAT (instance lớn). |
| Greedy còn môn unplaced | Bật **Tự nới ràng buộc**; check log "relaxations" trong tab Tổng quan. |

### Peak SV/ca quá cao (không bố trí phòng được)

| Triệu chứng | Cách xử lý |
|---|---|
| Max SV/ca ~4,000 (vd Triết MLN 56 lớp) | **Bật Tách môn lớn** (mặc định ON), giảm ngưỡng xuống 800–1,000. |
| Tải SV/ngày lệch 5× | Đổi preset sang **"Cân bằng tải"** trong sidebar. |
| Có sheet `KPI` báo `MaxSV/Slot` > sức chứa phòng | Giảm `soft_slot_cap` xuống dưới tổng sức chứa. |

### Quá nhiều vi phạm prep-day

| Triệu chứng | Cách xử lý |
|---|---|
| > 1,000 vi phạm | Tăng số vòng LNS (3 → 5). |
| Số SV bị "thi 2 môn cùng ngày" cao | Giảm `max môn/SV/ngày` xuống 1 (đảm bảo cứng), hoặc tăng prep_day_per_credit. |

### PBL/đồ án không ở cuối đợt

| Triệu chứng | Cách xử lý |
|---|---|
| PBL position < 0.5 | Đổi preset sang **"Ưu tiên prep-day"** (giảm balance_weight, để PBL push-late mạnh hơn). |

---

## 7. Kiến trúc kỹ thuật (dành cho dev)

```
┌─────────────────────────────────────────────────────┐
│ engine/io.py                                        │
│  • Đọc Excel, gộp lớp thành môn thi chung           │
│  • Auto-split môn lớn (FFD theo size)               │
└─────────────────┬───────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────┐
│ engine/diagnostics.py                               │
│  • Pre-flight: bottleneck, môn quá lớn, mật độ      │
│  • Post-run KPI: slot util, peak load, PBL pos      │
└─────────────────┬───────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────┐
│ engine/heuristic.py — DSATUR greedy                 │
│  • Sort theo (priority, degree, size)               │
│  • Score slot = PBL + balance + prep + repair       │
│  • Soft cap nearly-hard (bậc 1.5 + base 10k)        │
│  • Cascade-relax tự động khi unplaced               │
│  • lns_improve(): move-based LS,                    │
│    chấm điểm = prep_vio + peak_overflow             │
└─────────────────┬───────────────────────────────────┘
                  │ assignment + warm-start
┌─────────────────▼───────────────────────────────────┐
│ engine/scheduler.py — CP-SAT polish                 │
│  • Bỏ slot×exam reification (đẩy về phase phòng)    │
│  • max_exams_per_day = AddCumulative per-student    │
│  • Top-K prep pairs cho soft penalty                │
│  • Skip auto khi instance > 500k cặp môn×slot       │
└─────────────────┬───────────────────────────────────┘
                  │ final scheduled
┌─────────────────▼───────────────────────────────────┐
│ engine/rooms.py                                     │
│  • FFD bin-packing theo phòng to nhất               │
│  • Cân bằng tải giám thị theo (ngày, total)         │
└─────────────────────────────────────────────────────┘
```

### Mô hình hoá tối ưu (CP-SAT polish)

**Biến quyết định**
- `slot_i ∈ {0, …, D·S-1}`: slot của môn `i`.
- `day_i = slot_i // S`.

**Ràng buộc cứng**
1. `slot_i ≠ slot_j` cho mọi cặp `(i,j)` có SV trùng.
2. `AddCumulative([intervals_for_student_s], [1,1,…], capacity=max_per_day)` mỗi SV.
3. `|day_i − day_j| ≥ ⌈min_prep_days⌉` nếu `min_prep_days > 0`.
4. Domain `slot_i` lọc theo `exam_type` (lý thuyết / vấn đáp / máy tính).
5. `slot_i = target` cho `fixed_slots` (đổi lịch thủ công).

**Hàm mục tiêu (mềm)**
```
Minimize:
  Σ overlap(i,j) × max(0, req_days(i,j) − |day_i − day_j|)     # prep-day
+ Σ priority_i × (max_day − day_i)                             # PBL push-late
+ Σ |slot_i − base_slot_i|                                     # repair distance
```

### Move-based LNS

Mỗi vòng (`iteration`):
1. Tính `vio[eid]` = số cặp prep vi phạm liên quan đến `eid`.
2. Lấy top `pool_size` môn vi phạm nhất.
3. Với mỗi môn: duyệt allowed_slots, chọn slot tốt nhất theo `vio + peak_cost`.
4. Nếu cải thiện → commit move.

`peak_cost(slot) = max(0, slot_load(slot) − soft_cap) × PEAK_WEIGHT`

### Cascade-relax (greedy phase)

1. Bỏ giới hạn sức chứa tổng phòng.
2. Bỏ `max_exams_per_day`.
3. Cho phép xung đột SV tối thiểu (last resort, log lại).

---

## 8. Benchmark thực tế

Dataset: `DSSV_2510_xep_lich_thi.xlsx` — 112,961 đăng ký, 16,487 SV, 1,055 môn, 56 ngày, 12 ca/ngày.

| Phiên bản | Max SV/ca | Vi phạm prep | SV bị (% tổng) | Thời gian | Phù hợp |
|---|---|---|---|---|---|
| **v1 (CP-SAT thuần)** | – | – | – | 600s `UNKNOWN` | ❌ Không ra nghiệm |
| **v2 (Greedy)** | 4,056 | 6,898 | 5,625 (34%) | 7s | ⚠️ Peak quá cao |
| **v3 (Greedy+LNS, không split)** | 4,285 | 353 | 305 (1.8%) | 26s | ⚠️ Vẫn cần ~57 phòng |
| **v3 (Greedy+LNS, split>1500)** | **1,590** | 479 | – | 25s | ✅ ~26 phòng OK |
| **v3 (split>1000)** | **1,303** | 405 | – | 23s | ✅ ~22 phòng OK |
| **v3 (split>800)** | **1,043** | 695 | – | 23s | ✅ ~18 phòng OK |

> **Khuyến nghị**: dùng cấu hình mặc định (split>1500, LNS=3, balance="Ưu tiên prep-day").
> Giảm ngưỡng split nếu trường có ít phòng cùng lúc.

---

## 9. Cấu trúc thư mục

```
ExamScheduling/
├── setup.py                 # Thiết lập venv + pip (macOS / Windows / Linux)
├── setup.sh                 # Wrapper: gọi python3 setup.py (macOS / Linux)
├── setup.bat                # Wrapper: gọi py/python setup.py (Windows)
├── app.py                   # Streamlit UI (6 tabs)
├── engine/
│   ├── models.py            # dataclass: Exam, Room, ScheduledExam, SolveStats, …
│   ├── io.py                # đọc Excel, build exams, auto-split
│   ├── diagnostics.py       # FeasibilityReport, SchedulingKPI
│   ├── heuristic.py         # DSATUR greedy + LNS move-based
│   ├── scheduler.py         # CP-SAT polish (hybrid entry)
│   ├── rooms.py             # phân phòng FFD + giám thị
│   └── exporters.py         # xuất Excel: LichThi/ViPham/TheoSinhVien/KPI
├── data/                    # nơi đặt file mẫu (gitignored)
├── requirements.txt
└── README.md
```

---

## 10. Hướng nâng cấp tiếp

- **Lịch ràng buộc giảng viên**: 1 giảng viên không bị 2 ca chấm cùng lúc.
- **Phân ca theo phòng cụ thể** (room-aware slot constraint) khi danh sách phòng nhỏ.
- **Tối ưu phân phòng**: dùng CP-SAT cho FFD thay vì greedy.
- **Persist & versioning**: lưu lịch sử đợt thi, so sánh phiên bản, audit log đổi lịch.
- **Mobile view**: cho phép SV/GV xem lịch trên điện thoại (read-only).
- **Email/Zalo notification** khi có thay đổi lịch.

---

## 📞 API mã nguồn chính

```python
from engine.io import load_schedule_window, load_registrations, build_exams
from engine.scheduler import solve
from engine.diagnostics import diagnose, compute_kpi
from engine.rooms import assign_rooms_and_invigilators

window = load_schedule_window("Ke_hoach_thi.xlsx")
regs = load_registrations("DSSV.xlsx")
exams, student_ref = build_exams(regs, prep_day_per_credit=0.6, max_exam_size=1500)

result = solve(
    exams=exams,
    window=window,
    rooms=[],
    allowed_sessions_by_exam_id={...},
    prep_day_per_credit=0.6,
    min_prep_days=0,
    max_exams_per_day=2,
    solver_time_limit_seconds=120,
    balance_weight=0.3,
    soft_slot_cap=1500,
    lns_iterations=3,
)
# result.scheduled: List[ScheduledExam]
# result.stats: SolveStats (KPI tóm tắt)
```
