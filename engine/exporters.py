from __future__ import annotations

from io import BytesIO
from typing import Dict, List

import pandas as pd

from .diagnostics import cohort_code_from_malop
from .i18n import hien_thi_loai_hinh
from .models import Exam, PrepViolation, ScheduledExam


def _student_room_and_split_code(item: ScheduledExam, sid: str) -> tuple[str, str]:
    """Phòng vật lý và mã ghép cho một SV khi đã chia nhóm theo phòng."""
    groups = getattr(item, "room_student_groups", None) or []
    codes = getattr(item, "room_split_codes", None) or []
    rids = list(item.room_ids or [])
    if groups and rids and len(groups) == len(rids):
        for gi, grp in enumerate(groups):
            if sid in grp:
                rid = rids[gi] if gi < len(rids) else ""
                code = codes[gi] if gi < len(codes) else ""
                return rid, code
    if rids:
        c0 = codes[0] if codes else ""
        return rids[0], c0
    return "", ""


def _exam_malophp_cell(exam: Exam | None) -> str:
    """Chuỗi MalopHP xuất Excel — giữ nguyên mã lớp học phần từ đăng ký (có thể nhiều lớp, cách nhau bởi dấu phẩy)."""
    if exam is None:
        return ""
    parts = [str(s).strip() for s in exam.section_ids if str(s).strip()]
    return ", ".join(sorted(parts)) if parts else ""


def _exam_nien_khoa_cell(exam: Exam | None) -> str:
    """Niên khóa: 2 ký tự đầu của 4 ký tự cuối MalopHP (theo từng lớp trong ca thi)."""
    if exam is None:
        return ""
    explicit = sorted({str(c).strip() for c in getattr(exam, "cohort_codes", []) if str(c).strip()})
    if explicit:
        return ", ".join(explicit)
    codes = sorted(
        {c for s in exam.section_ids if (c := cohort_code_from_malop(s))}
    )
    return ", ".join(codes) if codes else ""


def schedule_to_dataframe(
    scheduled: List[ScheduledExam],
    exams: List[Exam] | None = None,
) -> pd.DataFrame:
    exam_map = {e.exam_id: e for e in exams} if exams else {}
    rows = []
    for s in scheduled:
        ex = exam_map.get(s.exam_id)
        tin_chi = float(ex.credits) if ex else None
        fmt = int(getattr(ex, "exam_format", 1) or 1) if ex else None
        pfx7 = str(getattr(ex, "course_prefix_7", "") or "") if ex else ""
        pri = int(ex.priority) if ex else None
        malop = _exam_malophp_cell(ex)
        nien_khoa = _exam_nien_khoa_cell(ex)
        codes = list(getattr(s, "room_split_codes", None) or [])
        rows.append(
            {
                "Ma_ca_thi": s.exam_id,
                "MalopHP": malop,
                "Nien_khoa": nien_khoa,
                "Ma_hoc_phan": s.course_id,
                "Ten_mon": s.course_name,
                "So_tin_chi": tin_chi,
                "Thu_tu_uu_tien": pri,
                "Ma_khoa_hoc_phan_7": pfx7,
                "Ma_hinh_thuc": fmt,
                "Ngay_thi": s.exam_date.isoformat(),
                "So_ca": s.session,
                "Ky_hieu_ca": getattr(s, "session_label", ""),
                "Ma_phong_chia": ", ".join(codes) if codes else "",
                "Phong": ", ".join(s.room_ids),
                "Giam_thi": ", ".join(s.invigilator_ids),
            }
        )
    return pd.DataFrame(rows)


def violations_to_dataframe(
    violations: List[PrepViolation],
    newest_cohort_code: int = 0,
) -> pd.DataFrame:
    anchor_s = f"{int(newest_cohort_code):02d}" if newest_cohort_code > 0 else ""
    rows = []
    for v in violations:
        code = str(getattr(v, "student_cohort_code", "") or "").strip()
        mk = int(getattr(v, "student_cohort", 0) or 0)
        display_code = code or (f"{mk:02d}" if mk > 0 else "")
        rows.append(
            {
                "Ma_sinh_vien": v.student_id,
                "Ten_sinh_vien": v.student_name,
                "Ma_khoa_SV": display_code,
                "La_khoa_moi_nhat": (
                    "Có"
                    if anchor_s and display_code == anchor_s
                    else ("Không" if display_code else "")
                ),
                "Mon_thi_truoc": v.earlier_exam,
                "Mon_thi_sau": v.later_exam,
                "Ma_ca_thi_sau": getattr(v, "later_exam_id", "") or "",
                "So_ngay_on_yeu_cau": v.required_days,
                "So_ngay_on_thuc_te": v.actual_days,
            }
        )
    return pd.DataFrame(rows)


def student_view_dataframe(
    scheduled: List[ScheduledExam],
    exams: List[Exam],
    student_name_map: Dict[str, str],
    student_cohort_codes: Dict[str, str] | None = None,
) -> pd.DataFrame:
    """Lịch theo từng sinh viên — dùng cho cả UI lẫn xuất file."""
    exam_map = {e.exam_id: e for e in exams}
    rows = []
    for item in scheduled:
        exam = exam_map.get(item.exam_id)
        if not exam:
            continue
        for sid in exam.student_ids:
            rid, split_code = _student_room_and_split_code(item, sid)
            phong_cell = rid if rid else ", ".join(item.room_ids)
            cohort = ""
            if student_cohort_codes:
                cohort = str(student_cohort_codes.get(sid, "") or "").strip()
            if not cohort and exam.section_ids:
                cohort = cohort_code_from_malop(exam.section_ids[0])
            rows.append(
                {
                    "Ma_sinh_vien": sid,
                    "Ten_sinh_vien": student_name_map.get(sid, ""),
                    "Ma_ca_thi": item.exam_id,
                    "MalopHP": _exam_malophp_cell(exam),
                    "Nien_khoa": cohort,
                    "Ma_hoc_phan": exam.course_id,
                    "Ten_mon": exam.course_name,
                    "So_tin_chi": float(exam.credits),
                    "Thu_tu_uu_tien": int(exam.priority),
                    "Ma_khoa_hoc_phan_7": str(getattr(exam, "course_prefix_7", "") or ""),
                    "Ma_hinh_thuc": int(getattr(exam, "exam_format", 1) or 1),
                    "Loai_hinh_thi": hien_thi_loai_hinh(exam.exam_type),
                    "Ngay_thi": item.exam_date.isoformat(),
                    "So_ca": item.session,
                    "Ky_hieu_ca": getattr(item, "session_label", ""),
                    "Ma_phong_chia": split_code,
                    "Phong": phong_cell,
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(["Ma_sinh_vien", "Ngay_thi", "So_ca"])


def exam_view_dataframe(scheduled: List[ScheduledExam], exams: List[Exam]) -> pd.DataFrame:
    exam_map = {e.exam_id: e for e in exams}
    rows = []
    for item in scheduled:
        exam = exam_map.get(item.exam_id)
        if not exam:
            continue
        codes = list(getattr(item, "room_split_codes", None) or [])
        rows.append(
            {
                "Ma_ca_thi": item.exam_id,
                "MalopHP": _exam_malophp_cell(exam),
                "Nien_khoa": _exam_nien_khoa_cell(exam),
                "Ma_hoc_phan": item.course_id,
                "Ten_mon": item.course_name,
                "So_tin_chi": float(exam.credits),
                "Thu_tu_uu_tien": int(exam.priority),
                "Ma_khoa_hoc_phan_7": str(getattr(exam, "course_prefix_7", "") or ""),
                "Ma_hinh_thuc": int(getattr(exam, "exam_format", 1) or 1),
                "Loai_hinh_thi": hien_thi_loai_hinh(exam.exam_type),
                "Ngay_thi": item.exam_date.isoformat(),
                "So_ca": item.session,
                "Ky_hieu_ca": getattr(item, "session_label", ""),
                "So_sinh_vien": exam.size,
                "So_lop": len(exam.section_ids),
                "Danh_sach_lop": ", ".join(exam.section_ids[:5])
                + ("..." if len(exam.section_ids) > 5 else ""),
                "Ma_phong_chia": ", ".join(codes) if codes else "",
                "Phong": ", ".join(item.room_ids),
                "Giam_thi": ", ".join(item.invigilator_ids),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(["Ngay_thi", "So_ca", "Ten_mon"])


def unplaced_to_dataframe(diagnostics: list) -> pd.DataFrame:
    rows = []
    for d in diagnostics or []:
        sug = getattr(d, "suggestions_vi", None) or []
        rows.append(
            {
                "Ma_ca_thi": getattr(d, "exam_id", ""),
                "Ten_mon": getattr(d, "course_name", ""),
                "Loai_hinh": getattr(d, "exam_type", ""),
                "So_SV": getattr(d, "size", 0),
                "SV_khoa_nam_1": getattr(d, "year1_student_count", 0),
                "So_cap_xung_dot": getattr(d, "conflict_pair_count", 0),
                "So_o_thu": getattr(d, "candidate_slots", 0),
                "Nguyen_nhan_chinh": getattr(d, "primary_blocker_vi", ""),
                "Chi_tiet": getattr(d, "detail_vi", ""),
                "Mon_xung_dot_nhieu": "; ".join(getattr(d, "top_conflict_courses", None) or []),
                "Goi_y": " | ".join(sug) if sug else "",
            }
        )
    return pd.DataFrame(rows)


def to_excel_bytes(
    schedule_df: pd.DataFrame,
    violations_df: pd.DataFrame,
    student_df: pd.DataFrame | None = None,
    kpi_rows: list[tuple[str, object]] | None = None,
    unplaced_df: pd.DataFrame | None = None,
) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        schedule_df.to_excel(writer, index=False, sheet_name="Lich_thi")
        if unplaced_df is not None and not unplaced_df.empty:
            unplaced_df.to_excel(writer, index=False, sheet_name="Mon_chua_xep")
        violations_df.to_excel(writer, index=False, sheet_name="Vi_pham_ngay_on")
        if student_df is not None and not student_df.empty:
            student_df.to_excel(writer, index=False, sheet_name="Theo_sinh_vien")
        if kpi_rows:
            kpi_df = pd.DataFrame(kpi_rows, columns=["Chi_so", "Gia_tri"])
            kpi_df.to_excel(writer, index=False, sheet_name="KPI")
    return output.getvalue()
