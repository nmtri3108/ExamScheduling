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

from engine.diagnostics import compute_kpi, diagnose
from engine.exporters import (
    exam_view_dataframe,
    schedule_to_dataframe,
    student_view_dataframe,
    to_excel_bytes,
    violations_to_dataframe,
)
from engine.io import (
    build_exams,
    load_invigilators,
    load_registrations,
    load_rooms,
    load_schedule_window,
)
from engine.i18n import hien_thi_loai_hinh, hien_thi_phuong_thuc
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


def _build_slot_config(theory: str, oral: str, computer: str):
    by_type_labels = {
        "theory": _parse_csv(theory),
        "oral": _parse_csv(oral),
        "computer": _parse_csv(computer),
    }
    if any(len(v) == 0 for v in by_type_labels.values()):
        raise ValueError("Mỗi nhóm ca thi (lý thuyết / PBL / trắc nghiệm máy) phải có ≥ 1 ca.")
    all_labels: List[str] = []
    for labels in by_type_labels.values():
        for lbl in labels:
            if lbl not in all_labels:
                all_labels.append(lbl)
    allowed_by_type: Dict[str, List[int]] = {}
    for etype, labels in by_type_labels.items():
        allowed_by_type[etype] = [all_labels.index(l) for l in labels]
    return all_labels, allowed_by_type


def _run_pipeline(
    exams,
    window,
    rooms,
    invigilators,
    session_labels,
    allowed_sessions_by_exam_id,
    allowed_sessions_by_type,
    student_name_map,
    prep_day_per_credit,
    min_prep_days,
    max_exams_per_day,
    solver_time_limit,
    invigilators_per_room,
    optimize,
    balance_weight=1.0,
    soft_slot_cap=0,
    lns_iterations=3,
    fixed_slots=None,
    base_slots=None,
    auto_relax=True,
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
    )

    result = solve(
        exams=exams,
        window=window,
        rooms=rooms,
        allowed_sessions_by_exam_id=allowed_sessions_by_exam_id,
        session_labels=session_labels,
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
    )

    room_report = assign_rooms_and_invigilators(
        scheduled=result.scheduled,
        exams=exams,
        rooms=rooms,
        invigilators=invigilators,
        invigilators_per_room=int(invigilators_per_room),
    )

    violations = detect_prep_violations(
        scheduled=result.scheduled,
        exams=exams,
        student_name_map=student_name_map,
        prep_day_per_credit=float(prep_day_per_credit),
    )
    kpi = compute_kpi(result.scheduled, exams, window, violations)
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
    "Kết hợp thuật toán tham lam (DSATUR) và tối ưu ràng buộc (SAT): luôn có lịch khả thi, "
    "ưu tiên ngày ôn và xếp môn PBL/đồ án về cuối đợt."
)

with st.sidebar:
    st.header("⚙️ Cấu hình")

    with st.expander("Tham số học vụ", expanded=True):
        prep_day_per_credit = st.number_input(
            "Số ngày ôn / 1 tín chỉ", min_value=0.1, value=0.6, step=0.1,
            help="Ví dụ 0.6 ngày × 3 TC = ~2 ngày ôn cho môn 3 TC.",
        )
        min_prep_days = st.number_input(
            "Số ngày ôn tối thiểu (cứng)", min_value=0.0, value=0.0, step=0.5,
            help="0 = không ép cứng. Lớn hơn 0 = bắt buộc khoảng cách tối thiểu (ngày) giữa hai môn có chung sinh viên.",
        )
        max_exams_per_day = st.number_input(
            "Tối đa môn / sinh viên / ngày", min_value=1, value=2, step=1,
        )

    with st.expander("Bộ ca thi theo loại", expanded=True):
        theory_text = st.text_input(
            "Ca lý thuyết (cách nhau bằng dấu phẩy)", value="2C1,2C2,2C3,2C4",
            help="Danh sách ký hiệu ca cho môn lý thuyết, ví dụ 2C1 là ca 1 buổi 2.",
        )
        oral_text = st.text_input(
            "Ca PBL / đồ án / vấn đáp (cách nhau bằng dấu phẩy)", value="1A1,1P1",
        )
        computer_text = st.text_input(
            "Ca trắc nghiệm máy tính (cách nhau bằng dấu phẩy)", value="3C1,3C2,3C3,3C4,3C5,3C6",
        )

    with st.expander("Bộ giải và tối ưu", expanded=False):
        solver_time_limit = st.number_input(
            "Thời gian tối đa (giây)", min_value=10, value=120, step=10,
            help="Giới hạn thời gian cho bước tối ưu SAT. Bước tham lam thường dưới 30 giây.",
        )
        optimize_objective = st.checkbox(
            "Bật tối ưu (LNS + bước SAT)", value=True,
            help="Bước cải tiến cục bộ (LNS) chạy nhanh. Tối ưu ràng buộc (SAT) chỉ chạy khi bài toán đủ nhỏ.",
        )
        auto_relax_on_infeasible = st.checkbox(
            "Tự nới ràng buộc khi vô nghiệm", value=True,
        )

    with st.expander("Tách môn lớn (tự động)", expanded=True):
        st.caption(
            "Khi một môn có quá nhiều SV (ví dụ Triết MLN ~4000 SV), bật chế độ này "
            "để hệ thống chia thành nhiều ca thi khác nhau, mỗi ca dùng đề riêng."
        )
        enable_split = st.checkbox(
            "Bật tách môn lớn (khuyên dùng cho trường lớn)",
            value=True,
            help="Bật để tránh các môn 'siêu lớn' (như Triết MLN ~4000 SV) tạo peak không thể bố trí phòng.",
        )
        max_exam_size = st.number_input(
            "Ngưỡng SV tối đa / 1 ca thi",
            min_value=200, max_value=10000, value=1500, step=100,
            disabled=not enable_split,
            help=(
                "Môn vượt ngưỡng sẽ tự chia thành nhiều ca thi khác nhau "
                "(mỗi ca một đề, phân theo lớp học phần). "
                "1500 là mặc định cân đối; giảm nếu ít phòng."
            ),
        )

    with st.expander("Phân bố tải", expanded=True):
        balance_mode = st.radio(
            "Mục tiêu phân bố",
            options=[
                "Ưu tiên ngày ôn (mặc định)",
                "Cân bằng tải (trải đều sinh viên theo ngày)",
                "Cân bằng mạnh (trải đều, có thể tăng vi phạm ngày ôn)",
                "Nén lịch (dùng ít ngày nhất)",
            ],
            index=0,
            help=(
                "Ưu tiên ngày ôn: nhiều khoảng nghỉ giữa các môn — có thể dồn đầu/cuối đợt.\n"
                "Cân bằng: hoà giữa ngày ôn và đều tải theo ngày.\n"
                "Nén: gom môn vào ít ngày — dễ quá tải phòng."
            ),
        )
        if balance_mode.startswith("Cân bằng mạnh"):
            balance_weight = 1.0
        elif balance_mode.startswith("Cân bằng tải"):
            balance_weight = 0.5
        elif balance_mode.startswith("Nén"):
            balance_weight = 0.05
        else:
            balance_weight = 0.3
        soft_slot_cap_value = st.number_input(
            "Ngưỡng mềm số SV / ca (0 = tự động theo phòng hoặc 1500)",
            min_value=0,
            value=1500,
            step=100,
            help=(
                "Phạt mạnh khi một ca có quá nhiều sinh viên. "
                "0 = tự lấy theo tổng sức chứa phòng (nếu có file phòng), không thì dùng 1500."
            ),
        )
        lns_iterations = st.slider(
            "Số vòng cải tiến LNS (giảm vi phạm ngày ôn)",
            min_value=0, max_value=8, value=3, step=1,
            help=(
                "Mỗi vòng thử chuyển khoảng 120 môn vi phạm nhiều nhất sang ô thời gian tốt hơn. "
                "Ba vòng thường giảm mạnh vi phạm. 0 = tắt bước này."
            ),
        )

    with st.expander("Phòng & giám thị", expanded=False):
        invigilators_per_room = st.number_input(
            "Giám thị / phòng", min_value=1, value=2, step=1,
        )

# ---------------------------------------------------------------------------
# Upload + Run
# ---------------------------------------------------------------------------

up_col1, up_col2 = st.columns(2)
with up_col1:
    plan_file = st.file_uploader(
        "1) Kế hoạch thi (Ke_hoach_thi.xlsx) — bắt buộc",
        type=["xlsx"],
        help="Cần có cột 'Ngày BD', 'Ngày kết thúc'.",
    )
    reg_file = st.file_uploader(
        "2) Danh sách SV đăng ký môn (DSSV_*.xlsx) — bắt buộc",
        type=["xlsx"],
        help="Cột bắt buộc: MaHS, TenSV, MalopHP, TenLopHP, SoTC.",
    )
with up_col2:
    rooms_file = st.file_uploader(
        "3) Phòng thi (tuỳ chọn)",
        type=["xlsx"],
        help="Cột trong Excel: RoomID (mã phòng), Location (vị trí), Capacity (sức chứa).",
    )
    invigilators_file = st.file_uploader(
        "4) Giám thị (tuỳ chọn)",
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
            session_labels, allowed_by_type = _build_slot_config(theory_text, oral_text, computer_text)
            window.sessions_per_day = len(session_labels)

            registrations = load_registrations(reg_path)
            rooms = load_rooms(rooms_path)
            invigilators = load_invigilators(inv_path)
            # Auto soft_slot_cap nếu user để 0
            effective_cap = int(soft_slot_cap_value)
            if effective_cap <= 0:
                effective_cap = int(sum(r.capacity for r in rooms) * 0.95) if rooms else 1500
            exams, student_ref = build_exams(
                registrations,
                prep_day_per_credit=float(prep_day_per_credit),
                max_exam_size=int(max_exam_size) if enable_split else None,
            )
            allowed_by_exam = {
                e.exam_id: allowed_by_type.get(e.exam_type, allowed_by_type["theory"])
                for e in exams
            }
            student_name_map = {sid: r.student_name for sid, r in student_ref.items()}

        outcome = _run_pipeline(
            exams=exams,
            window=window,
            rooms=rooms,
            invigilators=invigilators,
            session_labels=session_labels,
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
        )

        st.session_state["scheduler_data"] = {
            "window": window,
            "session_labels": session_labels,
            "allowed_by_type": allowed_by_type,
            "allowed_by_exam": allowed_by_exam,
            "registrations": registrations,
            "rooms": rooms,
            "invigilators": invigilators,
            "exams": exams,
            "student_name_map": student_name_map,
            "outcome": outcome,
            "config": {
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
            },
        }
        st.success(
            f"Xếp lịch hoàn tất trong {outcome['result'].stats.elapsed_seconds:.1f} giây — "
            f"đã đặt {len(outcome['result'].scheduled)}/{len(exams)} môn."
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"❌ Lỗi khi xếp lịch: {exc}")
        st.exception(exc)


# ---------------------------------------------------------------------------
# Results UI (multi-tab)
# ---------------------------------------------------------------------------

state = st.session_state.get("scheduler_data")
if not state:
    st.info("👆 Upload file và bấm **Xếp lịch thi** để bắt đầu.")
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
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Môn đã xếp", f"{len(result.scheduled)} / {result.stats.num_exams}")
    c2.metric("Sinh viên", f"{result.stats.num_students:,}")
    c3.metric("Ô thời gian đã dùng / tổng", f"{kpi.slots_used} / {result.stats.num_slots}")
    c4.metric("Vi phạm ngày ôn", f"{kpi.prep_violation_count:,}")

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

    st.markdown("**Vi phạm ngày ôn (xem nhanh)**")
    vios_df = violations_to_dataframe(violations)
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
    student_df = student_view_dataframe(result.scheduled, exams, student_name_map)
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
                new_outcome = _run_pipeline(
                    exams=exams,
                    window=window,
                    rooms=state["rooms"],
                    invigilators=state["invigilators"],
                    session_labels=session_labels,
                    allowed_sessions_by_exam_id=state["allowed_by_exam"],
                    allowed_sessions_by_type=state["allowed_by_type"],
                    student_name_map=student_name_map,
                    prep_day_per_credit=cfg["prep_day_per_credit"],
                    min_prep_days=cfg["min_prep_days"],
                    max_exams_per_day=cfg["max_exams_per_day"],
                    solver_time_limit=cfg["solver_time_limit"],
                    invigilators_per_room=cfg["invigilators_per_room"],
                    optimize=cfg["optimize"],
                    balance_weight=cfg.get("balance_weight", 1.0),
                    soft_slot_cap=cfg.get("soft_slot_cap", 0),
                    lns_iterations=cfg.get("lns_iterations", 3),
                    auto_relax=cfg["auto_relax"],
                    fixed_slots=fixed_slots,
                    base_slots=base_slots,
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
vios_df = violations_to_dataframe(violations)
student_df = student_view_dataframe(result.scheduled, exams, student_name_map)

kpi_rows = [
    ("Phương thức giải", hien_thi_phuong_thuc(result.stats.method)),
    ("Thời gian giải (giây)", round(result.stats.elapsed_seconds, 2)),
    ("Số môn đã xếp", len(result.scheduled)),
    ("Tổng số môn", result.stats.num_exams),
    ("Số sinh viên", result.stats.num_students),
    ("Ô thời gian đã dùng", kpi.slots_used),
    ("Tổng số ô thời gian", result.stats.num_slots),
    ("Số vi phạm ngày ôn", kpi.prep_violation_count),
    ("Điểm vị trí PBL (0–1)", round(kpi.pbl_position_score, 3)),
    ("Trung bình SV / ca", round(kpi.avg_students_per_slot, 1)),
    ("Cao nhất SV / ca", kpi.max_students_per_slot),
]
excel_bytes = to_excel_bytes(schedule_df, vios_df, student_df, kpi_rows)
st.download_button(
    "⬇️ Tải file kết quả (Excel)",
    data=excel_bytes,
    file_name="ket_qua_xep_lich_thi.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)
