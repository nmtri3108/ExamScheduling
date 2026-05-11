from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

from .models import Exam, Invigilator, Registration, Room, ScheduleWindow


PBL_KEYWORDS = (
    "pbl",
    "đồ án",
    "do an",
    "project",
    "thực tập",
    "thuc tap",
    "khóa luận",
    "khoa luan",
    "tiểu luận",
    "tieu luan",
    "thesis",
    "luận văn",
    "luan van",
)

COMPUTER_TEST_KEYWORDS = (
    "trắc nghiệm",
    "trac nghiem",
    "máy tính",
    "may tinh",
    "computer",
    "lab",
    "phòng máy",
    "phong may",
)


def _normalize_text(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def _infer_course_id_from_sections(section_ids: List[str]) -> str:
    """MalopHP encodes course id in the first 12 chars (theo dữ liệu mẫu)."""
    prefixes = [str(s)[:12] for s in section_ids if str(s).strip()]
    if not prefixes:
        return ""
    return pd.Series(prefixes).mode().iloc[0]


def _infer_exam_type(course_name_normalized: str) -> str:
    if any(k in course_name_normalized for k in PBL_KEYWORDS):
        return "oral"
    if any(k in course_name_normalized for k in COMPUTER_TEST_KEYWORDS):
        return "computer"
    return "theory"


# ---------------------------------------------------------------------------
# Plan / window
# ---------------------------------------------------------------------------

def load_schedule_window(plan_path: str | Path) -> ScheduleWindow:
    df = pd.read_excel(plan_path, sheet_name=0)
    needed = {"Ngày BD", "Ngày kết thúc"}
    if not needed.issubset(df.columns):
        raise ValueError(
            f"File kế hoạch thi thiếu cột bắt buộc: {sorted(needed.difference(df.columns))}"
        )

    start_date = pd.to_datetime(df["Ngày BD"], errors="coerce").dropna().min()
    end_date = pd.to_datetime(df["Ngày kết thúc"], errors="coerce").dropna().max()
    if pd.isna(start_date) or pd.isna(end_date):
        raise ValueError("Không đọc được ngày bắt đầu/kết thúc từ file kế hoạch thi.")
    if end_date < start_date:
        raise ValueError("Ngày kết thúc trước ngày bắt đầu trong file kế hoạch.")
    return ScheduleWindow(start_date=start_date.date(), end_date=end_date.date())


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------

def load_registrations(reg_path: str | Path) -> List[Registration]:
    df = pd.read_excel(reg_path, sheet_name=0)
    required_cols = {"MaHS", "TenSV", "MalopHP", "TenLopHP", "SoTC"}
    missing = required_cols.difference(df.columns)
    if missing:
        raise ValueError(f"File đăng ký thiếu cột: {sorted(missing)}")

    df = df[list(required_cols)].copy()
    df = df.dropna(subset=["MaHS", "MalopHP", "TenLopHP"])
    df["SoTC"] = pd.to_numeric(df["SoTC"], errors="coerce").fillna(2.0)
    df["MaHS"] = df["MaHS"].astype(str).str.strip()
    df["MalopHP"] = df["MalopHP"].astype(str).str.strip()
    df["TenLopHP"] = df["TenLopHP"].astype(str).str.strip()
    df["TenSV"] = df["TenSV"].astype(str).fillna("").str.strip()
    df = df.drop_duplicates(subset=["MaHS", "MalopHP"])

    rows: List[Registration] = []
    for row in df.itertuples(index=False):
        rows.append(
            Registration(
                student_id=row.MaHS,
                student_name=row.TenSV,
                section_id=row.MalopHP,
                course_name=row.TenLopHP,
                credits=float(row.SoTC),
            )
        )
    return rows


def build_exams(
    registrations: List[Registration],
    prep_day_per_credit: float = 0.6,
    common_exam_min_sections: int = 2,
    max_exam_size: int | None = None,
) -> Tuple[List[Exam], Dict[str, Registration]]:
    """Tạo danh sách Exam từ Registration.

    Args:
        common_exam_min_sections: số lớp tối thiểu để gom thi chung. Mặc định 2.
        max_exam_size: nếu set, môn nào có > max_exam_size SV sẽ được tự động
            tách thành N ca thi khác nhau (mỗi ca có đề riêng, không chung SV).
            None = không tách (giữ nguyên đề chung lớn).
    """
    if not registrations:
        return [], {}

    df = pd.DataFrame(
        [
            {
                "student_id": r.student_id,
                "student_name": r.student_name,
                "section_id": r.section_id,
                "course_name": r.course_name,
                "course_norm": _normalize_text(r.course_name),
                "credits": r.credits,
            }
            for r in registrations
        ]
    )

    section_counts = df.groupby("course_norm")["section_id"].nunique()
    common_exam_courses = set(
        section_counts[section_counts >= max(2, int(common_exam_min_sections))].index.tolist()
    )

    df["exam_group"] = df.apply(
        lambda x: x["course_norm"] if x["course_norm"] in common_exam_courses else x["section_id"],
        axis=1,
    )

    exams: List[Exam] = []
    next_idx = 1
    grouped = df.groupby("exam_group", sort=True)
    for exam_group, exam_df in grouped:
        course_name = exam_df["course_name"].iloc[0]
        credits = float(exam_df["credits"].mode().iloc[0])
        section_ids = sorted(exam_df["section_id"].unique().tolist())
        student_ids = sorted(exam_df["student_id"].unique().tolist())
        course_id = _infer_course_id_from_sections(section_ids)
        normalized = _normalize_text(course_name)
        exam_type = _infer_exam_type(normalized)
        priority = 10 if any(k in normalized for k in PBL_KEYWORDS) else 0

        # Auto-split nếu vượt ngưỡng — chia theo section_ids để mỗi part có SV ~ bằng nhau.
        if max_exam_size and len(student_ids) > max_exam_size:
            num_parts = (len(student_ids) + max_exam_size - 1) // max_exam_size
            # Chia section_ids thành num_parts nhóm liên tiếp (giữ SV cùng lớp gần nhau)
            section_groups: List[List[str]] = [[] for _ in range(num_parts)]
            # Tính số SV mỗi section
            section_size = exam_df.groupby("section_id")["student_id"].nunique().to_dict()
            sorted_sections = sorted(section_ids, key=lambda s: -section_size.get(s, 0))
            # Phân bổ greedy theo bin-packing FFD
            group_loads = [0] * num_parts
            for sec in sorted_sections:
                # đặt vào group có load nhỏ nhất
                target = min(range(num_parts), key=lambda i: group_loads[i])
                section_groups[target].append(sec)
                group_loads[target] += section_size.get(sec, 0)
            for part_idx, sec_list in enumerate(section_groups, start=1):
                if not sec_list:
                    continue
                part_students = sorted(
                    exam_df[exam_df["section_id"].isin(sec_list)]["student_id"].unique().tolist()
                )
                exams.append(
                    Exam(
                        exam_id=f"EXAM{next_idx:05d}",
                        course_id=course_id,
                        course_name=f"{course_name} (đề {part_idx}/{num_parts})",
                        exam_type=exam_type,
                        section_ids=sorted(sec_list),
                        credits=credits,
                        student_ids=part_students,
                        priority=priority,
                        prep_days=round(credits * prep_day_per_credit, 2),
                    )
                )
                next_idx += 1
        else:
            exams.append(
                Exam(
                    exam_id=f"EXAM{next_idx:05d}",
                    course_id=course_id,
                    course_name=course_name,
                    exam_type=exam_type,
                    section_ids=section_ids,
                    credits=credits,
                    student_ids=student_ids,
                    priority=priority,
                    prep_days=round(credits * prep_day_per_credit, 2),
                )
            )
            next_idx += 1

    student_ref: Dict[str, Registration] = {}
    for r in registrations:
        student_ref[r.student_id] = r
    return exams, student_ref


# ---------------------------------------------------------------------------
# Rooms / Invigilators
# ---------------------------------------------------------------------------

def load_rooms(rooms_path: str | Path | None) -> List[Room]:
    if not rooms_path:
        return []
    df = pd.read_excel(rooms_path, sheet_name=0)
    required_cols = {"RoomID", "Location", "Capacity"}
    missing = required_cols.difference(df.columns)
    if missing:
        raise ValueError(f"File phòng thi thiếu cột: {sorted(missing)}")
    rows: List[Room] = []
    for row in df.itertuples(index=False):
        try:
            capacity = int(row.Capacity)
        except (TypeError, ValueError):
            continue
        if capacity <= 0:
            continue
        room_type = "any"
        if hasattr(row, "RoomType"):
            value = str(getattr(row, "RoomType", "") or "").strip().lower()
            if value:
                room_type = value
        rows.append(
            Room(
                room_id=str(row.RoomID),
                location=str(row.Location),
                capacity=capacity,
                room_type=room_type,
            )
        )
    return rows


def load_invigilators(invigilators_path: str | Path | None) -> List[Invigilator]:
    if not invigilators_path:
        return []
    df = pd.read_excel(invigilators_path, sheet_name=0)
    required_cols = {"InvigilatorID", "FullName"}
    missing = required_cols.difference(df.columns)
    if missing:
        raise ValueError(f"File giám thị thiếu cột: {sorted(missing)}")
    rows: List[Invigilator] = []
    for row in df.itertuples(index=False):
        limit_per_day = getattr(row, "MaxSessionsPerDay", 2) or 2
        limit_total = getattr(row, "MaxSessionsTotal", 9999) or 9999
        rows.append(
            Invigilator(
                invigilator_id=str(row.InvigilatorID),
                full_name=str(row.FullName),
                max_sessions_per_day=int(limit_per_day),
                max_sessions_total=int(limit_total),
            )
        )
    return rows
