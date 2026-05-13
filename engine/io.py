from __future__ import annotations

import unicodedata
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


def _infer_exam_format_from_name(course_name_normalized: str) -> int:
    """1=tự luận, 2=trắc nghiệm máy, 3=vấn đáp (theo bảng mã hóa nghiệp vụ)."""
    if any(k in course_name_normalized for k in PBL_KEYWORDS):
        return 3
    if any(k in course_name_normalized for k in COMPUTER_TEST_KEYWORDS):
        return 2
    return 1


def _format_to_exam_type(fmt: int) -> str:
    return {1: "theory", 2: "computer", 3: "oral"}.get(int(fmt), "theory")


def _course_prefix_7(section_id: str) -> str:
    s = str(section_id).strip()
    return s[:7] if len(s) >= 7 else s


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

    fmt_col = None
    for candidate in ("MaHinhThuc", "Ma_hinh_thuc", "HinhThucThi", "LoaiHinhThi"):
        if candidate in df.columns:
            fmt_col = candidate
            break

    use_cols = list(required_cols)
    if fmt_col:
        use_cols.append(fmt_col)

    df = df[use_cols].copy()
    df = df.dropna(subset=["MaHS", "MalopHP", "TenLopHP"])
    df["SoTC"] = pd.to_numeric(df["SoTC"], errors="coerce").fillna(2.0)
    df["MaHS"] = df["MaHS"].astype(str).str.strip()
    df["MalopHP"] = df["MalopHP"].astype(str).str.strip()
    df["TenLopHP"] = df["TenLopHP"].astype(str).str.strip()
    df["TenSV"] = df["TenSV"].astype(str).fillna("").str.strip()
    df = df.drop_duplicates(subset=["MaHS", "MalopHP"])

    rows: List[Registration] = []
    for row in df.itertuples(index=False):
        fmt_val = None
        if fmt_col:
            raw = getattr(row, fmt_col, None)
            if raw is not None and str(raw).strip() != "" and str(raw).strip().lower() != "nan":
                try:
                    v = int(float(str(raw).strip()))
                    if v in (1, 2, 3):
                        fmt_val = v
                except (TypeError, ValueError):
                    fmt_val = None
        rows.append(
            Registration(
                student_id=row.MaHS,
                student_name=row.TenSV,
                section_id=row.MalopHP,
                course_name=row.TenLopHP,
                credits=float(row.SoTC),
                exam_format=fmt_val,
            )
        )
    return rows


def build_exams(
    registrations: List[Registration],
    prep_day_per_credit: float = 0.6,
    common_section_threshold: int = 3,
    max_exam_size: int | None = None,
) -> Tuple[List[Exam], Dict[str, Registration]]:
    """Tạo danh sách Exam từ Registration.

    Args:
        common_section_threshold: số lớp học phần tối thiểu để gom thi chung.
            Mặc định 3 nghĩa là **trên 2 lớp** (≥ 3 lớp học phần) mới gom chung.
        max_exam_size: nếu set, là **trần cứng** số SV mỗi ca thi: môn nào có tổng
            SV > ngưỡng sẽ tách thành nhiều ca; sau khi gom theo lớp, từng ca vẫn
            được cắt theo danh sách SV để không ca nào vượt ngưỡng (kể cả một MalopHP quá đông).
            None = không áp trần (giữ nguyên một ca / đề chung theo logic nhóm).
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
                "exam_format": r.exam_format,
                "prefix7": _course_prefix_7(r.section_id),
            }
            for r in registrations
        ]
    )

    section_counts = df.groupby("course_norm")["section_id"].nunique()
    thr = int(common_section_threshold)
    if thr < 2:
        thr = 3
    common_exam_courses = set(section_counts[section_counts >= thr].index.tolist())

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
        fmt_series = exam_df["exam_format"].dropna()
        if not fmt_series.empty:
            try:
                exam_format = int(float(fmt_series.mode().iloc[0]))
                if exam_format not in (1, 2, 3):
                    exam_format = _infer_exam_format_from_name(normalized)
            except (TypeError, ValueError):
                exam_format = _infer_exam_format_from_name(normalized)
        else:
            exam_format = _infer_exam_format_from_name(normalized)
        exam_type = _format_to_exam_type(exam_format)
        prefix7 = str(exam_df["prefix7"].mode().iloc[0]) if len(exam_df) else ""
        if not prefix7 and section_ids:
            prefix7 = _course_prefix_7(section_ids[0])
        priority = 10 if (exam_format == 3 or any(k in normalized for k in PBL_KEYWORDS)) else 0

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
            max_cap = int(max_exam_size)
            for part_idx, sec_list in enumerate(section_groups, start=1):
                if not sec_list:
                    continue
                part_students = sorted(
                    exam_df[exam_df["section_id"].isin(sec_list)]["student_id"].unique().tolist()
                )
                # Cứng: không ca thi nào vượt max_cap SV (kể cả một lớp quá đông).
                chunks: List[List[str]] = []
                for st in range(0, len(part_students), max_cap):
                    ch = part_students[st : st + max_cap]
                    if ch:
                        chunks.append(ch)
                nch = len(chunks)
                for j, chunk in enumerate(chunks, start=1):
                    secs = sorted(
                        exam_df[exam_df["student_id"].isin(chunk)]["section_id"].unique().tolist()
                    )
                    if nch == 1:
                        disp_name = f"{course_name} (đề {part_idx}/{num_parts})"
                    else:
                        disp_name = f"{course_name} (đề {part_idx}/{num_parts} — nhóm {j}/{nch})"
                    exams.append(
                        Exam(
                            exam_id=f"EXAM{next_idx:05d}",
                            course_id=course_id,
                            course_name=disp_name,
                            exam_type=exam_type,
                            section_ids=secs,
                            credits=credits,
                            student_ids=chunk,
                            exam_format=exam_format,
                            course_prefix_7=prefix7,
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
                    exam_format=exam_format,
                    course_prefix_7=prefix7,
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

def _fold_header_label(name: str) -> str:
    """Chuẩn hóa tên cột Excel để khớp alias (bỏ dấu, chỉ giữ chữ số)."""
    s = unicodedata.normalize("NFKD", str(name).strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c.lower() if c.isalnum() else "" for c in s)


# Mỗi cột chuẩn → tập alias đã fold (tiếng Việt / viết tắt thường gặp).
_ROOM_HEADER_ALIASES: Tuple[Tuple[str, frozenset[str]], ...] = (
    (
        "RoomID",
        frozenset(
            {
                "roomid",
                "maphong",
                "maph",
                "sophong",
                "mapt",
                "idphong",
                "tenphong",
                "phong",
                "maphongthi",
                "tenphongthi",
            }
        ),
    ),
    (
        "Location",
        frozenset(
            {
                "location",
                "khu",
                "vitri",
                "diadiem",
                "toanha",
                "daynha",
                "coso",
                "khuvuc",
            }
        ),
    ),
    (
        "Capacity",
        frozenset(
            {
                "capacity",
                "soluong",
                "succhua",
                "sl",
                "siso",
                "chocong",
                "socho",
                "soghe",
                "sisosv",
            }
        ),
    ),
    (
        "RoomType",
        frozenset(
            {
                "roomtype",
                "loaiphong",
                "loaiphongthi",
                "maghephinhthucthi",
                "mahinhthucphong",
                "mahinhthuc",
                "loaihinhthiphong",
            }
        ),
    ),
)


def _normalize_room_type_cell(value: object) -> str:
    """Giá trị ô loại phòng: cùng bộ mã 1/2/3 như hình thức thi (1=tự luận, 2=TN, 3=vấn đáp)
    hoặc chữ theory / computer / any (dùng nội bộ khi phân phòng).
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "any"
    raw = str(value).strip()
    if not raw or raw.lower() in ("nan", "none", "-", "—"):
        return "any"
    s = raw.lower()
    s_fold = _fold_header_label(raw)
    if s in ("1", "1.0") or s_fold in ("tuluuan", "lythuyet", "lth"):
        return "theory"
    if s in ("2", "2.0") or s_fold in ("tracnghiem", "maytinh", "phongmay", "tn"):
        return "computer"
    if s in ("3", "3.0") or s_fold in ("vandap", "pbl", "dodan", "oral"):
        return "any"
    return s


def _standardize_room_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Đổi tên cột về RoomID, Location, Capacity [, RoomType] nếu nhận diện được alias."""
    canon_to_source: Dict[str, str] = {}
    for col in df.columns:
        folded = _fold_header_label(col)
        for canon, aliases in _ROOM_HEADER_ALIASES:
            if folded in aliases and canon not in canon_to_source:
                canon_to_source[canon] = col
                break
    out = pd.DataFrame()
    for canon, _ in _ROOM_HEADER_ALIASES:
        if canon == "RoomType":
            continue
        if canon not in canon_to_source:
            continue
        out[canon] = df[canon_to_source[canon]]
    if "RoomType" in canon_to_source:
        out["RoomType"] = df[canon_to_source["RoomType"]]
    return out


def load_rooms(rooms_path: str | Path | None) -> List[Room]:
    if not rooms_path:
        return []
    raw = pd.read_excel(rooms_path, sheet_name=0)
    df = _standardize_room_columns(raw)
    required_cols = {"RoomID", "Capacity"}
    missing = required_cols.difference(df.columns)
    if missing:
        seen = [str(c) for c in raw.columns]
        raise ValueError(
            "File phòng thi thiếu cột bắt buộc (sau khi nhận diện alias): "
            f"{sorted(missing)}. Cột đang có: {seen}. "
            "Cần có: mã phòng (RoomID / Phòng / MaPhong…), sức chứa (Capacity / So luong…); "
            "khuyến nghị thêm: khu/vị trí (Location / Khu…), loại phòng (RoomType)."
        )
    if "Location" not in df.columns:
        df = df.copy()
        df["Location"] = ""
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
            room_type = _normalize_room_type_cell(getattr(row, "RoomType", None))
        rows.append(
            Room(
                room_id=str(row.RoomID).strip(),
                location=str(row.Location).strip() if row.Location is not None and not pd.isna(row.Location) else "",
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
