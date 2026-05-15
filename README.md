# Hệ thống xếp lịch thi tín chỉ (ExamScheduling)

Phần mềm xếp lịch thi cho trường đại học hệ tín chỉ, hỗ trợ cán bộ phòng đào tạo:
xếp lịch nhanh – đúng ràng buộc – tự tách môn lớn – tự repair khi đổi lịch thủ công.

> **Phiên bản v3.1 (2026-05)** — pipeline: **Diagnose → Greedy (sóng khóa, Khoa_nhom, ôn theo cặp TC, Sunday-spread) → LNS → CP-SAT (nếu đủ nhỏ).**  
> Data lớn thường chỉ tới **Greedy + LNS**; SAT tự tắt khi vượt ngưỡng.  
> Chi tiết: [mục 3 — Logic xếp lịch & nghiệp vụ](#3-logic-xếp-lịch--nghiệp-vụ).

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
| **Chỉ số khóa (sóng xếp)** | **2 ký tự đầu** của 4 ký tự cuối, phải là chữ số (vd `2510` → `25`) | Greedy xếp theo **sóng**: hết khóa mới nhất trong đợt (≈ năm 1, mã lớn) rồi mới tới khóa cũ; trong sóng: `priority` → bậc xung đột → quy mô SV. |

### 3.3 Ngày ôn (prep-day) — một quy tắc thống nhất

Các tham số sidebar (**không hardcode** `0.6` — đó chỉ là giá trị mặc định):

| Tham số | Công thức |
|---|---|
| `prep_day_per_credit` | Hệ số ngày ôn / 1 tín chỉ (mặc định 0.6) |
| `min_prep_days` | Sàn ngày ôn tối thiểu (mặc định 0 = tắt; đặt 1 → ép thêm khoảng cách tối thiểu) |

**Khoảng ôn giữa hai môn cùng SV** (Greedy cứng, LNS, báo cáo vi phạm, SAT soft):

```
required = max(min_prep_days, max(TC_môn_A, TC_môn_B) × prep_day_per_credit)
```

→ Môn **4 TC** cặp với môn 3 TC vẫn tính theo **4 TC** (tránh chỉ còn 1 ngày ôn như khi chỉ lấy TC môn sau).

**Greedy — nới dần khi chật** (ghi trong log `relaxations`):

1. Bỏ giới hạn sức chứa tổng (phòng xử lý sau)  
2. Bỏ `max môn/SV/ngày`  
3. Cho phép trùng SV cùng ca — **vẫn giữ ôn max(TC) + Khoa_nhom + giãn CN (≥3 ngày)**  
4. Nới **Khoa_nhom** / neo buổi — **vẫn giữ ôn & giãn CN**  
5. **Ép xếp hết** — chỉ lúc này mới nới ôn/CN, chọn slot ít vi phạm nhất  
6. Gán khẩn cấp slot 0 nếu thiếu cấu hình ca (hiếm)

**Không** tự chạy lại greedy với `min_prep_days=0` (tránh req=3 mà thực tế 0–1 ngày).

**Sunday-spread:** Môn thi **Chủ nhật** (thường môn đông) → môn khác **có SV trùng** phải cách **≥3 ngày** (cứng trong greedy/LNS; chỉ nới ở bước ép cuối). Vd Vật lý CN → Giải tích không dính Thứ Hai/T2.

**Quy tắc thứ trong tuần (môn đông):** Tổng SV theo **7 ký tự đầu** MalopHP ≥ ngưỡng sidebar → chỉ xếp **T7 hoặc CN**; môn nhỏ hơn → **T2–T7** (không CN). Dùng `weekday_at_day_index()` (Thứ Hai = 0 … Chủ nhật = 6).

### 3.4 Tách môn lớn và trần SV mỗi ca

- Khi bật «Tách môn lớn» và đặt **ngưỡng SV tối đa / 1 ca**: đó là **trần cứng** — **không ca nào** được vượt quá số SV đó (kể cả một MalopHP rất đông hoặc sau khi FFD gom lớp).
- Quy trình gần đúng: chia nhóm theo lớp (FFD theo quy mô) → với mỗi nhóm, **cắt thêm** danh sách SV theo từng khúc ≤ ngưỡng → sinh thêm ca nếu cần.
- Tên hiển thị: `(đề k/N)`; nếu một đề phải tách thêm theo SV: `— nhóm j/n`.

### 3.5 Pipeline sau khi bấm «Xếp lịch thi»

1. **Chẩn đoán trước** (`diagnose`): khả thi sơ bộ, mật độ xung đột, Khoa_nhom vs số ngày, v.v.
2. **Greedy** (`schedule_greedy`): sóng khóa → chọn slot (cân tải, prep cặp TC, Sunday-spread, PBL muộn, soft cap). **Khoa_nhom** và **không trùng SV cùng slot** giữ cứng đến bước (5); bước (6)–(7) đảm bảo **100% môn có slot**.
3. **Nới ràng buộc** (nếu còn ca chưa đặt): xem [mục 3.3](#33-ngày-ôn-prep-day--một-quy-tắc-thống-nhất).
4. **LNS** (`lns_improve`): move-based — prep cứng; pass **cùng-ngày**; thêm **pass sửa prep** (chỉ nhận move làm giảm tổng vi phạm).
5. **CP-SAT** (tuỳ chọn): chỉ khi `số_môn × số_slot ≤ 500_000` và `số_cặp_xung_đột ≤ 60_000` và còn ≥ 10s; warm-start từ Greedy+LNS. Soft prep SAT cũng dùng **max(TC)** cặp.

> **Thực tế dataset lớn:** thường thấy `greedy+lns` trong KPI; dòng log có thể ghi «Đã bỏ bước SAT vì bài toán quá lớn».

### 3.6 Phân phòng, mã ghép phòng & «cùng khu»

- Sau khi có **ngày + ca**: `assign_rooms_and_invigilators` gán phòng + giám thị (tự luận / trắc nghiệm / vấn đáp).
- **Cùng khu** khi nhiều phòng: **ký tự đầu `RoomID`** (vd `B101`, `B205` → khu `B`).
- **Chia SV theo phòng** (theo sức chứa, sort mã SV); sinh **`Ma_phong_chia`**:

  ```
  [7 ký tự đầu MalopHP] + [ký hiệu ca, bỏ gạch dưới] + [STT phòng 2 số]
  ```

  Ví dụ: học phần `1012107`, ca `C1`, phòng thứ 1 → `1012107C101`  
  (tương đương quy ước `1012107_C1_01`).

- Cột Excel: `Ma_phong_chia` (theo môn: danh sách; theo SV: một mã); `Phong` = mã phòng vật lý.

### 3.7 Đổi lịch thủ công & xuất

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
| Số ngày ôn / 1 tín chỉ (`prep_day_per_credit`) | 0.6 | **Biến cấu hình** — môn `n` TC cần ~`n × hệ_số` ngày giữa hai môn liên tiếp (theo **max TC** cặp). |
| Số ngày ôn tối thiểu (`min_prep_days`) | 1.0 (khuyên dùng) | Sàn cứng: mọi cặp môn cùng SV cách ≥ `⌈min_prep_days⌉` ngày (cộng thêm quy tắc theo TC). |
| Max môn / SV / ngày | 2 | Số đề thi tối đa 1 SV được phép thi/ngày. |
| Ngưỡng «môn rất đông» (T7–CN) | 800 | 0 = tắt. Đếm SV theo **7 ký tự đầu** MalopHP; ≥ ngưỡng → chỉ thi cuối tuần. |
| Ưu tiên giãn lịch (`spread_prep_factor`) | 1.75 | Tăng (2.0–2.5) để Greedy/LNS né xếp sát hơn khi data lớn. |

**Sóng khóa (tự động):** xếp khóa mới trước, khóa cũ sau; prep mềm nhẹ hơn với ca khóa cũ. Chi tiết: [mục 3.2–3.3](#32-malophp-7-ký-tự-đầu-4-ký-tự-cuối-và-2-số-khóa).

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
| Tự nới ràng buộc khi vô nghiệm | ✅ | Cascade 7 bước → **bắt buộc 100% môn có slot** (bước 6–7). Không chạy lại greedy với `min_prep_days=0`. |

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
- Cột `MalopHP`, `Ma_phong_chia` (mã ghép phòng/ca), `Phong`, `Ma_khoa_hoc_phan_7`, v.v.

### Tab 4 – 👤 Lịch theo SV

- Search theo mã SV / tên SV.
- Mỗi dòng: lịch SV + **`Ma_phong_chia`** (một phòng) + **`Phong`** (mã phòng vật lý).

### Tab 5 – 🏫 Phòng & giám thị

- Số phòng đang dùng / tổng.
- Cảnh báo môn quá tải so với phòng.
- Bar chart tải phòng + bảng tải giám thị.

### Tab 6 – ✍️ Đổi lịch thủ công

- Chọn môn → chọn ngày/ca mới → bấm **Áp dụng**.
- Hệ thống auto-repair toàn lịch, giữ các môn khác gần lịch cũ.

### Xuất Excel

Cuối trang có nút **⬇️ Tải xuống `ket_qua_xep_lich_thi.xlsx`** với các sheet:
- `Lich_thi` – lịch theo ca/môn (có `Ma_phong_chia`, `Phong`, …).
- `Vi_pham_ngay_on` – vi phạm prep (`required` theo **max TC** cặp).
- `Theo_sinh_vien` – view theo SV (~100k dòng dataset lớn).
- `KPI` – tóm tắt số liệu.

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

### Quá nhiều vi phạm prep-day / môn 4TC sát môn đông CN

| Triệu chứng | Cách xử lý |
|---|---|
| > 1,000 vi phạm | Tăng LNS (3 → 5), `spread_prep_factor` (1.75 → 2.5). |
| Môn 4TC chỉ 1 ngày sau môn CN đông | Đã ép **max(TC)×prep** ở Greedy/LNS; tăng `prep_day_per_credit` hoặc `min_prep_days`; kiểm tra log có «Nới ôn theo tín chỉ» không. |
| Số SV bị "thi 2 môn cùng ngày" cao | `max môn/SV/ngày` = 1; tăng `prep_day_per_credit`. |
| Chỉ chạy `greedy` (không LNS) | Bật tối ưu + LNS ≥ 3 trong sidebar. |

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
│  • Sóng khóa MalopHP → priority → bậc → size       │
│  • Prep cứng: max(TC)×prep + min_prep (cặp môn)    │
│  • Score: balance + prep cặp + Sunday-spread + PBL  │
│  • diagnostics: prep_days_required_for_pair, weekday│
│  • Cascade → force 100% placed (steps 6–7)          │
│  • lns_improve: prep_feasible, same-day pass        │
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
│  • Gom phòng cùng khu = ký tự đầu RoomID            │
│  • Chia SV theo capacity → Ma_phong_chia           │
│  • format_ma_phong_chia: 7 ký tự + ca + STT phòng  │
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

**Hàm mục tiêu (mềm)** — `req(i,j) = max(min_prep, max(TC_i, TC_j) × prep_per_credit)`
```
Minimize:
  Σ overlap(i,j) × max(0, req(i,j) − |day_i − day_j|)          # prep-day
+ Σ same_day(i,j) × weight                                     # ưu tiên bỏ cùng ngày
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
3. Trùng SV — giữ ôn + giãn CN.  
4. Nới Khoa_nhom (vẫn giữ ôn/CN).  
5. Ép cuối — nới ôn/CN chỉ khi bắt buộc, minimize vi phạm.  
6. Slot 0 khẩn cấp.

Helper prep: `engine/diagnostics.py` — `prep_days_required_for_pair`, `min_calendar_gap_days_between_exams`, `weekday_at_day_index`.

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
│   ├── diagnostics.py       # KPI, prep helpers, weekday, Khoa_nhom
│   ├── heuristic.py         # Greedy sóng khóa + LNS
│   ├── scheduler.py         # solve(), detect_prep_violations, CP-SAT
│   ├── rooms.py             # phân phòng + Ma_phong_chia
│   └── exporters.py         # Excel: Lich_thi / Vi_pham_ngay_on / …
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
prep = 0.6  # hoặc giá trị từ cấu hình trường
exams, student_ref = build_exams(regs, prep_day_per_credit=prep, max_exam_size=1500)

result = solve(
    exams=exams,
    window=window,
    rooms=[],
    allowed_sessions_by_exam_id={...},
    prep_day_per_credit=prep,
    min_prep_days=1.0,
    max_exams_per_day=2,
    solver_time_limit_seconds=120,
    balance_weight=0.3,
    soft_slot_cap=1500,
    lns_iterations=3,
    spread_prep_factor=1.75,
    weekend_large_course_min_students=800,
)
# result.scheduled — room_ids, room_split_codes sau assign_rooms_and_invigilators
# detect_prep_violations(..., prep_day_per_credit=prep, min_prep_days=1.0)
```
