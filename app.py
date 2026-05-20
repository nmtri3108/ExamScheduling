"""Giao diện Streamlit — hệ thống xếp lịch thi tín chỉ.

Luồng xử lý: tham lam (DSATUR) → cải tiến LNS → (tuỳ chọn) tối ưu ràng buộc SAT;
chẩn đoán trước khi giải; phân phòng và giám thị sau khi có lịch.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Dict, List

import pandas as pd
import streamlit as st

from engine.diagnostics import (
    build_student_cohort_code_map,
    build_student_cohort_map,
    compute_kpi,
    diagnose,
)
from engine.exporters import (
    exam_view_dataframe,
    schedule_to_dataframe,
    student_view_dataframe,
    to_excel_bytes,
    unplaced_to_dataframe,
    violations_to_dataframe,
)
from engine.io import (
    build_exams,
    load_invigilators,
    load_registrations,
    load_rooms,
    load_schedule_window,
)
from engine.i18n import hien_thi_loai_hinh, hien_thi_phuong_thuc, markdown_canh_bao_hoc_phan_thieu_ngay_on
from engine.manual_pattern import build_manual_pattern_profile
from engine.rooms import assign_rooms_and_invigilators
from engine.scheduler import detect_prep_violations, solve

st.set_page_config(
    page_title="Xếp lịch thi tín chỉ",
    page_icon="📅",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save_uploaded(uploaded_file) -> Path:
    suffix = Path(uploaded_file.name).suffix
    _, path = tempfile.mkstemp(suffix=suffix)
    Path(path).write_bytes(uploaded_file.getbuffer())
    return Path(path)


def _parse_csv(text: str) -> List[str]:
    return [x.strip() for x in str(text).split(",") if x.strip()]


def _show_hoc_phan_prep_hard_errors(kpi) -> None:
    """Hiển thị cùng một nội dung ở tab Tổng quan và Chẩn đoán (tránh lệch chữ)."""
    hard_errs = getattr(kpi, "prep_prefix_hard_errors", None) or []
    if hard_errs:
        st.error(markdown_canh_bao_hoc_phan_thieu_ngay_on(hard_errs))


def _build_slot_config(theory: str, oral: str, computer: str):
    """Trả về (session_labels, allowed_by_type, session_half).

    `session_half[i]` ∈ {0,1}: 0 = buổi sáng, 1 = buổi chiều — theo thứ tự ca trong
    từng nhóm loại: nửa đầu danh sách ca của loại đó là sáng, nửa sau là chiều.
    """
    by_type_labels = {
        "theory": _parse_csv(theory),
        "oral": _parse_csv(oral),
        "computer": _parse_csv(computer),
    }
    if any(len(v) == 0 for v in by_type_labels.values()):
        raise ValueError("Mỗi nhóm ca thi (lý thuyết / PBL / trắc nghiệm máy) phải có ≥ 1 ca.")
    label_half: Dict[str, int] = {}
    for labels in by_type_labels.values():
        n = len(labels)
        mid = (n + 1) // 2
        for i, lbl in enumerate(labels):
            if lbl not in label_half:
                label_half[lbl] = 0 if i < mid else 1
    all_labels: List[str] = []
    for labels in by_type_labels.values():
        for lbl in labels:
            if lbl not in all_labels:
                all_labels.append(lbl)
    allowed_by_type: Dict[str, List[int]] = {}
    for etype, labels in by_type_labels.items():
        allowed_by_type[etype] = [all_labels.index(lbl) for lbl in labels]
    session_half = [label_half[lbl] for lbl in all_labels]
    return all_labels, allowed_by_type, session_half


def _run_pipeline(
    exams,
    window,
    rooms,
    invigilators,
    session_labels,
    session_half,
    allowed_sessions_by_exam_id,
    allowed_sessions_by_type,
    student_name_map,
    prep_day_per_credit,
    min_prep_days,
    max_exams_per_day,
    solver_time_limit,
    invigilators_per_room,
    optimize,
    balance_weight=0.12,
    soft_slot_cap=1100,
    lns_iterations=7,
    fixed_slots=None,
    base_slots=None,
    auto_relax=True,
    theory_fill_low=0.90,
    theory_fill_high=1.00,
    computer_fill_low=0.85,
    computer_fill_high=0.95,
    weekend_large_course_min_students=0,
    spread_prep_factor=2.4,
    student_cohort=None,
    student_cohort_codes=None,
    year1_cohort_anchor=0,
    year1_allow_same_day=True,
    preferred_session_by_prefix7=None,
    weekday_session_bonus=None,
    pattern_weight=1.0,
    target_students_per_room_by_exam_format=None,
    preferred_zone_by_session_label=None,
    max_rooms_per_slot_per_format=50,
):
    """Một bước: chẩn đoán → solve → phân phòng → vi phạm → KPI."""
    started = time.time()
    progress_bar = st.progress(0, text="Bắt đầu...")

    def _cb(pct: int, msg: str):
        elapsed = int(time.time() - started)
        progress_bar.progress(min(max(0, pct), 99), text=f"{msg} | đã chạy {elapsed}s")

    diagnose_report = diagnose(
        exams=exams,
        window=window,
        rooms=rooms,
        allowed_sessions_by_type=allowed_sessions_by_type,
        min_prep_days=float(min_prep_days),
        max_exams_per_day=int(max_exams_per_day),
        weekend_large_course_min_students=int(weekend_large_course_min_students),
    )

    result = solve(
        exams=exams,
        window=window,
        rooms=rooms,
        allowed_sessions_by_exam_id=allowed_sessions_by_exam_id,
        session_labels=session_labels,
        session_half=list(session_half or []),
        prep_day_per_credit=float(prep_day_per_credit),
        min_prep_days=float(min_prep_days),
        max_exams_per_day=int(max_exams_per_day),
        solver_time_limit_seconds=float(solver_time_limit),
        optimize_objective=bool(optimize),
        fixed_slots=fixed_slots,
        base_slots=base_slots,
        auto_relax=auto_relax,
        balance_weight=float(balance_weight),
        soft_slot_cap=int(soft_slot_cap) if soft_slot_cap and int(soft_slot_cap) > 0 else None,
        lns_iterations=int(lns_iterations),
        progress_cb=_cb,
        weekend_large_course_min_students=int(weekend_large_course_min_students),
        spread_prep_factor=float(spread_prep_factor),
        student_cohort=student_cohort,
        student_cohort_codes=student_cohort_codes,
        year1_cohort_anchor=int(year1_cohort_anchor or 0),
        year1_allow_same_day=bool(year1_allow_same_day),
        preferred_session_by_prefix7=preferred_session_by_prefix7,
        weekday_session_bonus=weekday_session_bonus,
        pattern_weight=float(pattern_weight),
        max_rooms_per_slot_per_format=int(max_rooms_per_slot_per_format),
    )

    room_report = assign_rooms_and_invigilators(
        scheduled=result.scheduled,
        exams=exams,
        rooms=rooms,
        invigilators=invigilators,
        invigilators_per_room=int(invigilators_per_room),
        theory_fill_low=float(theory_fill_low),
        theory_fill_high=float(theory_fill_high),
        computer_fill_low=float(computer_fill_low),
        computer_fill_high=float(computer_fill_high),
        target_students_per_room_by_exam_format=target_students_per_room_by_exam_format,
        preferred_zone_by_session_label=preferred_zone_by_session_label,
        max_rooms_per_slot_per_format=int(max_rooms_per_slot_per_format),
    )

    violations = detect_prep_violations(
        scheduled=result.scheduled,
        exams=exams,
        student_name_map=student_name_map,
        prep_day_per_credit=float(prep_day_per_credit),
        min_prep_days=float(min_prep_days),
        student_cohort=student_cohort,
        student_cohort_codes=student_cohort_codes,
        year1_cohort_anchor=int(year1_cohort_anchor or 0),
        year1_allow_same_day=bool(year1_allow_same_day),
    )
    kpi = compute_kpi(
        result.scheduled,
        exams,
        window,
        violations,
        student_cohort=student_cohort,
        student_cohort_codes=student_cohort_codes,
        year1_cohort_anchor=int(year1_cohort_anchor or 0),
        year1_allow_same_day=bool(year1_allow_same_day),
    )
    progress_bar.progress(100, text="Hoàn tất!")
    progress_bar.empty()

    return {
        "result": result,
        "diagnose": diagnose_report,
        "violations": violations,
        "kpi": kpi,
        "room_report": room_report,
    }


# ---------------------------------------------------------------------------
# Sidebar — Cấu hình
# ---------------------------------------------------------------------------

st.title("📅 Hệ thống xếp lịch thi tín chỉ")
st.caption(
    "Ngày trong tuần: môn thường xếp thứ Hai–thứ Bảy (không Chủ nhật). "
    "Môn rất đông (tổng SV theo **7 ký tự đầu** mã lớp ≥ ngưỡng ở sidebar) chỉ xếp **thứ Bảy hoặc Chủ nhật**. "
    "Giải gồm: tham lam (DSATUR) → cải tiến LNS → tối ưu ràng buộc (SAT) nếu bài toán đủ nhỏ."
)

with st.sidebar:
    st.header("⚙️ Cấu hình")

    with st.expander("Tham số học vụ", expanded=True):
        prep_day_per_credit = st.number_input(
            "Số ngày nghỉ ôn mong muốn / 1 tín chỉ",
            min_value=0.1,
            value=0.6,
            step=0.1,
            help="Ví dụ 0,6 × 3 tín chỉ ≈ gần 2 ngày nghỉ giữa hai môn liên tiếp của sinh viên (mục tiêu mềm, không phải lệnh cứng).",
        )
        min_prep_days = st.number_input(
            "Ngày nghỉ ôn tối thiểu (ràng buộc cứng)",
            min_value=0.0,
            value=1.0,
            step=0.5,
            help="0 = tắt. Đặt 1 nghĩa là giữa hai môn liên tiếp phải cách ít nhất 1 ngày theo lịch (kết hợp với ô trên).",
        )
        max_exams_per_day = st.number_input(
            "Tối đa bao nhiêu môn một sinh viên được thi trong cùng một ngày",
            min_value=1,
            value=2,
            step=1,
            help=(
                "Khóa năm 1 (theo cấu hình niên khóa) có thể tới 2 môn/ngày nếu bật ô trên. "
                "Khóa cũ hơn engine tự giới hạn **1 môn/ngày** khi bật ngày ôn — giảm thi cùng ngày."
            ),
        )
        weekend_large_min_sv = st.number_input(
            "Ngưỡng «môn rất đông»: chỉ thi cuối tuần (T7–CN)",
            min_value=0,
            value=800,
            step=50,
            help=(
                "0 = tắt quy tắc. Khi bật: đếm **số sinh viên khác nhau** theo **7 ký tự đầu** mã lớp học phần; "
                "nếu ≥ ngưỡng thì môn đó chỉ xếp thứ Bảy hoặc Chủ nhật. "
                "Dưới ngưỡng: thứ Hai–thứ Bảy (không Chủ nhật)."
            ),
        )
        spread_prep_factor = st.slider(
            "Ưu tiên giãn lịch (điểm phạt trong bước tham lam)",
            min_value=1.0,
            max_value=3.5,
            value=2.6,
            step=0.05,
            help=(
                "Càng cao càng ưu tiên dùng nhiều ngày/slot trống để giữ ngày ôn (khuyên **2,5–2,7** "
                "cho ~16k SV / ~1.600 ca). Mặc định 2,6 — cân bằng xếp hết ca và giảm vi phạm ôn."
            ),
        )
        year1_cohort_anchor = st.number_input(
            "Niên khóa SV năm 1 hiện tại (2 số đầu, 4 ký tự cuối MalopHP)",
            min_value=0,
            max_value=99,
            value=25,
            step=1,
            help=(
                "Ví dụ đặt **25**: ưu tiên xếp & giữ ngày ôn cho khóa **25**, rồi **24, 23, …**; "
                "mã lạ hoặc số > 25 (yy, zz, 48, 49, 50, …) ưu tiên thấp nhất. "
                "**0** = tự lấy mã số lớn nhất trong file đăng ký."
            ),
        )
        year1_allow_same_day = st.checkbox(
            "Khóa năm 1: được thi cùng ngày (ôn giữa hai ngày thi khác nhau)",
            value=True,
            help=(
                "Bật (khuyên dùng): engine **giữ ~1 môn/SV/ngày** (giống xếp tay). "
                "Khóa năm 1 **không bắt** ngày ôn giữa hai môn nếu trùng ngày (hiếm). "
                "Giữa hai **ngày khác nhau** vẫn cần đủ ngày ôn theo tín chỉ. "
                "Tắt = bắt đủ ôn kể cả cùng ngày (rất khó xếp hết)."
            ),
        )

    with st.expander("Bộ ca thi theo loại", expanded=True):
        theory_text = st.text_input(
            "Ca lý thuyết (cách nhau bằng dấu phẩy)", value="2C1,2C2,2C3,2C4",
            help=(
                "Ký hiệu ca theo thứ tự thời gian. Nửa đầu danh sách = buổi sáng, nửa sau = chiều "
                "(dùng chung cho ràng buộc cùng buổi theo 7 ký tự đầu MalopHP)."
            ),
        )
        oral_text = st.text_input(
            "Ca PBL / đồ án / vấn đáp (cách nhau bằng dấu phẩy)", value="1A1,1P1",
        )
        computer_text = st.text_input(
            "Ca trắc nghiệm máy tính (cách nhau bằng dấu phẩy)", value="3C1,3C2,3C3,3C4,3C5,3C6",
        )

    with st.expander("Bộ giải và tối ưu", expanded=False):
        solver_time_limit = st.number_input(
            "Giới hạn thời gian cho bước tối ưu (giây)",
            min_value=10,
            value=240,
            step=10,
            help="Chủ yếu áp dụng cho bước tối ưu ràng buộc (SAT). Bước tham lam + LNS thường xong trước đó.",
        )
        optimize_objective = st.checkbox(
            "Bật cải tiến sau tham lam (LNS + SAT nếu đủ nhỏ)",
            value=True,
            help="LNS: sửa cục bộ, thường rất hữu ích. SAT: chỉ chạy khi số môn và xung đột nằm trong ngưỡng an toàn.",
        )
        auto_relax_on_infeasible = st.checkbox(
            "Ghi chú khi không xếp hết môn (không tự bỏ ngày ôn toàn cục)",
            value=True,
            help=(
                "Nếu vẫn còn môn chưa đặt sau bước tham lam, chỉ ghi log gợi ý — "
                "không chạy lại với min_prep_days=0 (tránh làm vỡ ngày ôn theo tín chỉ)."
            ),
        )
        pattern_weight = st.slider(
            "Mức học theo pattern chia tay (nếu có file mẫu)",
            min_value=0.0,
            max_value=3.0,
            value=1.0,
            step=0.1,
            help=(
                "0 = tắt. >0: ưu tiên ca/nhịp thứ-ca giống file bạn đã chia tay "
                "(2510DanhSachThiChung + ALL_ALL_LThiGV_2510) nếu các file này có trong thư mục data."
            ),
        )

    with st.expander("Tách môn lớn (tự động)", expanded=True):
        st.caption(
            "Dùng khi một môn gom quá nhiều sinh viên trong một ca: hệ thống tách thành nhiều ca, "
            "mỗi ca một đề (ví dụ môn đại trà hàng nghìn người)."
        )
        enable_split = st.checkbox(
            "Tách môn quá đông thành nhiều ca (khuyên dùng)",
            value=True,
            help="Giúp tránh một ca «vỡ» phòng hoặc quá tải giám thị khi một môn có rất nhiều sinh viên.",
        )
        max_exam_size = st.number_input(
            "Tối đa bao nhiêu sinh viên / một ca thi (sau khi tách)",
            min_value=200,
            max_value=10000,
            value=1500,
            step=100,
            disabled=not enable_split,
            help=(
                "Giới hạn cứng cho mỗi ca. Môn vượt sẽ bị chia thêm ca. "
                "1500 là điểm cân bằng mặc định; giảm nếu ít phòng hoặc muốn ca nhỏ hơn."
            ),
        )

    with st.expander("Phân bố tải", expanded=True):
        balance_mode = st.radio(
            "Ưu tiên khi xếp lịch",
            options=[
                "Ưu tiên nghỉ ôn giữa các môn (mặc định)",
                "Cân bằng: vừa nghỉ ôn, vừa trải đều số SV theo ngày",
                "Cân bằng mạnh về số SV/ngày (có thể hy sinh một phần nghỉ ôn)",
                "Nén lịch: dùng ít ngày nhất có thể",
            ],
            index=0,
            help=(
                "Mặc định (khuyên dùng): ưu tiên ngày ôn, dùng rộng cửa sổ thi còn trống.\n"
                "Cân bằng: san tải theo ngày — có thể hy sinh một phần ôn.\n"
                "Nén: ít ngày thi hơn — dễ tăng vi phạm 0 ngày ôn."
            ),
        )
        if "mạnh về số SV" in balance_mode:
            balance_weight = 1.0
        elif balance_mode.startswith("Cân bằng:"):
            balance_weight = 0.5
        elif balance_mode.startswith("Nén"):
            balance_weight = 0.05
        else:
            balance_weight = 0.12
        soft_slot_cap_value = st.number_input(
            "Ngưỡng mềm: một ca không nên quá bao nhiêu sinh viên (0 = tự tính)",
            min_value=0,
            value=1100,
            step=100,
            help=(
                "Ca vượt ngưỡng bị đẩy sang slot/ngày khác nếu có thể. "
                "Mặc định 1100 (khuyên 1000–1200) — tránh ca 1500+ SV, cân tải gần lịch mẫu."
            ),
        )
        lns_iterations = st.slider(
            "Số vòng sửa lịch cục bộ (LNS)",
            min_value=0,
            max_value=15,
            value=4,
            step=1,
            help=(
                "Mỗi vòng xử lý ~200 môn vi phạm ôn nặng nhất. "
                "Mặc định 4 vòng (đủ tốt cho dữ liệu lớn, chạy nhanh hơn); 0 = tắt LNS."
            ),
        )

    with st.expander("Phòng & giám thị", expanded=False):
        invigilators_per_room = st.number_input(
            "Giám thị / phòng", min_value=1, value=2, step=1,
        )
        st.caption(
            "Mã hình thức thi: 1=Tự luận (phòng lý thuyết, lấp đầy 90–100%), "
            "2=Trắc nghiệm (phòng máy, 85–95%), 3=Vấn đáp (không gò theo SV)."
        )
        c_lo, c_hi = st.columns(2)
        with c_lo:
            theory_fill_low = st.number_input(
                "Lý thuyết — tỉ lệ tối thiểu", min_value=0.5, max_value=1.0, value=0.90, step=0.01,
            )
            computer_fill_low = st.number_input(
                "Máy — tỉ lệ tối thiểu", min_value=0.5, max_value=1.0, value=0.85, step=0.01,
            )
        with c_hi:
            theory_fill_high = st.number_input(
                "Lý thuyết — tỉ lệ tối đa", min_value=0.5, max_value=1.0, value=1.00, step=0.01,
            )
            computer_fill_high = st.number_input(
                "Máy — tỉ lệ tối đa", min_value=0.5, max_value=1.0, value=0.95, step=0.01,
            )
        max_rooms_per_slot_per_format = st.number_input(
            "Ngưỡng mềm phòng/ca theo mỗi loại phòng",
            min_value=1,
            max_value=200,
            value=48,
            step=1,
            help="Ràng buộc mềm: ưu tiên không quá N phòng/ca cho từng mã loại phòng (1/2/3). Khuyên <50 để phù hợp năng lực coi thi thực tế.",
        )

# ---------------------------------------------------------------------------
# Upload + Run
# ---------------------------------------------------------------------------

up_col1, up_col2 = st.columns(2)
with up_col1:
    plan_file = st.file_uploader(
        "Bước 1 — Kế hoạch thi (Ke_hoach_thi.xlsx)",
        type=["xlsx"],
        help=(
            "Bắt buộc. Cột «Ngày BD», «Ngày kết thúc» và «Khoa_lop*» (4 ký tự = hậu tố MalopHP). "
            "Mỗi lớp/khóa có đợt thi riêng; hệ thống chỉ xếp ca trong khoảng ngày tương ứng."
        ),
    )
    reg_file = st.file_uploader(
        "Bước 2 — Danh sách sinh viên đăng ký (DSSV_*.xlsx)",
        type=["xlsx"],
        help=(
            "Bắt buộc. Cột tối thiểu: MaHS, TenSV, TenLopHP, SoTC và (MalopHP hoặc MaHocPhan). "
            "Khuyến nghị dùng thêm Khóa, Khóa_Lớp, MaHocPhan để engine không phải cắt từ MalopHP. "
            "Tuỳ chọn MaHinhThuc: 1 = tự luận, 2 = trắc nghiệm máy, 3 = vấn đáp (để trống thì suy từ tên môn). "
            "Quy tắc Khoa_nhom: **4 ký tự cuối** MalopHP — hai môn khác học phần mà trùng hậu tố này không thi cùng một ngày."
        ),
    )
with up_col2:
    rooms_file = st.file_uploader(
        "Bước 3 — Danh sách phòng (tuỳ chọn)",
        type=["xlsx"],
        help=(
            "Mã phòng (RoomID / Phòng…), sức chứa (Capacity / Số lượng…). "
            "Tuỳ chọn: khu; cột loại phòng (RoomType / «Mã ghép hình thức thi») dùng mã 1/2/3 giống môn thi."
        ),
    )
    invigilators_file = st.file_uploader(
        "Bước 4 — Danh sách giám thị (tuỳ chọn)",
        type=["xlsx"],
        help="Cột: InvigilatorID (mã), FullName (họ tên). Tuỳ chọn: MaxSessionsPerDay, MaxSessionsTotal.",
    )

run_btn = st.button("🚀 Xếp lịch thi", type="primary", use_container_width=True)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

if run_btn:
    if not plan_file or not reg_file:
        st.error("Bạn cần upload ít nhất 2 file: kế hoạch thi và danh sách đăng ký môn.")
        st.stop()
    try:
        with st.spinner("Đang đọc dữ liệu..."):
            plan_path = _save_uploaded(plan_file)
            reg_path = _save_uploaded(reg_file)
            rooms_path = _save_uploaded(rooms_file) if rooms_file else None
            inv_path = _save_uploaded(invigilators_file) if invigilators_file else None

            window = load_schedule_window(plan_path)
            session_labels, allowed_by_type, session_half = _build_slot_config(
                theory_text, oral_text, computer_text
            )
            window.sessions_per_day = len(session_labels)
            if window.has_per_cohort_windows:
                n_distinct = len({(a, b) for a, b in window.khoa_lop_windows.values()})
                st.info(
                    f"Kế hoạch thi: **{len(window.khoa_lop_windows)}** mã Khoa_lop* "
                    f"({n_distinct} đợt ngày). Khung tổng {window.start_date} → {window.end_date}; "
                    "mỗi ca chỉ xếp trong đợt của lớp (4 ký tự cuối MalopHP)."
                )

            registrations = load_registrations(reg_path)
            rooms = load_rooms(rooms_path)
            invigilators = load_invigilators(inv_path)
            # Auto soft_slot_cap nếu user để 0
            effective_cap = int(soft_slot_cap_value)
            if effective_cap <= 0:
                effective_cap = int(sum(r.capacity for r in rooms) * 0.95) if rooms else 1500
            exams, student_ref, student_cohort, student_cohort_codes = build_exams(
                registrations,
                prep_day_per_credit=float(prep_day_per_credit),
                max_exam_size=int(max_exam_size) if enable_split else None,
                khoa_lop_windows=window.khoa_lop_windows or None,
            )
            y1_anchor = int(year1_cohort_anchor or 0)
            student_cohort_codes = build_student_cohort_code_map(
                exams, registrations=registrations, year1_anchor=y1_anchor
            )
            student_cohort = build_student_cohort_map(
                exams,
                registrations=registrations,
                year1_anchor=y1_anchor,
                student_cohort_codes=student_cohort_codes,
            )
            allowed_by_exam = {
                e.exam_id: allowed_by_type.get(e.exam_type, allowed_by_type["theory"])
                for e in exams
            }
            student_name_map = {sid: r.student_name for sid, r in student_ref.items()}
            data_dir = Path(__file__).resolve().parent / "data"
            common_ref = data_dir / "2510DanhSachThiChung.xlsx"
            inv_ref = data_dir / "ALL_ALL_LThiGV_2510.xlsx"
            manual_pattern = build_manual_pattern_profile(
                common_schedule_path=common_ref if common_ref.exists() else None,
                invigilator_schedule_path=inv_ref if inv_ref.exists() else None,
                session_labels=session_labels,
            )
            if manual_pattern.notes and float(pattern_weight) > 0:
                st.info("Pattern học từ lịch chia tay:\n\n" + "\n".join(f"- {n}" for n in manual_pattern.notes))

        outcome = _run_pipeline(
            exams=exams,
            window=window,
            rooms=rooms,
            invigilators=invigilators,
            session_labels=session_labels,
            session_half=session_half,
            allowed_sessions_by_exam_id=allowed_by_exam,
            allowed_sessions_by_type=allowed_by_type,
            student_name_map=student_name_map,
            prep_day_per_credit=prep_day_per_credit,
            min_prep_days=min_prep_days,
            max_exams_per_day=max_exams_per_day,
            solver_time_limit=solver_time_limit,
            invigilators_per_room=invigilators_per_room,
            optimize=optimize_objective,
            balance_weight=balance_weight,
            soft_slot_cap=effective_cap,
            lns_iterations=lns_iterations,
            auto_relax=auto_relax_on_infeasible,
            theory_fill_low=theory_fill_low,
            theory_fill_high=theory_fill_high,
            computer_fill_low=computer_fill_low,
            computer_fill_high=computer_fill_high,
            weekend_large_course_min_students=int(weekend_large_min_sv),
            spread_prep_factor=float(spread_prep_factor),
            student_cohort=student_cohort,
            student_cohort_codes=student_cohort_codes,
            year1_cohort_anchor=y1_anchor,
            year1_allow_same_day=bool(year1_allow_same_day),
            preferred_session_by_prefix7=manual_pattern.preferred_session_by_prefix7,
            weekday_session_bonus=manual_pattern.weekday_session_bonus,
            pattern_weight=float(pattern_weight),
            target_students_per_room_by_exam_format=manual_pattern.target_students_per_room_by_exam_format,
            preferred_zone_by_session_label=manual_pattern.preferred_zone_by_session_label,
            max_rooms_per_slot_per_format=int(max_rooms_per_slot_per_format),
        )

        st.session_state["scheduler_data"] = {
            "window": window,
            "session_labels": session_labels,
            "session_half": session_half,
            "allowed_by_type": allowed_by_type,
            "allowed_by_exam": allowed_by_exam,
            "registrations": registrations,
            "rooms": rooms,
            "invigilators": invigilators,
            "exams": exams,
            "student_name_map": student_name_map,
            "student_cohort": student_cohort,
            "student_cohort_codes": student_cohort_codes,
            "outcome": outcome,
            "config": {
                "year1_cohort_anchor": y1_anchor,
                "year1_allow_same_day": bool(year1_allow_same_day),
                "prep_day_per_credit": float(prep_day_per_credit),
                "min_prep_days": float(min_prep_days),
                "max_exams_per_day": int(max_exams_per_day),
                "solver_time_limit": float(solver_time_limit),
                "invigilators_per_room": int(invigilators_per_room),
                "optimize": bool(optimize_objective),
                "auto_relax": bool(auto_relax_on_infeasible),
                "balance_weight": float(balance_weight),
                "soft_slot_cap": int(effective_cap),
                "lns_iterations": int(lns_iterations),
                "enable_split": bool(enable_split),
                "max_exam_size": int(max_exam_size) if enable_split else None,
                "theory_fill_low": float(theory_fill_low),
                "theory_fill_high": float(theory_fill_high),
                "computer_fill_low": float(computer_fill_low),
                "computer_fill_high": float(computer_fill_high),
                "session_half": list(session_half),
                "weekend_large_course_min_students": int(weekend_large_min_sv),
                "spread_prep_factor": float(spread_prep_factor),
                "pattern_weight": float(pattern_weight),
                "preferred_session_by_prefix7": dict(manual_pattern.preferred_session_by_prefix7),
                "weekday_session_bonus": {
                    f"{k[0]}_{k[1]}": float(v) for k, v in manual_pattern.weekday_session_bonus.items()
                },
                "target_students_per_room_by_exam_format": dict(
                    manual_pattern.target_students_per_room_by_exam_format
                ),
                "preferred_zone_by_session_label": dict(manual_pattern.preferred_zone_by_session_label),
                "max_rooms_per_slot_per_format": int(max_rooms_per_slot_per_format),
            },
        }
        n_placed = len(outcome["result"].scheduled)
        n_unplaced = len(getattr(outcome["result"].stats, "unplaced_exam_ids", None) or [])
        if n_unplaced:
            st.warning(
                f"Chạy xong trong {outcome['result'].stats.elapsed_seconds:.1f}s — "
                f"**{n_unplaced} môn chưa có lịch** ({n_placed}/{len(exams)} môn đã xếp). "
                "Xem tab Tổng quan → bảng «Môn chưa xếp»."
            )
        else:
            st.success(
                f"Xếp lịch hoàn tất trong {outcome['result'].stats.elapsed_seconds:.1f}s — "
                f"đã đặt {n_placed}/{len(exams)} môn."
            )
    except Exception as exc:  # noqa: BLE001
        st.error(f"❌ Lỗi khi xếp lịch: {exc}")
        st.exception(exc)


# ---------------------------------------------------------------------------
# Results UI (multi-tab)
# ---------------------------------------------------------------------------

state = st.session_state.get("scheduler_data")
if not state:
    st.info(
        "Chọn **hai file bắt buộc** (kế hoạch thi + danh sách đăng ký), tuỳ chọn thêm phòng và giám thị, "
        "rồi bấm **Xếp lịch thi**."
    )
    st.stop()

window = state["window"]
exams = state["exams"]
outcome = state["outcome"]
result = outcome["result"]
diagnose_report = outcome["diagnose"]
violations = outcome["violations"]
kpi = outcome["kpi"]
room_report = outcome["room_report"]
session_labels = state["session_labels"]
student_name_map = state["student_name_map"]

unplaced_diag = list(getattr(result, "unplaced_diagnostics", None) or [])
unplaced_ids = list(getattr(result.stats, "unplaced_exam_ids", None) or [])
if not unplaced_ids and unplaced_diag:
    unplaced_ids = [d.exam_id for d in unplaced_diag]

tabs = st.tabs(
    [
        "📊 Tổng quan & KPI",
        "🔍 Chẩn đoán",
        "📅 Lịch theo môn",
        "👤 Lịch theo SV",
        "🏫 Phòng & giám thị",
        "✍️ Đổi lịch thủ công",
    ]
)

# ---- Tab 1: Tổng quan & KPI ----
with tabs[0]:
    st.subheader("Tóm tắt")
    if unplaced_ids:
        st.error(
            f"**CẢNH BÁO NGHIÊM TRỌNG — {len(unplaced_ids)} môn chưa có lịch thi** "
            f"({len(result.scheduled)}/{result.stats.num_exams} môn đã xếp). "
            "Sinh viên đăng ký các môn này **không có ngày/giờ thi** trong file xuất ra. "
            "Không được coi là «xếp xong»."
        )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Môn đã xếp", f"{len(result.scheduled)} / {result.stats.num_exams}")
    c2.metric("Sinh viên", f"{result.stats.num_students:,}")
    c3.metric("Ô thời gian đã dùng / tổng", f"{kpi.slots_used} / {result.stats.num_slots}")
    c4.metric(
        "Cặp môn thiếu nghỉ ôn (ước lượng)",
        f"{kpi.prep_violation_count:,}",
        delta=(
            f"Trong đó thi cùng ngày: {kpi.same_day_violation_count:,}"
            if kpi.same_day_violation_count
            else "Không có trường hợp cùng ngày"
        ),
        delta_color=("inverse" if kpi.same_day_violation_count else "normal"),
        help=(
            "Đếm các lần sinh viên thi hai môn liên tiếp mà khoảng nghỉ thực tế nhỏ hơn "
            "mức «Số ngày nghỉ ôn / tín chỉ» bạn cấu hình. "
            "«Cùng ngày» là trường hợp nặng nhất (0 ngày nghỉ giữa hai môn)."
        ),
    )

    _show_hoc_phan_prep_hard_errors(kpi)

    if kpi.prep_violation_students_year1 or kpi.prep_violation_count_year1:
        ck = getattr(kpi, "newest_cohort_code", 0) or 0
        st.info(
            f"**SV năm 1 (niên khóa {ck:02d}):** {kpi.prep_violation_students_year1:,} SV / "
            f"{kpi.prep_violation_count_year1:,} cặp **chưa đủ ngày ôn** — mục tiêu **0**. "
            f"(Thi cùng ngày vẫn được phép; KPI đếm cặp môn liên tiếp mà khoảng ôn thực tế < yêu cầu.)"
        )
    elif getattr(kpi, "newest_cohort_code", 0):
        st.success(
            f"Niên khóa năm 1 (mã {kpi.newest_cohort_code:02d}): không ghi nhận SV vi phạm ngày ôn."
        )

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Phương thức giải", hien_thi_phuong_thuc(result.stats.method))
    c6.metric("Thời gian giải", f"{result.stats.elapsed_seconds:.1f} giây")
    c7.metric("Tỉ lệ lấp ô thời gian", f"{kpi.slot_utilization*100:.1f}%")
    c8.metric("Vị trí PBL (0–1)", f"{kpi.pbl_position_score:.2f}",
              help="Gần 1 nghĩa là môn PBL/đồ án được xếp về cuối đợt thi nhiều hơn.")

    if kpi.by_day_load:
        st.markdown("**Tải sinh viên theo ngày**")
        load_df = pd.DataFrame(kpi.by_day_load, columns=["Ngày", "TổngSV"])
        st.bar_chart(load_df.set_index("Ngày"))

    if unplaced_diag:
        st.markdown("### Môn chưa xếp — nguyên nhân & gợi ý")
        st.dataframe(
            unplaced_to_dataframe(unplaced_diag),
            use_container_width=True,
            hide_index=True,
        )
    if result.stats.relaxations:
        st.warning("Hệ thống đã tự nới ràng buộc:\n\n" + "\n".join(f"- {r}" for r in result.stats.relaxations))
    if result.stats.notes:
        with st.expander("Chi tiết quá trình giải"):
            for n in result.stats.notes:
                st.write("•", n)

    # Ma trận số môn theo ngày × ca
    st.markdown("**Số môn theo ngày và ký hiệu ca**")
    schedule_df = schedule_to_dataframe(result.scheduled, exams)
    if not schedule_df.empty:
        pivot = (
            schedule_df.groupby(["Ngay_thi", "Ky_hieu_ca"]).size().unstack(fill_value=0)
        )
        st.dataframe(pivot, use_container_width=True, height=300)

# ---- Tab 2: Chẩn đoán ----
with tabs[1]:
    st.subheader("Chẩn đoán bài toán")
    c1, c2, c3 = st.columns(3)
    c1.metric("Cặp xung đột", f"{diagnose_report.num_conflicts:,}")
    c2.metric("Mật độ xung đột", f"{diagnose_report.conflict_density*100:.2f}%")
    c3.metric("Tối đa môn / sinh viên", diagnose_report.max_exams_per_student)

    if diagnose_report.errors:
        st.error("**Vấn đề cứng:**\n\n" + "\n".join(f"- {e}" for e in diagnose_report.errors))
    _show_hoc_phan_prep_hard_errors(kpi)
    if diagnose_report.warnings:
        st.warning("**Cảnh báo:**\n\n" + "\n".join(f"- {w}" for w in diagnose_report.warnings))
    if diagnose_report.info:
        st.info("**Thông tin:**\n\n" + "\n".join(f"- {i}" for i in diagnose_report.info))

    st.markdown("**Phân bố môn theo loại**")
    type_df = pd.DataFrame(
        [
            {
                "Loại môn": hien_thi_loai_hinh(k),
                "Số môn": v,
                "Số ô ca khả dụng": diagnose_report.by_type_slot_capacity.get(k, 0),
            }
            for k, v in diagnose_report.by_type_count.items()
        ]
    )
    st.dataframe(type_df, use_container_width=True, hide_index=True)

    st.markdown("**Danh sách vi phạm nghỉ ôn (tóm tắt)**")
    vios_df = violations_to_dataframe(
        violations, newest_cohort_code=getattr(kpi, "newest_cohort_code", 0) or 0
    )
    if vios_df.empty:
        st.success("Không có vi phạm ngày ôn.")
    else:
        top_courses = vios_df["Mon_thi_sau"].value_counts().head(15).reset_index()
        top_courses.columns = ["Môn thi sau", "Số vụ"]
        st.dataframe(top_courses, use_container_width=True, hide_index=True, height=320)

# ---- Tab 3: Lịch theo môn ----
with tabs[2]:
    st.subheader("Lịch thi theo môn / lớp")
    exam_df = exam_view_dataframe(result.scheduled, exams)
    if exam_df.empty:
        st.warning("Chưa có dữ liệu lịch.")
    else:
        f1, f2, f3 = st.columns([2, 1, 1])
        with f1:
            kw = st.text_input("Tìm theo tên môn / mã học phần", "")
        with f2:
            loai = sorted(exam_df["Loai_hinh_thi"].dropna().unique().tolist())
            types = ["Tất cả"] + loai
            sel_type = st.selectbox("Loại hình thi", types, index=0)
        with f3:
            dates = sorted(exam_df["Ngay_thi"].unique().tolist())
            sel_dates = st.multiselect("Ngày thi", dates, default=dates)

        view = exam_df.copy()
        if kw.strip():
            mask = view["Ten_mon"].str.contains(kw.strip(), case=False, na=False) | view["Ma_hoc_phan"].str.contains(
                kw.strip(), case=False, na=False
            )
            view = view[mask]
        if sel_type != "Tất cả":
            view = view[view["Loai_hinh_thi"] == sel_type]
        if sel_dates:
            view = view[view["Ngay_thi"].isin(sel_dates)]
        st.caption(f"Hiển thị {len(view)} / {len(exam_df)} môn.")
        st.dataframe(view, use_container_width=True, hide_index=True, height=520)

# ---- Tab 4: Lịch theo SV ----
with tabs[3]:
    st.subheader("Lịch thi theo sinh viên")
    student_df = student_view_dataframe(
        result.scheduled,
        exams,
        student_name_map,
        student_cohort_codes=state.get("student_cohort_codes"),
    )
    if student_df.empty:
        st.warning("Chưa có dữ liệu.")
    else:
        st.caption(f"Tổng cộng {student_df['Ma_sinh_vien'].nunique()} sinh viên.")
        col_a, col_b = st.columns([1, 2])
        with col_a:
            kw = st.text_input("Tìm theo mã / tên sinh viên", "")
        view = student_df
        if kw.strip():
            mask = view["Ma_sinh_vien"].astype(str).str.contains(kw.strip(), case=False) | view[
                "Ten_sinh_vien"
            ].str.contains(kw.strip(), case=False, na=False)
            view = view[mask]
        st.dataframe(view, use_container_width=True, hide_index=True, height=520)

# ---- Tab 5: Phòng & giám thị ----
with tabs[4]:
    st.subheader("Phân phòng & giám thị")
    rooms = state["rooms"]
    invigilators = state["invigilators"]
    if not rooms:
        st.info("Chưa upload danh sách phòng — bỏ qua phần phân phòng.")
    else:
        c1, c2 = st.columns(2)
        c1.metric("Phòng đang dùng", f"{len(room_report.room_usage)} / {len(rooms)}")
        c2.metric("Quá tải", len(room_report.overflows))
        if room_report.overflows:
            st.error("Sự cố sức chứa:\n\n" + "\n".join(f"- {x}" for x in room_report.overflows[:30]))
        if getattr(room_report, "soft_warnings", None):
            st.warning(
                "Cảnh báo mềm:\n\n"
                + "\n".join(f"- {x}" for x in (room_report.soft_warnings[:20]))
            )
        if room_report.room_usage:
            usage_df = pd.DataFrame(
                sorted(room_report.room_usage.items(), key=lambda x: -x[1]),
                columns=["Ma_phong", "So_ca_dung"],
            )
            st.markdown("**Tải phòng**")
            st.bar_chart(usage_df.set_index("Ma_phong").head(40))

    if not invigilators:
        st.info("Chưa upload danh sách giám thị — bỏ qua.")
    else:
        if room_report.invigilator_shortage:
            st.error(
                "Thiếu giám thị:\n\n"
                + "\n".join(f"- {x}" for x in room_report.invigilator_shortage[:30])
            )
        usage = room_report.invigilator_usage
        if usage:
            inv_df = pd.DataFrame(
                sorted(usage.items(), key=lambda x: -x[1]),
                columns=["Ma_giam_thi", "So_ca"],
            )
            st.dataframe(inv_df, use_container_width=True, hide_index=True, height=320)

# ---- Tab 6: Đổi lịch thủ công ----
with tabs[5]:
    st.subheader("Đổi lịch thủ công và tự sửa lịch")
    st.caption(
        "Chọn một môn cần đổi ngày hoặc ca; hệ thống sẽ giữ lịch các môn khác gần như cũ nhất có thể."
    )
    exam_lookup = {e.exam_id: e for e in exams}
    schedule_lookup = {item.exam_id: item for item in result.scheduled}
    options = sorted(
        [
            (
                eid,
                f"{eid} | {schedule_lookup[eid].exam_date} ca {schedule_lookup[eid].session} "
                f"{getattr(schedule_lookup[eid], 'session_label', '')} | {exam_lookup[eid].course_name}",
            )
            for eid in schedule_lookup.keys()
        ],
        key=lambda x: x[1],
    )
    if not options:
        st.warning("Chưa có lịch thi để đổi.")
    else:
        label_to_eid = {label: eid for eid, label in options}
        selected_label = st.selectbox("Môn / lớp", [lbl for _, lbl in options], index=0)
        selected_eid = label_to_eid[selected_label]
        current_item = schedule_lookup[selected_eid]

        c1, c2 = st.columns(2)
        with c1:
            new_date = st.date_input(
                "Ngày thi mới",
                value=current_item.exam_date,
                min_value=window.start_date,
                max_value=window.end_date,
            )
        with c2:
            session_choices = list(range(1, window.sessions_per_day + 1))
            new_session = st.selectbox(
                "Ca thi mới",
                session_choices,
                index=current_item.session - 1,
                format_func=lambda x: f"{x} - {session_labels[x-1] if x-1 < len(session_labels) else ''}",
            )

        if st.button("Áp dụng đổi lịch và tự sửa", use_container_width=True):
            try:
                day_idx = (new_date - window.start_date).days
                target_slot = day_idx * window.sessions_per_day + (int(new_session) - 1)
                base_slots = {}
                for item in result.scheduled:
                    d_off = (item.exam_date - window.start_date).days
                    base_slots[item.exam_id] = d_off * window.sessions_per_day + (item.session - 1)
                fixed_slots = {selected_eid: target_slot}
                cfg = state["config"]
                sh = state.get("session_half") or cfg.get("session_half") or [0] * len(session_labels)
                new_outcome = _run_pipeline(
                    exams=exams,
                    window=window,
                    rooms=state["rooms"],
                    invigilators=state["invigilators"],
                    session_labels=session_labels,
                    session_half=sh,
                    allowed_sessions_by_exam_id=state["allowed_by_exam"],
                    allowed_sessions_by_type=state["allowed_by_type"],
                    student_name_map=student_name_map,
                    prep_day_per_credit=cfg["prep_day_per_credit"],
                    min_prep_days=cfg["min_prep_days"],
                    max_exams_per_day=cfg["max_exams_per_day"],
                    solver_time_limit=cfg["solver_time_limit"],
                    invigilators_per_room=cfg["invigilators_per_room"],
                    optimize=cfg["optimize"],
                    balance_weight=cfg.get("balance_weight", 0.12),
                    soft_slot_cap=cfg.get("soft_slot_cap", 1100),
                    lns_iterations=cfg.get("lns_iterations", 7),
                    auto_relax=cfg["auto_relax"],
                    fixed_slots=fixed_slots,
                    base_slots=base_slots,
                    theory_fill_low=cfg.get("theory_fill_low", 0.90),
                    theory_fill_high=cfg.get("theory_fill_high", 1.00),
                    computer_fill_low=cfg.get("computer_fill_low", 0.85),
                    computer_fill_high=cfg.get("computer_fill_high", 0.95),
                    weekend_large_course_min_students=int(
                        cfg.get("weekend_large_course_min_students", 0)
                    ),
                    spread_prep_factor=float(cfg.get("spread_prep_factor", 2.4)),
                    student_cohort=state.get("student_cohort"),
                    student_cohort_codes=state.get("student_cohort_codes"),
                    year1_cohort_anchor=int(cfg.get("year1_cohort_anchor", 0) or 0),
                    year1_allow_same_day=bool(cfg.get("year1_allow_same_day", True)),
                    preferred_session_by_prefix7=cfg.get("preferred_session_by_prefix7", {}),
                    weekday_session_bonus={
                        tuple(map(int, k.split("_"))): float(v)
                        for k, v in (cfg.get("weekday_session_bonus", {}) or {}).items()
                        if "_" in str(k)
                    },
                    pattern_weight=float(cfg.get("pattern_weight", 1.0)),
                    target_students_per_room_by_exam_format=cfg.get(
                        "target_students_per_room_by_exam_format", {}
                    ),
                    preferred_zone_by_session_label=cfg.get("preferred_zone_by_session_label", {}),
                    max_rooms_per_slot_per_format=int(cfg.get("max_rooms_per_slot_per_format", 48)),
                )
                state["outcome"] = new_outcome
                st.session_state["scheduler_data"] = state
                st.success("Đổi lịch thành công; đã tự sửa toàn bộ lịch liên quan.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Lỗi đổi lịch: {exc}")

# ---- Export ----
st.markdown("---")
st.subheader("📦 Xuất file Excel")
schedule_df = schedule_to_dataframe(result.scheduled, exams)
vios_df = violations_to_dataframe(
    violations, newest_cohort_code=getattr(kpi, "newest_cohort_code", 0) or 0
)
student_df = student_view_dataframe(
    result.scheduled,
    exams,
    student_name_map,
    student_cohort_codes=state.get("student_cohort_codes"),
)

kpi_rows = [
    ("Phương thức giải", hien_thi_phuong_thuc(result.stats.method)),
    ("Thời gian giải (giây)", round(result.stats.elapsed_seconds, 2)),
    ("Số môn đã xếp", len(result.scheduled)),
    ("Tổng số môn", result.stats.num_exams),
    ("Số sinh viên", result.stats.num_students),
    ("Ô thời gian đã dùng", kpi.slots_used),
    ("Tổng số ô thời gian", result.stats.num_slots),
    ("Số cặp môn thiếu nghỉ ôn (ước lượng)", kpi.prep_violation_count),
    (
        f"Cặp vi phạm ôn — SV niên khóa {getattr(kpi, 'newest_cohort_code', 0) or 0:02d}",
        kpi.prep_violation_count_year1,
    ),
    (
        f"SV niên khóa {getattr(kpi, 'newest_cohort_code', 0) or 0:02d} vi phạm ôn",
        kpi.prep_violation_students_year1,
    ),
    ("Niên khóa SV năm 1 (cấu hình)", getattr(kpi, "newest_cohort_code", 0)),
    ("Số cặp thi cùng ngày (0 ngày nghỉ)", kpi.same_day_violation_count),
    ("Số SV bị vi phạm cùng-ngày", kpi.same_day_violation_students),
    ("Khoảng ôn trung bình của các cặp vi phạm (ngày)", round(kpi.avg_prep_gap, 2)),
    ("Khoảng ôn nhỏ nhất ghi nhận (ngày)", round(kpi.min_prep_gap, 2)),
    ("Điểm vị trí PBL (0–1)", round(kpi.pbl_position_score, 3)),
    ("Trung bình SV / ca", round(kpi.avg_students_per_slot, 1)),
    ("Cao nhất SV / ca", kpi.max_students_per_slot),
]
if unplaced_ids:
    kpi_rows.insert(0, ("CẢNH BÁO: Số môn chưa có lịch thi", len(unplaced_ids)))
for msg in getattr(kpi, "prep_prefix_hard_errors", None) or []:
    kpi_rows.append(("Cảnh báo học phần: >10% SV thiếu nghỉ ôn", msg))

unplaced_df = unplaced_to_dataframe(unplaced_diag) if unplaced_diag else None
excel_bytes = to_excel_bytes(schedule_df, vios_df, student_df, kpi_rows, unplaced_df=unplaced_df)
st.download_button(
    "⬇️ Tải file kết quả (Excel)",
    data=excel_bytes,
    file_name="ket_qua_xep_lich_thi.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)
