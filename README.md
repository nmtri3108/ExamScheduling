# Hệ thống xếp lịch thi tín chỉ (ExamScheduling)

Phần mềm xếp lịch thi cho trường đại học hệ tín chỉ, hỗ trợ cán bộ phòng đào tạo:
xếp lịch nhanh – đúng ràng buộc – tự tách môn lớn – tự repair khi đổi lịch thủ công.

> **Phiên bản v3 (2026-05)** — pipeline: **Diagnose → Greedy (khóa ưu tiên, Khoa_nhom, load-balance, soft-cap) → LNS → CP-SAT polish.**  
> Chi tiết nghiệp vụ và ràng buộc: [mục 3 — Logic xếp lịch & nghiệp vụ](#3-logic-xếp-lịch--nghiệp-vụ).

---

## Mục lục

1. [Cài đặt và khởi động](#1-cài-đặt-và-khởi-động)
2. [File đầu vào](#2-file-đầu-vào)
3. [Logic xếp lịch & nghiệp vụ](#3-logic-xếp-lịch--nghiệp-vụ)
4. [Hướng dẫn từng bước](#4-hướng-dẫn-từng-bước-trên-giao-diện)
5. [Hiểu các thông số trong sidebar](#5-hiểu-các-thông-số-trong-sidebar)
6. [Đọc kết quả & 6 tab](#6-đọc-kết-quả--6-tab)
7. [Kịch bản xử lý sự cố thường gặp](#7-kịch-bản-xử-lý-sự-cố-thường-gặp)
8. [Kiến trúc kỹ thuật](#8-kiến-trúc-kỹ-thuật-dành-cho-dev)
9. [Benchmark thực tế](#9-benchmark-thực-tế)
10. [Cấu trúc thư mục](#10-cấu-trúc-thư-mục)
11. [Hướng nâng cấp tiếp](#11-hướng-nâng-cấp-tiếp)

---

## 1. Cài đặt và khởi động

### 1.1 Cài đặt lần đầu (Windows)

1. **Cài Git** — tải từ [git-scm.com/download/win](https://git-scm.com/download/win), cài xong mở **cmd** hoặc **PowerShell** và kiểm tra `git --version`.
2. **Clone repo** — chọn thư mục chứa mã nguồn, rồi (thay URL bằng địa chỉ repo thật của bạn):
   ```cmd
   git clone https://github.com/USER/ExamScheduling.git
   cd ExamScheduling
   ```
3. **Cài Python 3.10+** — một trong hai cách (khuyên dùng một cách thôi):
   - **Microsoft Store**: mở Store, tìm **Python 3.11** hoặc **Python 3.12**, bấm Cài đặt; hoặc
   - **python.org**: [Windows downloads](https://www.python.org/downloads/windows/) — khi cài, bật **Add python.exe to PATH** (hoặc dùng nút *Manage app execution aliases* và tắt alias «App Installer» nếu `python` trỏ nhầm).
4. **Chạy `setup.bat`** — trong thư mục repo, double-click `setup.bat` **hoặc**:
   ```cmd
   setup.bat
   ```
   File này gọi `setup.py --run`: tạo `.venv`, cài `requirements.txt`, rồi **mở luôn** ứng dụng Streamlit trong trình duyệt.

Nếu không muốn tự mở app: `py -3 setup.py` hoặc `python setup.py` (không thêm `--run`), sau đó chạy thủ công:

```cmd
.venv\Scripts\streamlit run app.py
```

### 1.2 macOS / Linux

Cần **Python 3.10+** (ví dụ `brew install python@3.12` trên macOS).

| Cách | Lệnh |
|---|---|
| Script | `./setup.sh` hoặc `python3 setup.py` (thêm `--run` nếu muốn mở Streamlit ngay) |
| Thủ công | `python3 -m venv .venv` → `.venv/bin/pip install -r requirements.txt` → `.venv/bin/streamlit run app.py` |

### 1.3 Dùng app (sau khi môi trường sẵn sàng)

Mở trình duyệt tại `http://localhost:8501`, upload **kế hoạch thi** + **danh sách đăng ký**,
giữ mặc định sidebar nếu chưa quen, bấm **Xếp lịch thi**. Thời gian chạy phụ thuộc quy mô (thường vài chục giây đến vài phút).

> Gợi ý: lần đầu chỉ cần đủ hai file bắt buộc rồi bấm chạy; các tham số mặc định đã được chỉnh cho bài toán lớn (hàng nghìn môn).

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
- **Khoa_nhom** = **4 ký tự cuối** `MalopHP`: hai **môn khác học phần** mà cùng một hậu tố này **không được xếp thi cùng một ngày** (Greedy, LNS và CP-SAT đều áp). Các **ca tách cùng một học phần** (cùng 7 ký tự đầu MalopHP hoặc cùng `CourseID`) được **miễn** quy tắc này giữa các ca với nhau.
- `ExamType`: `theory` / `oral` / `computer` từ tên môn (qua từ khoá).
- **Môn thi chung**: môn có từ **3 lớp học phần trở lên** (ngưỡng cấu hình được, mặc định ≥3) → gộp một đề thi chung (trừ khi tách môn lớn tạo thêm ca).

### 2.3 Tuỳ chọn – Phòng thi

| Cột | Bắt buộc | Ý nghĩa |
|---|---|---|
| `RoomID` | Có | Mã phòng |
| `Capacity` | Có | Sức chứa (số chỗ) |
| `Location` / `Khu` | Không | Vị trí hiển thị (có thể để trống) |
| `RoomType` | Không | `theory` / `computer` / `any` — khớp bảng mã hình thức thi |

Khi một môn cần **nhiều phòng**, bước phân phòng **ưu tiên gom phòng cùng khu**: **khu = ký tự đầu của `RoomID`** (ví dụ `B101` và `B205` cùng khu `B`), không dựa vào cột `Location`.

### 2.4 ⚙️ Tuỳ chọn – Giám thị

| Cột | Bắt buộc | Ý nghĩa |
|---|---|---|
| `InvigilatorID` | ✅ | Mã giám thị |
| `FullName` | ✅ | Họ tên |
| `MaxSessionsPerDay` | – | Số ca / ngày (mặc định 2) |
| `MaxSessionsTotal` | – | Tổng ca cả đợt (mặc định ∞) |

---

## 3. Logic xếp lịch & nghiệp vụ

Phần này mô tả **đúng hành vi hiện tại** của app (Streamlit `app.py` + thư mục `engine/`), giúp cán bộ và dev cùng ngôn ngữ.

### 3.1 Đơn vị xếp lịch: theo ca thi (Exam), không theo từng sinh viên

- Từ file DSSV, hệ thống gom đăng ký thành danh sách **ca thi** (`Exam`): mỗi ca có `student_ids`, `section_ids` (MalopHP), loại hình thi, v.v.
- Greedy / LNS / CP-SAT gán **ô thời gian (slot) cho từng ca**, theo thứ tự ưu tiên và đồ thị xung đột.
- **Sinh viên** dùng để: biết hai ca nào **không được trùng slot** (chung SV); giới hạn **số môn/SV/ngày**; tính **ngày ôn** (prep) mềm/cứng; không có vòng lặp «lần lượt mỗi SV rồi tìm chỗ».

### 3.2 MalopHP: 7 ký tự đầu, 4 ký tự cuối, và 2 số «khóa»

| Khái niệm | Cách lấy | Dùng cho |
|---|---|---|
| Tiền tố học phần | **7 ký tự đầu** `MalopHP` | Gom «môn đông» theo ngưỡng SV; các **ca tách cùng học phần** phải cùng **buổi** (sáng/chiều) khi bật tách đề. |
| **Khoa_nhom** | **4 ký tự cuối** `MalopHP` | Hai **học phần khác nhau** có cùng hậu tố → **không thi cùng một ngày** (cứng: Greedy, LNS, CP-SAT). **Miễn** giữa các ca **cùng học phần** (cùng 7 ký tự đầu hoặc cùng `CourseID`). |
| **Chỉ số khóa (ưu tiên xếp)** | **2 ký tự đầu** của 4 ký tự cuối, phải là chữ số (vd `2510` → `25`) | Trên mỗi ca lấy **max** theo mọi MalopHP của ca: số **càng lớn** → xếp **trước** trong Greedy để chiếm slot tốt hơn (ưu tiên SV khóa mới / ít môn). |

### 3.3 Tách môn lớn và trần SV mỗi ca

- Khi bật «Tách môn lớn» và đặt **ngưỡng SV tối đa / 1 ca**: đó là **trần cứng** — **không ca nào** được vượt quá số SV đó (kể cả một MalopHP rất đông hoặc sau khi FFD gom lớp).
- Quy trình gần đúng: chia nhóm theo lớp (FFD theo quy mô) → với mỗi nhóm, **cắt thêm** danh sách SV theo từng khúc ≤ ngưỡng → sinh thêm ca nếu cần.
- Tên hiển thị: `(đề k/N)`; nếu một đề phải tách thêm theo SV: `— nhóm j/n`.

### 3.4 Pipeline sau khi bấm «Xếp lịch thi»

1. **Chẩn đoán trước** (`diagnose`): khả thi sơ bộ, mật độ xung đột, Khoa_nhom vs số ngày, v.v.
2. **Greedy** (`heuristic.schedule_greedy`): đặt lần lượt từng ca vào slot hợp lệ, chấm điểm slot (cân tải, prep mềm, PBL muộn, soft cap, repair). **Thứ tự ca**: khóa số (2 chữ) giảm dần → `priority` (PBL) → bậc xung đột → quy mô SV. **Prep mềm**: ca thuộc **khóa cũ hơn** (so với khóa lớn nhất trong đợt) có **trọng số phạt prep thấp hơn** (chấp nhận dễ xếp «chật» hơn).
3. **Nới ràng buộc (tuỳ chọn)**: capacity tổng phòng → `max môn/SV/ngày` → cuối cùng mới cho **trùng SV trên cùng slot** (last resort, có log). **Khoa_nhom** và **trùng slot do SV** ở các bước trước vẫn cứng.
4. **LNS** (`lns_improve`): thử chuyển ca vi phạm prep sang slot khác (vẫn kiểm Khoa_nhom, xung đột, v.v.).
5. **CP-SAT** (nếu đủ nhỏ): đánh bóng nghiệm, warm-start từ Greedy+LNS; có ràng buộc **ngày khác nhau** cho cặp ca vi phạm Khoa_nhom (học phần khác nhau).

### 3.5 Phân phòng và «cùng khu»

- Sau khi có **ngày + ca** cho từng môn: `assign_rooms_and_invigilators` gán phòng + giám thị theo loại hình thi và sức chứa.
- **Cùng khu** khi cần nhiều phòng: **ký tự đầu của `RoomID`** (in hoa), không dùng cột `Location`.

### 3.6 Đổi lịch thủ công & xuất

- Tab đổi lịch: gửi lại solver với `fixed_slots` (ca đổi khóa vào slot mới), các ca khác **repair** gần lịch cũ (hàm mục tiêu khoảng cách slot).
- Ghim lịch mâu thuẫn **Khoa_nhom** có thể bị bỏ ghim và ghi trong log nới ràng buộc.
- Xuất Excel: nhiều sheet (lịch thi, vi phạm prep, theo SV, KPI) — xem [mục kết quả & tab](#6-đọc-kết-quả--6-tab).

---

## 4. Hướng dẫn từng bước trên giao diện

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

### Bước 3: Bấm nút xếp lịch thi
- Progress bar cho biết các pha: Greedy → LNS → CP-SAT (nếu instance nhỏ).
- Hoàn tất sẽ tự cuộn xuống phần kết quả.

### Bước 4: Kiểm tra kết quả qua 6 tab → xuất Excel
Xem mục [kết quả & tab](#6-đọc-kết-quả--6-tab).

### Bước 5 (nếu cần): Đổi lịch thủ công
- Vào tab **✍️ Đổi lịch thủ công**.
- Chọn môn cần đổi → chọn ngày/ca mới → bấm **Áp dụng đổi lịch & repair**.
- Hệ thống chạy lại pipeline với ràng buộc `fixed_slots = {môn đổi: slot mới}`,
  giữ các môn còn lại gần lịch cũ nhất có thể.

---

## 5. Hiểu các thông số trong sidebar

### Tham số học vụ

| Thông số | Mặc định | Ý nghĩa |
|---|---|---|
| Số ngày ôn / 1 tín chỉ | 0.6 | Môn `n` TC cần ~`0.6n` ngày giữa môn trước. |
| Số ngày ôn tối thiểu (cứng) | 0.0 | Nếu >0 → ép cứng `\|day_i − day_j\| ≥ ⌈x⌉` cho cặp xung đột. |
| Max môn / SV / ngày | 2 | Số đề thi tối đa 1 SV được phép thi/ngày. |

**Ưu tiên khóa (tự động — không có ô tắt trên UI):** Greedy xếp ca có **mã khóa** (2 chữ số đầu của 4 ký tự cuối `MalopHP`) **lớn hơn** trước; phạt **prep-day mềm** nhẹ hơn với ca chủ yếu **khóa cũ** trong đợt. Chi tiết: [mục 3](#3-logic-xếp-lịch--nghiệp-vụ).

### Tách môn lớn (auto-split) ✨

| Thông số | Mặc định | Ý nghĩa |
|---|---|---|
| Bật tách môn lớn | ✅ | Khuyến nghị bật cho trường có môn chung lớn (>1,500 SV). |
| Ngưỡng SV tối đa / 1 ca thi | 1,500 | **Trần cứng** số SV mỗi ca: không ca nào vượt ngưỡng. Môn vượt tổng SV → chia nhiều ca (đề riêng); sau khi gom theo lớp (FFD), từng ca vẫn có thể **cắt thêm theo danh sách SV** nếu một nhóm lớp vẫn quá đông. |

> Khi tách, tên ca có dạng `<Tên môn> (đề k/N)`; nếu một đề phải tách thêm theo SV: thêm hậu tố `— nhóm j/n`.

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

## 6. Đọc kết quả & 6 tab

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
- **Cảnh báo cứng** (đỏ): ví dụ không đủ ngày cho số môn/SV; **Khoa_nhom**: một hậu tố 4 ký tự cuối MalopHP gắn quá nhiều **môn khác nhau** so với số ngày đợt thi.
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

## 7. Kịch bản xử lý sự cố thường gặp

### Lỗi Khoa_nhom (chẩn đoán trước khi chạy)

| Triệu chứng | Cách xử lý |
|---|---|
| Một hậu tố 4 ký tự cuối MalopHP gắn quá nhiều **môn khác nhau** so với số **ngày** trong kế hoạch thi | Mở rộng đợt thi (thêm ngày), hoặc điều chỉnh mã lớp / nhóm tách sao cho không dồn cùng một `Khoa_nhom` cho quá nhiều học phần độc lập. |
| Ghim lịch thủ công (`fixed_slots`) báo bỏ ghim / mâu thuẫn | Hai môn khác học phần cùng `Khoa_nhom` không thể cùng ngày; chọn ngày khác hoặc bỏ ghim một trong hai. |

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

## 8. Kiến trúc kỹ thuật (dành cho dev)

```
┌─────────────────────────────────────────────────────┐
│ engine/io.py                                        │
│  • Đọc Excel, gộp lớp thành môn thi chung (ngưỡng ≥3 lớp) │
│  • Auto-split môn lớn: FFD theo lớp + cắt cứng theo max SV/ca │
└─────────────────┬───────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────┐
│ engine/diagnostics.py                               │
│  • Pre-flight: bottleneck, môn quá lớn, Khoa_nhom vs số ngày │
│  • Post-run KPI: slot util, peak load, PBL pos      │
└─────────────────┬───────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────┐
│ engine/heuristic.py — Greedy + LNS                  │
│  • Thứ tự ca: khóa (2 số đầu của 4 cuối MalopHP) giảm dần → priority → bậc → size │
│  • Score slot = PBL + balance + prep (nhẹ hơn với khóa cũ) + repair │
│  • Khoa_nhom cứng (trừ ca tách cùng học phần)       │
│  • Soft cap nearly-hard (bậc 1.5 + base 10k)        │
│  • Cascade-relax khi unplaced (Khoa_nhom vẫn cứng)  │
│  • lns_improve(): move-based LS (+ Khoa_nhom)       │
└─────────────────┬───────────────────────────────────┘
                  │ assignment + warm-start
┌─────────────────▼───────────────────────────────────┐
│ engine/scheduler.py — CP-SAT polish                 │
│  • Bỏ slot×exam reification (đẩy về phase phòng)    │
│  • max_exams_per_day = AddCumulative per-student    │
│  • Khoa_nhom: day_i ≠ day_j cho cặp môn khác học phần │
│  • Top-K prep pairs cho soft penalty                │
│  • Skip auto khi instance > 500k cặp môn×slot       │
└─────────────────┬───────────────────────────────────┘
                  │ final scheduled
┌─────────────────▼───────────────────────────────────┐
│ engine/rooms.py                                     │
│  • Gom phòng cùng khu = cùng ký tự đầu RoomID      │
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
2. `AddCumulative([intervals_for_student_s], [1,1,…], capacity=max_per_day)` mỗi SV (khi SV đó có nhiều hơn `max_per_day` môn).
3. `|day_i − day_j| ≥ ⌈min_prep_days⌉` nếu `min_prep_days > 0` (trên các cặp xung đột SV).
4. **Khoa_nhom**: nếu hai ca có giao **4 ký tự cuối MalopHP** và là **hai học phần khác nhau** (không thuộc miễn ca tách) thì `day_i ≠ day_j`.
5. Domain `slot_i` lọc theo `exam_type` và quy tắc thứ trong tuần (môn đông → T7–CN, v.v.).
6. `slot_i = target` cho `fixed_slots` (đổi lịch thủ công).

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

**Khoa_nhom** và **xung đột slot (SV trùng)** ở các bước trước vẫn được giữ cứng; chỉ bước (3) mới cho phép trùng SV tối thiểu.

---

## 9. Benchmark thực tế

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

## 10. Cấu trúc thư mục

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

## 11. Hướng nâng cấp tiếp

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
