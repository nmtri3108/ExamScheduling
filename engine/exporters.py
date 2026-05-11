from __future__ import annotations

from dataclasses import asdict
from io import BytesIO
from typing import Dict, List

import pandas as pd

from .i18n import hien_thi_loai_hinh
from .models import Exam, PrepViolation, ScheduledExam


def schedule_to_dataframe(
    scheduled: List[ScheduledExam],
    exams: List[Exam] | None = None,
) -> pd.DataFrame:
    exam_map = {e.exam_id: e for e in exams} if exams else {}
    rows = []
    for s in scheduled:
        ex = exam_map.get(s.exam_id)
        tin_chi = float(ex.credits) if ex else None
        rows.append(
            {
                "Ma_ca_thi": s.exam_id,
                "Ma_hoc_phan": s.course_id,
                "Ten_mon": s.course_name,
                "So_tin_chi": tin_chi,
                "Ngay_thi": s.exam_date.isoformat(),
                "So_ca": s.session,
                "Ky_hieu_ca": getattr(s, "session_label", ""),
                "Phong": ", ".join(s.room_ids),
                "Giam_thi": ", ".join(s.invigilator_ids),
            }
        )
    return pd.DataFrame(rows)


def violations_to_dataframe(violations: List[PrepViolation]) -> pd.DataFrame:
    rows = []
    for v in violations:
        rows.append(
            {
                "Ma_sinh_vien": v.student_id,
                "Ten_sinh_vien": v.student_name,
                "Mon_thi_truoc": v.earlier_exam,
                "Mon_thi_sau": v.later_exam,
                "So_ngay_on_yeu_cau": v.required_days,
                "So_ngay_on_thuc_te": v.actual_days,
            }
        )
    return pd.DataFrame(rows)


def student_view_dataframe(
    scheduled: List[ScheduledExam],
    exams: List[Exam],
    student_name_map: Dict[str, str],
) -> pd.DataFrame:
    """Lịch theo từng sinh viên — dùng cho cả UI lẫn xuất file."""
    exam_map = {e.exam_id: e for e in exams}
    rows = []
    for item in scheduled:
        exam = exam_map.get(item.exam_id)
        if not exam:
            continue
        for sid in exam.student_ids:
            rows.append(
                {
                    "Ma_sinh_vien": sid,
                    "Ten_sinh_vien": student_name_map.get(sid, ""),
                    "Ma_ca_thi": item.exam_id,
                    "Ma_hoc_phan": exam.course_id,
                    "Ten_mon": exam.course_name,
                    "So_tin_chi": float(exam.credits),
                    "Loai_hinh_thi": hien_thi_loai_hinh(exam.exam_type),
                    "Ngay_thi": item.exam_date.isoformat(),
                    "So_ca": item.session,
                    "Ky_hieu_ca": getattr(item, "session_label", ""),
                    "Phong": ", ".join(item.room_ids),
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
        rows.append(
            {
                "Ma_ca_thi": item.exam_id,
                "Ma_hoc_phan": item.course_id,
                "Ten_mon": item.course_name,
                "So_tin_chi": float(exam.credits),
                "Loai_hinh_thi": hien_thi_loai_hinh(exam.exam_type),
                "Ngay_thi": item.exam_date.isoformat(),
                "So_ca": item.session,
                "Ky_hieu_ca": getattr(item, "session_label", ""),
                "So_sinh_vien": exam.size,
                "So_lop": len(exam.section_ids),
                "Danh_sach_lop": ", ".join(exam.section_ids[:5])
                + ("..." if len(exam.section_ids) > 5 else ""),
                "Phong": ", ".join(item.room_ids),
                "Giam_thi": ", ".join(item.invigilator_ids),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(["Ngay_thi", "So_ca", "Ten_mon"])


def to_excel_bytes(
    schedule_df: pd.DataFrame,
    violations_df: pd.DataFrame,
    student_df: pd.DataFrame | None = None,
    kpi_rows: list[tuple[str, object]] | None = None,
) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        schedule_df.to_excel(writer, index=False, sheet_name="Lich_thi")
        violations_df.to_excel(writer, index=False, sheet_name="Vi_pham_ngay_on")
        if student_df is not None and not student_df.empty:
            student_df.to_excel(writer, index=False, sheet_name="Theo_sinh_vien")
        if kpi_rows:
            kpi_df = pd.DataFrame(kpi_rows, columns=["Chi_so", "Gia_tri"])
            kpi_df.to_excel(writer, index=False, sheet_name="KPI")
    return output.getvalue()
