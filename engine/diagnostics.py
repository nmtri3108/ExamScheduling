"""Chẩn đoán tiền-giải & KPI hậu-giải.

Mục tiêu:
- Trước khi gọi solver: cho người dùng thấy bài toán có khả thi không, ở đâu chật.
- Sau khi solve: cung cấp KPI để đánh giá chất lượng lịch.
"""
from __future__ import annotations

from datetime import date, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from math import ceil
from typing import Dict, List, Tuple

from .models import Exam, PrepViolation, Room, ScheduledExam, ScheduleWindow


# ---------------------------------------------------------------------------
# Pre-flight diagnostics
# ---------------------------------------------------------------------------

@dataclass
class FeasibilityReport:
    num_exams: int = 0
    num_students: int = 0
    num_sections: int = 0
    num_slots: int = 0
    num_days: int = 0
    max_exams_per_student: int = 0
    avg_exams_per_student: float = 0.0
    num_conflicts: int = 0           # cặp môn có chung SV
    conflict_density: float = 0.0    # 0..1
    by_type_count: Dict[str, int] = field(default_factory=dict)
    by_type_slot_capacity: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)

    @property
    def is_feasible_in_principle(self) -> bool:
        return len(self.errors) == 0


def build_conflict_index(exams: List[Exam]) -> Dict[Tuple[int, int], int]:
    """Trả về dict {(i, j): số SV trùng}. Dùng set-intersection theo SV để O(N·SV)."""
    student_to_exams: Dict[str, List[int]] = defaultdict(list)
    for i, exam in enumerate(exams):
        for sid in exam.student_ids:
            student_to_exams[sid].append(i)
    overlap: Dict[Tuple[int, int], int] = defaultdict(int)
    for indices in student_to_exams.values():
        if len(indices) < 2:
            continue
        unique = sorted(set(indices))
        for a in range(len(unique)):
            for b in range(a + 1, len(unique)):
                overlap[(unique[a], unique[b])] += 1
    return dict(overlap)


def diagnose(
    exams: List[Exam],
    window: ScheduleWindow,
    rooms: List[Room],
    allowed_sessions_by_type: Dict[str, List[int]] | None = None,
    min_prep_days: float = 0.0,
    max_exams_per_day: int = 2,
    weekend_large_course_min_students: int = 0,
) -> FeasibilityReport:
    report = FeasibilityReport()
    if not exams:
        report.errors.append("Chưa có môn thi nào (file đăng ký rỗng).")
        return report
    report.num_exams = len(exams)
    report.num_sections = sum(len(e.section_ids) for e in exams)
    all_students = {sid for e in exams for sid in e.student_ids}
    report.num_students = len(all_students)
    report.num_slots = window.total_slots
    report.num_days = window.total_days

    # Số môn / SV
    exams_per_student: Dict[str, int] = defaultdict(int)
    for e in exams:
        for sid in e.student_ids:
            exams_per_student[sid] += 1
    if exams_per_student:
        report.max_exams_per_student = max(exams_per_student.values())
        report.avg_exams_per_student = sum(exams_per_student.values()) / len(exams_per_student)

    # Xung đột
    conflicts = build_conflict_index(exams)
    report.num_conflicts = len(conflicts)
    pair_total = report.num_exams * (report.num_exams - 1) / 2 or 1
    report.conflict_density = report.num_conflicts / pair_total

    # Tổng quan theo loại
    allowed_sessions_by_type = allowed_sessions_by_type or {}
    type_count: Dict[str, int] = defaultdict(int)
    for e in exams:
        type_count[e.exam_type] += 1
    report.by_type_count = dict(type_count)

    for etype, count in type_count.items():
        allowed = allowed_sessions_by_type.get(etype, list(range(window.sessions_per_day)))
        slot_capacity = report.num_days * max(1, len(allowed))
        report.by_type_slot_capacity[etype] = slot_capacity
        if count > slot_capacity:
            # CHỈ là warning vì nhiều môn cùng loại có thể trùng slot (không xung đột SV).
            report.warnings.append(
                f"Loại '{etype}' có {count} môn vs {slot_capacity} ca khả dụng "
                f"({len(allowed)} ca/ngày × {report.num_days} ngày). Vẫn có thể xếp được "
                f"do nhiều môn cùng loại có thể trùng ca nếu không chung sinh viên."
            )

    # Kiểm tra max_exams_per_day vs số ngày khả dụng
    if max_exams_per_day > 0 and report.max_exams_per_student:
        days_needed = ceil(report.max_exams_per_student / max_exams_per_day)
        if days_needed > report.num_days:
            report.errors.append(
                f"Có SV phải thi {report.max_exams_per_student} môn nhưng giới hạn "
                f"{max_exams_per_day} môn/ngày trên {report.num_days} ngày không đủ "
                f"(cần ≥ {days_needed} ngày)."
            )

    # Cảnh báo prep
    if min_prep_days > 0 and report.max_exams_per_student:
        span = (report.max_exams_per_student - 1) * min_prep_days
        if span > report.num_days - 1:
            report.warnings.append(
                f"min_prep_days={min_prep_days} có thể không đủ chỗ: SV nặng nhất cần "
                f"~{span:.1f} ngày khoảng cách trong khi đợt chỉ {report.num_days} ngày."
            )

    if window.khoa_lop_windows:
        no_plan_days = 0
        multi_window = 0
        for e in exams:
            if not exam_allowed_day_indices(e, window):
                no_plan_days += 1
            keys = exam_khoa_nhom_keys(e)
            if len(keys) > 1:
                wins = {
                    window.khoa_lop_windows[k]
                    for k in keys
                    if k in window.khoa_lop_windows
                }
                if len(wins) > 1:
                    multi_window += 1
        if no_plan_days:
            report.errors.append(
                f"{no_plan_days} ca thi không có ngày hợp lệ trong Ke_hoach_thi "
                "(lớp khác đợt bị gom chung hoặc thiếu mã Khoa_lop*)."
            )
        elif multi_window:
            report.warnings.append(
                f"{multi_window} ca vẫn gom nhiều đợt ngày khác nhau — kiểm tra tách môn / đăng ký."
            )
        else:
            report.info.append(
                f"Mỗi ca thi nằm trong một đợt Khoa_lop* "
                f"({len(window.khoa_lop_windows)} mã trong kế hoạch)."
            )

    # Sức chứa phòng + môn quá lớn
    largest = max((e.size for e in exams), default=0)
    large_exams = sorted([e for e in exams if e.size > 1000], key=lambda e: -e.size)
    if rooms:
        total_capacity = sum(r.capacity for r in rooms)
        if largest > total_capacity:
            report.errors.append(
                f"Môn lớn nhất có {largest} SV vượt tổng sức chứa phòng ({total_capacity})."
            )
        report.info.append(
            f"Tổng sức chứa {total_capacity} chỗ / ca; môn lớn nhất {largest} SV."
        )
        if largest > total_capacity * 0.7:
            report.warnings.append(
                f"Môn lớn nhất ({largest} SV) chiếm {largest/total_capacity:.0%} tổng sức chứa "
                f"→ cùng ca KHÔNG còn chỗ cho môn khác. Cân nhắc bật 'Tách môn lớn'."
            )
    if large_exams:
        names = ", ".join(f"{e.course_name} ({e.size})" for e in large_exams[:5])
        suffix = f" và {len(large_exams)-5} môn khác" if len(large_exams) > 5 else ""
        report.warnings.append(
            f"Có {len(large_exams)} môn > 1000 SV (lớn nhất: {largest}). "
            f"Nếu không đủ phòng cùng lúc, bật 'Tách môn lớn' để chia thành nhiều ca. "
            f"Ví dụ: {names}{suffix}."
        )

    # Cảnh báo quy mô
    if report.num_exams > 600 and report.num_conflicts > 100_000:
        report.info.append(
            "Bài toán quy mô lớn: hệ thống sẽ dùng thuật toán tham lam (DSATUR) trước, "
            "sau đó mới tối ưu ràng buộc (SAT) nếu kích thước cho phép."
        )

    # Ngày trong tuần: môn đông → chỉ T7/CN; môn thường → T2–T7 (không CN)
    if weekend_large_course_min_students > 0 and report.num_exams:
        totals = build_prefix_student_totals(exams)
        sat_sun_days = 0
        mon_sat_days = 0
        for i in range(window.total_days):
            wd = (window.start_date + timedelta(days=i)).weekday()
            if wd in (5, 6):
                sat_sun_days += 1
            if wd != 6:
                mon_sat_days += 1
        if sat_sun_days == 0:
            report.errors.append(
                "Đã bật ép môn đông thi cuối tuần (T7–CN) nhưng khung thi không có ngày thứ Bảy hoặc Chủ nhật."
            )
        if mon_sat_days == 0:
            report.errors.append(
                "Khung thi chỉ toàn Chủ nhật: không thể áp dụng quy tắc «môn thường thi thứ Hai–thứ Bảy»."
            )
        large_prefixes = [pfx for pfx, n in totals.items() if n >= weekend_large_course_min_students]
        if large_prefixes and sat_sun_days > 0:
            report.info.append(
                f"Quy tắc ngày trong tuần: môn đông (≥{weekend_large_course_min_students} SV theo 7 ký tự MalopHP) "
                f"chỉ xếp thứ Bảy/Chủ nhật; môn khác chỉ thứ Hai–thứ Bảy. "
                f"Có {len(large_prefixes)} nhóm học phần đạt ngưỡng."
            )

    # Khoa_nhom (4 ký tự cuối MalopHP): mỗi ngày tối đa một ca thi cho mỗi hậu tố.
    worst_k, worst_n = max_khoa_nhom_distinct_course_groups(exams)
    if worst_n > report.num_days:
        report.errors.append(
            f"Khoa_nhom «{worst_k}» có {worst_n} môn (nhóm học phần) khác nhau cần xếp khác ngày, "
            f"trong khi đợt chỉ có {report.num_days} ngày."
        )
    if worst_n > 0 or any(exam_khoa_nhom_keys(e) for e in exams):
        report.info.append(
            "Ràng buộc Khoa_nhom: 4 ký tự cuối MalopHP; hai môn khác nhau cùng hậu tố không cùng ngày "
            "(các ca tách cùng học phần được miễn)."
        )
    return report


# ---------------------------------------------------------------------------
# Post-run KPI
# ---------------------------------------------------------------------------

@dataclass
class SchedulingKPI:
    days_used: int = 0
    slots_used: int = 0
    slot_utilization: float = 0.0     # % slot có dùng / tổng slot khả dụng
    avg_students_per_slot: float = 0.0
    max_students_per_slot: int = 0
    prep_violation_count: int = 0
    prep_violation_students: int = 0
    prep_violation_count_year1: int = 0
    prep_violation_students_year1: int = 0
    newest_cohort_code: int = 0  # niên khóa SV năm 1 (setting hoặc auto)
    # Vi phạm có actual_days ≤ 0 (cùng ngày / "0 ngày ôn") — chỉ số đau nhất cho SV.
    same_day_violation_count: int = 0
    same_day_violation_students: int = 0
    avg_prep_gap: float = 0.0          # ngày ôn trung bình giữa các cặp môn liên tiếp của SV
    min_prep_gap: float = 0.0          # ngày ôn nhỏ nhất ghi nhận
    pbl_position_score: float = 1.0   # 1.0 = tất cả PBL ở cuối đợt
    by_day_load: List[Tuple[str, int]] = field(default_factory=list)
    # Điều kiện cứng: >10% SV / học phần (7 ký tự MalopHP) không có ngày ôn (thực tế ≤0)
    prep_prefix_hard_errors: List[str] = field(default_factory=list)


def _malop_prefix_7_for_exam(exam: Exam) -> str:
    """Khóa nhóm học phần: 7 ký tự đầu MalopHP (hoặc từ section/course_id)."""
    p = (exam.course_prefix_7 or "").strip()
    if p:
        return p[:7]
    for sec in sorted(exam.section_ids):
        s = str(sec).strip()
        if len(s) >= 7:
            return s[:7]
    cid = (exam.course_id or "").strip()
    return cid[:7] if len(cid) >= 7 else cid


def cohort_code_from_malop(section_id: str) -> str:
    """Niên khóa (2 ký tự) = hai ký tự đầu của 4 ký tự cuối MalopHP."""
    kn = khoa_nhom_from_malop(section_id)
    return kn[:2] if len(kn) >= 2 else ""


def cohort_index_from_malop(section_id: str) -> int:
    """Mã khóa dạng số (0 nếu không phải 2 chữ số). Dùng hiển thị / KPI."""
    code = cohort_code_from_malop(section_id)
    return int(code) if len(code) == 2 and code.isdigit() else 0


def cohort_numeric_value(code: str) -> int | None:
    """Giá trị số của mã khóa 2 chữ số; ``None`` nếu không phải số (yy, zz, …)."""
    c = str(code or "").strip()
    if len(c) == 2 and c.isdigit():
        return int(c)
    return None


# Sóng xếp / ôn: mã không thuộc chuỗi học vụ ≤ anchor (vd 48, yy) → ưu tiên cuối.
_COHORT_LOW_PRIORITY_WAVE = 1_000_000


def cohort_wave_index(code: str, year1_anchor: int) -> int:
    """Sóng ưu tiên: 0 = khóa anchor (năm 1), 1 = anchor−1, …; mã lạ / > anchor → cuối."""
    if year1_anchor <= 0:
        n = cohort_numeric_value(code)
        if n is None:
            return _COHORT_LOW_PRIORITY_WAVE
        return max(0, 99 - n)
    n = cohort_numeric_value(code)
    if n is not None and 0 <= n <= year1_anchor:
        return year1_anchor - n
    return _COHORT_LOW_PRIORITY_WAVE


def resolve_year1_cohort_anchor(
    year1_anchor_setting: int,
    exams: List[Exam] | None = None,
    student_cohort_codes: Dict[str, str] | None = None,
) -> int:
    """Khóa SV năm 1: từ setting sidebar; 0 = tự lấy max mã số hợp lệ trong data."""
    if int(year1_anchor_setting or 0) > 0:
        return int(year1_anchor_setting)
    mx = 0
    if student_cohort_codes:
        for code in student_cohort_codes.values():
            n = cohort_numeric_value(code)
            if n is not None:
                mx = max(mx, n)
    if exams:
        for ex in exams:
            for sec in ex.section_ids:
                n = cohort_numeric_value(cohort_code_from_malop(sec))
                if n is not None:
                    mx = max(mx, n)
    return mx


def khoa_nhom_from_malop(section_id: str) -> str:
    """Khoa_nhom = 4 ký tự cuối MalopHP (vd ``104181025102122`` → ``2122``).

    Hai môn khác học phần cùng hậu tố này không thi cùng một ngày.
    """
    s = str(section_id or "").strip()
    if not s:
        return ""
    return s[-4:] if len(s) >= 4 else s


def normalize_khoa_lop_key(value: str) -> str:
    s = str(value or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if s.isdigit():
        return s.zfill(4)
    return s


def exam_khoa_nhom_keys(exam: Exam) -> frozenset[str]:
    """Tập Khoa_lớp cho ca thi: ưu tiên cột explicit, fallback về 4 ký tự cuối MalopHP."""
    keys: set[str] = {
        normalize_khoa_lop_key(k) for k in getattr(exam, "khoa_lop_keys", []) if str(k).strip()
    }
    keys.discard("")
    if keys:
        return frozenset(keys)
    for sec in exam.section_ids:
        k = khoa_nhom_from_malop(sec)
        if k:
            keys.add(k)
    return frozenset(keys)


def exam_max_cohort_index(exam: Exam) -> int:
    """Khóa số lớn nhất trên ca (ưu tiên cột khóa explicit)."""
    from_codes = []
    for code in getattr(exam, "cohort_codes", []):
        n = cohort_numeric_value(str(code))
        if n is not None:
            from_codes.append(n)
    if from_codes:
        return max(from_codes)
    mx = 0
    for sec in exam.section_ids:
        mx = max(mx, cohort_index_from_malop(sec))
    return mx


def exam_min_cohort_wave(exam: Exam, year1_anchor: int) -> int:
    """Sóng ưu tiên của ca = sóng tốt nhất (nhỏ nhất) trong các mã khóa của ca."""
    codes = [str(c) for c in getattr(exam, "cohort_codes", []) if str(c).strip()]
    if not codes:
        codes = [cohort_code_from_malop(sec) for sec in exam.section_ids]
    waves = [cohort_wave_index(code, year1_anchor) for code in codes]
    if not waves:
        return _COHORT_LOW_PRIORITY_WAVE
    return min(waves)


def prep_days_required_for_exam(
    exam: Exam,
    prep_day_per_credit: float,
    min_prep_days: float = 0.0,
) -> float:
    """Số ngày ôn mong muốn cho một môn (khớp báo cáo vi phạm — môn «sau»)."""
    return max(float(min_prep_days), float(exam.credits) * float(prep_day_per_credit))


def prep_days_required_for_pair(
    exam_a: Exam,
    exam_b: Exam,
    prep_day_per_credit: float,
    min_prep_days: float = 0.0,
) -> float:
    """Khoảng ôn tối thiểu giữa hai môn cùng SV — dùng max tín chỉ để không «thiệt» môn 4+ TC."""
    cred = max(float(exam_a.credits), float(exam_b.credits))
    return max(float(min_prep_days), cred * float(prep_day_per_credit))


def prep_hard_gap_days_for_pair(
    exam_a: Exam,
    exam_b: Exam,
    prep_day_per_credit: float,
    min_prep_days: float = 0.0,
    *,
    year1_allow_same_day: bool = True,
    for_year1_student: bool = False,
    same_calendar_day: bool = False,
) -> float:
    """Khoảng lịch tối thiểu (số ngày) giữa hai **ngày thi khác nhau** — dùng ``ceil`` để khớp lịch.

    SV khóa năm 1 + ``year1_allow_same_day``: được thi **cùng ngày** (khoảng cách 0).
    """
    if year1_allow_same_day and for_year1_student and same_calendar_day:
        return 0.0
    return float(
        min_prep_index_gap_between(
            exam_a,
            exam_b,
            prep_day_per_credit,
            min_prep_days,
            year1_allow_same_day=year1_allow_same_day,
            for_year1_student=for_year1_student,
            same_calendar_day=same_calendar_day,
        )
    )


def min_prep_index_gap_between(
    exam_a: Exam,
    exam_b: Exam,
    prep_day_per_credit: float,
    min_prep_days: float = 0.0,
    *,
    year1_allow_same_day: bool = True,
    for_year1_student: bool = False,
    same_calendar_day: bool = False,
) -> int:
    """Số ngày lịch tối thiểu giữa hai ngày (chỉ số ngày hoặc chênh lệch date)."""
    if year1_allow_same_day and for_year1_student and same_calendar_day:
        return 0
    req = prep_days_required_for_pair(
        exam_a, exam_b, prep_day_per_credit, min_prep_days
    )
    if req <= 0:
        return 0
    return int(ceil(req))


def min_calendar_gap_days_between_exams(
    exam_a: Exam,
    exam_b: Exam,
    prep_day_per_credit: float,
    min_prep_days: float = 0.0,
) -> int:
    """Số ngày lịch tối thiểu giữa hai ngày thi (|day1-day2| phải ≥ giá trị này)."""
    return min_prep_index_gap_between(
        exam_a, exam_b, prep_day_per_credit, min_prep_days
    )


def prep_gap_violated(
    day_gap: int,
    exam_a: Exam,
    exam_b: Exam,
    prep_day_per_credit: float,
    min_prep_days: float = 0.0,
    *,
    year1_allow_same_day: bool = True,
    for_year1_student: bool = False,
    same_calendar_day: bool = False,
) -> bool:
    """``day_gap`` = |day_index_a - day_index_b| hoặc (date_b - date_a).days."""
    need = min_prep_index_gap_between(
        exam_a,
        exam_b,
        prep_day_per_credit,
        min_prep_days,
        year1_allow_same_day=year1_allow_same_day,
        for_year1_student=for_year1_student,
        same_calendar_day=same_calendar_day,
    )
    if need <= 0:
        return False
    return day_gap < need


def resolve_global_max_cohort(
    exams: List[Exam],
    student_cohort: Dict[str, int] | None = None,
) -> int:
    """Mã khóa lớn nhất trong đợt (= ≈ năm 1) — luôn suy từ data, không hardcode 25/26/27.

    Lấy max trên mọi MalopHP (ca thi) và mọi đăng ký SV để đợt sau tự đổi (26, 27, …).
    """
    mx_exam = max((exam_max_cohort_index(e) for e in exams), default=0)
    mx_stu = max(student_cohort.values(), default=0) if student_cohort else 0
    return int(max(mx_exam, mx_stu))


def build_student_cohort_code_map(
    exams: List[Exam],
    registrations: List | None = None,
    year1_anchor: int = 0,
) -> Dict[str, str]:
    """Mã khóa 2 ký tự / SV = mã có sóng ưu tiên cao nhất (nhỏ nhất) trên mọi đăng ký."""
    anchor = resolve_year1_cohort_anchor(year1_anchor, exams=exams)
    codes_by_sid: Dict[str, List[str]] = defaultdict(list)
    if registrations:
        for r in registrations:
            raw_khoa = str(getattr(r, "khoa", "") or "").strip()
            if raw_khoa.endswith(".0") and raw_khoa[:-2].isdigit():
                raw_khoa = raw_khoa[:-2]
            code = raw_khoa[:2] if raw_khoa else ""
            if not code:
                code = cohort_code_from_malop(getattr(r, "section_id", ""))
            if code:
                codes_by_sid[str(r.student_id)].append(code)
    for ex in exams:
        for sid in ex.student_ids:
            for sec in ex.section_ids:
                code = cohort_code_from_malop(sec)
                if code:
                    codes_by_sid[str(sid)].append(code)
    out: Dict[str, str] = {}
    for sid, codes in codes_by_sid.items():
        out[sid] = min(codes, key=lambda c: cohort_wave_index(c, anchor))
    return out


def build_student_cohort_map(
    exams: List[Exam],
    registrations: List | None = None,
    year1_anchor: int = 0,
    student_cohort_codes: Dict[str, str] | None = None,
) -> Dict[str, int]:
    """Mã khóa số / SV (0 nếu yy/zz). Ưu tiên map mã 2 ký tự nếu đã có."""
    if student_cohort_codes is None:
        student_cohort_codes = build_student_cohort_code_map(
            exams, registrations=registrations, year1_anchor=year1_anchor
        )
    return {
        sid: int(cohort_numeric_value(code) or 0)
        for sid, code in student_cohort_codes.items()
        if code
    }


def is_year1_anchor_student(
    student_id: str,
    student_cohort_codes: Dict[str, str],
    year1_anchor: int,
) -> bool:
    """SV thuộc niên khóa năm 1 (theo setting) — bắt buộc đủ ngày ôn khi nới lịch."""
    if year1_anchor <= 0:
        return False
    code = student_cohort_codes.get(str(student_id), "")
    return cohort_numeric_value(code) == year1_anchor


def is_newest_cohort_student(
    student_id: str,
    student_cohort: Dict[str, int],
    global_max_cohort: int | None = None,
    student_cohort_codes: Dict[str, str] | None = None,
    year1_anchor: int | None = None,
) -> bool:
    """Alias: SV khóa anchor (năm 1)."""
    anchor = int(year1_anchor or global_max_cohort or 0)
    if student_cohort_codes is not None and anchor > 0:
        return is_year1_anchor_student(student_id, student_cohort_codes, anchor)
    gmax = (
        int(global_max_cohort)
        if global_max_cohort is not None
        else max(student_cohort.values(), default=0)
    )
    if gmax <= 0:
        return False
    return student_cohort.get(str(student_id), 0) == gmax


def exam_cohort_wave_index(exam: Exam, year1_anchor: int) -> int:
    """Sóng xếp lịch theo setting niên khóa năm 1 (0 = khóa anchor, …)."""
    return exam_min_cohort_wave(exam, year1_anchor)


def same_course_khoa_nhom_waiver(e1: Exam, e2: Exam) -> bool:
    """Cùng học phần (ca tách đề / cùng mã) — không áp lệnh «khác ngày theo Khoa_nhom» giữa các ca."""
    a = (e1.course_prefix_7 or "").strip()
    b = (e2.course_prefix_7 or "").strip()
    if a and b and a == b:
        return True
    c1 = (e1.course_id or "").strip()
    c2 = (e2.course_id or "").strip()
    return bool(c1 and c2 and c1 == c2)


def max_khoa_nhom_distinct_course_groups(exams: List[Exam]) -> Tuple[str, int]:
    """Với mỗi hậu tố Khoa_nhom, đếm số nhóm môn (các ca cùng học phần gộp 1 nhóm). Trả (hậu tố, max nhóm)."""
    key_to_indices: Dict[str, List[int]] = defaultdict(list)
    for ei, e in enumerate(exams):
        for k in exam_khoa_nhom_keys(e):
            key_to_indices[k].append(ei)
    worst_k, worst_n = "", 0
    for k, idxs in key_to_indices.items():
        uniq = sorted(set(idxs))
        parent = {i: i for i in uniq}

        def find(x: int) -> int:
            p = x
            while parent[p] != p:
                p = parent[p]
            return p

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for ia in range(len(uniq)):
            for ib in range(ia + 1, len(uniq)):
                a, b = uniq[ia], uniq[ib]
                if same_course_khoa_nhom_waiver(exams[a], exams[b]):
                    union(a, b)
        n_groups = len({find(i) for i in uniq})
        if n_groups > worst_n:
            worst_n, worst_k = n_groups, k
    return worst_k, worst_n


def build_prefix_student_totals(exams: List[Exam]) -> Dict[str, int]:
    """Số sinh viên duy nhất theo nhóm 7 ký tự đầu MalopHP (gộp mọi ca thi cùng học phần)."""
    by_pfx: Dict[str, set[str]] = defaultdict(set)
    for e in exams:
        pfx = _malop_prefix_7_for_exam(e)
        if not pfx:
            continue
        by_pfx[pfx].update(e.student_ids)
    return {k: len(v) for k, v in by_pfx.items()}


def plan_bucket_for_section(
    section_id: str,
    khoa_lop_windows: Dict[str, Tuple[date, date]],
) -> Tuple[str, ...]:
    """Khóa gom ca thi chung: cùng đợt ngày trong Ke_hoach_thi (hoặc từng mã ngoài kế hoạch)."""
    kl = khoa_nhom_from_malop(section_id)
    bounds = khoa_lop_windows.get(kl)
    if bounds:
        return ("plan", bounds[0].isoformat(), bounds[1].isoformat())
    return ("noplan", kl)


def exam_allowed_day_indices(exam: Exam, window: ScheduleWindow) -> frozenset[int]:
    """Ngày được phép xếp ca thi theo giao Khoa_lop* trong kế hoạch (4 ký tự cuối MalopHP).

    Nhiều lớp trong một ca: lấy **giao** các khoảng ngày. Mã lớp không có trong kế hoạch:
    cho phép toàn khung min–max (đợt gộp).
    """
    if not window.khoa_lop_windows:
        return frozenset(range(window.total_days))
    keys = exam_khoa_nhom_keys(exam)
    if not keys:
        return frozenset(range(window.total_days))
    start_dates: List[date] = []
    end_dates: List[date] = []
    for k in keys:
        bounds = window.khoa_lop_windows.get(k)
        if bounds is None:
            return frozenset(range(window.total_days))
        start_dates.append(bounds[0])
        end_dates.append(bounds[1])
    start = max(start_dates)
    end = min(end_dates)
    if start > end:
        return frozenset()
    d0 = (start - window.start_date).days
    d1 = (end - window.start_date).days
    return frozenset(d for d in range(window.total_days) if d0 <= d <= d1)


def exam_plan_date_bounds(exam: Exam, window: ScheduleWindow) -> Tuple[date, date] | None:
    """Khoảng ngày kế hoạch (giao) cho ca thi; ``None`` nếu không giao được."""
    days = exam_allowed_day_indices(exam, window)
    if not days:
        return None
    d0, d1 = min(days), max(days)
    return (
        window.start_date + timedelta(days=d0),
        window.start_date + timedelta(days=d1),
    )


def weekday_at_day_index(window: ScheduleWindow, day_idx: int) -> int:
    """Thứ trong tuần của ô ngày `day_idx` (0=Thứ Hai … 6=Chủ nhật)."""
    return (window.start_date + timedelta(days=int(day_idx))).weekday()


def _weekday_at_day_index(window: ScheduleWindow, day_idx: int) -> int:
    return weekday_at_day_index(window, day_idx)


def day_allowed_for_exam_weekday_rule(
    exam: Exam,
    day_idx: int,
    window: ScheduleWindow,
    weekend_large_min_students: int,
    prefix_totals: Dict[str, int],
) -> bool:
    """Môn đông (≥ ngưỡng SV theo prefix) chỉ T7/CN; môn khác: T2–T7 (không chủ nhật)."""
    if weekend_large_min_students <= 0:
        return True
    pfx = _malop_prefix_7_for_exam(exam)
    n = prefix_totals.get(pfx, 0)
    wd = weekday_at_day_index(window, day_idx)
    if n >= weekend_large_min_students:
        return wd in (5, 6)
    return wd != 6


def enumerate_feasible_slots_for_exam(
    exam: Exam,
    window: ScheduleWindow,
    allowed_sessions_by_exam_id: Dict[str, List[int]],
    fixed_slots: Dict[str, int] | None = None,
    *,
    weekend_large_min_students: int = 0,
    prefix_totals: Dict[str, int] | None = None,
    relax_weekday_rule: bool = False,
) -> List[int]:
    """Danh sách chỉ số ô thời gian (0..total_slots-1) khả dĩ cho một môn."""
    fixed_slots = dict(fixed_slots or {})
    eid = exam.exam_id
    if eid in fixed_slots:
        return [int(fixed_slots[eid])]
    sessions = allowed_sessions_by_exam_id.get(eid, list(range(window.sessions_per_day)))
    sessions = sorted({int(s) for s in sessions if 0 <= int(s) < window.sessions_per_day})
    if not sessions:
        return []
    totals = prefix_totals or {}
    pfx = _malop_prefix_7_for_exam(exam)
    is_weekend_large = False
    if weekend_large_min_students > 0:
        n = totals.get(pfx, exam.size if pfx else exam.size)
        is_weekend_large = int(n) >= int(weekend_large_min_students) or int(exam.size) >= int(
            weekend_large_min_students
        )
    allowed_days = exam_allowed_day_indices(exam, window)
    slots: List[int] = []
    for d in range(window.total_days):
        if d not in allowed_days:
            continue
        # Môn rất đông (>= ngưỡng) luôn giữ quy tắc cuối tuần, kể cả khi relax_weekday_rule=True.
        apply_weekday_rule = weekend_large_min_students > 0 and (
            not relax_weekday_rule or is_weekend_large
        )
        if apply_weekday_rule:
            if not day_allowed_for_exam_weekday_rule(
                exam, d, window, weekend_large_min_students, totals
            ):
                continue
        for s in sessions:
            slots.append(d * window.sessions_per_day + s)
    return slots


_BLOCKER_VI = {
    "NO_SLOT": "Không có ô thời gian / ca thi trong cấu hình",
    "KE_HOACH": "Ngày thi ngoài khoảng Khoa_lop* trong Ke_hoach_thi (hoặc giao đợt rỗng)",
    "WEEKDAY": "Quy tắc thứ (môn đông chỉ T7–CN; môn khác không Chủ nhật)",
    "CONFLICT": "Trùng sinh viên với ca đã xếp (cùng ô thời gian)",
    "YEAR1_PREP": "SV khóa năm 1: giữa hai ngày thi khác nhau chưa đủ ngày ôn theo tín chỉ",
    "PREP_GAP": "Chưa đủ ngày ôn theo tín chỉ giữa hai môn",
    "KHOA_NHOM": "Trùng Khoa_nhom (4 ký tự cuối MalopHP) cùng ngày — môn HP khác",
    "PREFIX_HALF": "Khác buổi sáng/chiều với ca khác cùng 7 ký tự học phần",
    "SUNDAY_SPREAD": "Quá gần môn đã xếp vào Chủ nhật (giãn tối thiểu 3 ngày)",
    "MAX_PER_DAY": "Sinh viên đã đủ số môn thi tối đa trong ngày đó",
    "CAPACITY": "Vượt sức chứa tổng ca (ước lượng greedy)",
}

_BLOCKER_PRIORITY = (
    "NO_SLOT",
    "KE_HOACH",
    "WEEKDAY",
    "YEAR1_PREP",
    "CONFLICT",
    "KHOA_NHOM",
    "PREFIX_HALF",
    "PREP_GAP",
    "SUNDAY_SPREAD",
    "MAX_PER_DAY",
    "CAPACITY",
)


@dataclass
class UnplacedExamDiagnostic:
    exam_id: str
    course_name: str
    exam_type: str
    size: int
    year1_student_count: int
    conflict_pair_count: int
    candidate_slots: int
    primary_blocker: str
    primary_blocker_vi: str
    detail_vi: str
    suggestions_vi: List[str] = field(default_factory=list)
    blocker_counts: Dict[str, int] = field(default_factory=dict)
    top_conflict_courses: List[str] = field(default_factory=list)


def diagnose_unplaced_exams(
    unplaced_exam_ids: List[str],
    exams: List[Exam],
    assignment: Dict[str, int],
    window: ScheduleWindow,
    allowed_sessions_by_exam_id: Dict[str, List[int]],
    *,
    session_half: List[int] | None = None,
    fixed_slots: Dict[str, int] | None = None,
    max_exams_per_day: int = 2,
    min_prep_days: float = 0.0,
    prep_day_per_credit: float = 0.6,
    total_capacity: int | None = None,
    weekend_large_course_min_students: int = 0,
    prefix_student_totals: Dict[str, int] | None = None,
    student_cohort_codes: Dict[str, str] | None = None,
    year1_cohort_anchor: int = 0,
    year1_allow_same_day: bool = True,
    sunday_spread_min: int = 3,
) -> List[UnplacedExamDiagnostic]:
    """Phân tích vì sao từng môn không gán được slot sau greedy (rất nguy hiểm nếu bỏ qua)."""
    if not unplaced_exam_ids:
        return []

    exam_index = {e.exam_id: i for i, e in enumerate(exams)}
    conflicts = build_conflict_index(exams)
    code_map = student_cohort_codes or build_student_cohort_code_map(
        exams, year1_anchor=year1_cohort_anchor
    )
    anchor = resolve_year1_cohort_anchor(year1_cohort_anchor, exams, code_map)
    prefix_tot = prefix_student_totals or build_prefix_student_totals(exams)
    fixed_slots = dict(fixed_slots or {})
    spd = window.sessions_per_day
    cap = total_capacity if total_capacity is not None else 10**9

    slot_used_students: Dict[int, set] = defaultdict(set)
    day_exams_per_student: Dict[Tuple[str, int], int] = defaultdict(int)
    student_day_exams: Dict[str, Dict[int, List[int]]] = defaultdict(lambda: defaultdict(list))
    slot_load: Dict[int, int] = defaultdict(int)
    day_khoa: Dict[Tuple[int, str], List[int]] = defaultdict(list)
    prefix_half_anchor: Dict[str, int] = {}
    sunday_days: set = set()

    for eid, slot in assignment.items():
        if eid not in exam_index:
            continue
        idx = exam_index[eid]
        ex = exams[idx]
        day = int(slot) // spd
        if weekday_at_day_index(window, day) == 6:
            sunday_days.add(day)
        slot_load[int(slot)] += ex.size
        if session_half and ex.course_prefix_7:
            sess = int(slot) % spd
            h = int(session_half[sess]) if sess < len(session_half) else 0
            pfx = ex.course_prefix_7
            if pfx not in prefix_half_anchor:
                prefix_half_anchor[pfx] = h
        for sid in ex.student_ids:
            slot_used_students[int(slot)].add(str(sid))
            day_exams_per_student[(str(sid), day)] += 1
            student_day_exams[str(sid)][day].append(idx)
        for k in exam_khoa_nhom_keys(ex):
            day_khoa[(day, k)].append(idx)

    def _is_y1(sid: str) -> bool:
        return anchor > 0 and is_year1_anchor_student(sid, code_map, anchor)

    def _all_slots(ex: Exam) -> List[int]:
        slots = enumerate_feasible_slots_for_exam(
            ex,
            window,
            allowed_sessions_by_exam_id,
            fixed_slots,
            weekend_large_min_students=weekend_large_course_min_students,
            prefix_totals=prefix_tot,
            relax_weekday_rule=False,
        )
        if not slots:
            slots = enumerate_feasible_slots_for_exam(
                ex,
                window,
                allowed_sessions_by_exam_id,
                fixed_slots,
                weekend_large_min_students=weekend_large_course_min_students,
                prefix_totals=prefix_tot,
                relax_weekday_rule=True,
            )
        if not slots:
            sessions = allowed_sessions_by_exam_id.get(
                ex.exam_id, list(range(spd))
            )
            sessions = sorted({int(s) for s in sessions if 0 <= int(s) < spd})
            plan_days = exam_allowed_day_indices(ex, window)
            if sessions and plan_days:
                slots = [
                    d * spd + s for d in sorted(plan_days) for s in sessions
                ]
        return slots

    reports: List[UnplacedExamDiagnostic] = []

    for eid in unplaced_exam_ids:
        if eid not in exam_index:
            continue
        idx = exam_index[eid]
        ex = exams[idx]
        y1_count = sum(1 for sid in ex.student_ids if _is_y1(str(sid)))
        conf_pairs = 0
        top_conf: List[Tuple[int, str]] = []
        for (i, j), w in conflicts.items():
            if i == idx:
                conf_pairs += 1
                top_conf.append((w, exams[j].course_name))
            elif j == idx:
                conf_pairs += 1
                top_conf.append((w, exams[i].course_name))
        top_conf.sort(reverse=True)
        top_names = [n for _, n in top_conf[:5]]

        slots_strict = enumerate_feasible_slots_for_exam(
            ex,
            window,
            allowed_sessions_by_exam_id,
            fixed_slots,
            weekend_large_min_students=weekend_large_course_min_students,
            prefix_totals=prefix_tot,
            relax_weekday_rule=False,
        )
        slots_all = _all_slots(ex)
        plan_days = exam_allowed_day_indices(ex, window)
        counts: Dict[str, int] = defaultdict(int)
        if not plan_days and window.khoa_lop_windows:
            counts["KE_HOACH"] = 1
        if not slots_all:
            if "KE_HOACH" not in counts:
                counts["NO_SLOT"] = 1
        elif not slots_strict and slots_all:
            counts["WEEKDAY"] = len(slots_all)

        for slot in slots_all:
            day = int(slot) // spd
            blocked: set[str] = set()
            if slot_load[slot] + ex.size > cap:
                blocked.add("CAPACITY")
            if max_exams_per_day > 0:
                for sid in ex.student_ids:
                    if day_exams_per_student[(str(sid), day)] >= max_exams_per_day:
                        blocked.add("MAX_PER_DAY")
                        break
            for sid in ex.student_ids:
                if str(sid) in slot_used_students[slot]:
                    blocked.add("CONFLICT")
                    break
            keys = exam_khoa_nhom_keys(ex)
            for k in keys:
                for oidx in day_khoa.get((day, k), []):
                    if not same_course_khoa_nhom_waiver(ex, exams[oidx]):
                        blocked.add("KHOA_NHOM")
                        break
            if session_half and ex.course_prefix_7:
                sess = int(slot) % spd
                h = int(session_half[sess]) if sess < len(session_half) else 0
                ah = prefix_half_anchor.get(ex.course_prefix_7)
                if ah is not None and h != ah:
                    blocked.add("PREFIX_HALF")
            if sunday_days and weekday_at_day_index(window, day) != 6:
                for sun_day in sunday_days:
                    if abs(day - sun_day) < sunday_spread_min:
                        for sid in ex.student_ids:
                            if sun_day in student_day_exams.get(str(sid), {}):
                                blocked.add("SUNDAY_SPREAD")
                                break
            if min_prep_days > 0 or prep_day_per_credit > 0:
                for sid in ex.student_ids:
                    y1 = _is_y1(str(sid))
                    for d, olist in student_day_exams.get(str(sid), {}).items():
                        for oidx in olist:
                            same_day = d == day
                            req = prep_hard_gap_days_for_pair(
                                ex,
                                exams[oidx],
                                prep_day_per_credit,
                                min_prep_days,
                                year1_allow_same_day=year1_allow_same_day,
                                for_year1_student=y1,
                                same_calendar_day=same_day,
                            )
                            if req > 0 and abs(d - day) + 1e-9 < req:
                                if y1:
                                    blocked.add("YEAR1_PREP")
                                else:
                                    blocked.add("PREP_GAP")
                                break
                        if blocked:
                            break
                    if blocked:
                        break
            if not blocked:
                continue
            for b in blocked:
                counts[b] += 1

        if not counts and slots_all:
            counts["YEAR1_PREP"] = len(slots_all)

        primary = "UNKNOWN"
        if counts:
            primary = max(
                counts.keys(),
                key=lambda k: (counts[k], -_BLOCKER_PRIORITY.index(k) if k in _BLOCKER_PRIORITY else 0),
            )
        primary_vi = _BLOCKER_VI.get(primary, primary)
        detail_parts = [
            f"{_BLOCKER_VI.get(k, k)}: {v}/{max(1, len(slots_all))} ô"
            for k, v in sorted(counts.items(), key=lambda x: -x[1])[:4]
        ]
        detail_vi = (
            f"{len(slots_all)} ô thời gian thử; {y1_count}/{ex.size} SV thuộc khóa {anchor:02d}; "
            f"{conf_pairs} cặp xung đột với môn khác. "
            + ("; ".join(detail_parts) if detail_parts else "Mọi ô đều bị chặn bởi ràng buộc cứng.")
        )
        suggestions: List[str] = []
        if primary == "NO_SLOT":
            suggestions.append("Kiểm tra cấu hình ca thi (lý thuyết/PBL/máy) và file kế hoạch ngày.")
        elif primary == "WEEKDAY":
            suggestions.append("Mở rộng đợt thi hoặc giảm ngưỡng «môn rất đông chỉ T7–CN».")
        elif primary == "YEAR1_PREP":
            suggestions.append(
                f"Giữa hai **ngày thi khác nhau**, SV khóa {anchor:02d} cần đủ ngày ôn — mở thêm ngày hoặc giãn lịch các môn trùng SV."
            )
            suggestions.append(
                "Thi cùng ngày vẫn được phép (theo max môn/SV/ngày); không cần tách khóa 25 khỏi cùng ngày."
            )
            if y1_count < ex.size:
                suggestions.append(
                    "Một phần SV không phải khóa 25 — có thể tách ca theo nhóm khóa nếu được phép."
                )
        elif primary == "CONFLICT":
            suggestions.append(
                "Các môn có nhiều SV trùng đang tranh cùng ô — cần thêm ngày/ca hoặc tách đề."
            )
        elif primary == "KHOA_NHOM":
            suggestions.append("Trải các môn cùng hậu tố 4 ký tự MalopHP ra nhiều ngày hơn.")
        elif primary == "PREFIX_HALF":
            suggestions.append("Các ca tách cùng học phần cần cùng buổi — thử mở thêm ngày cùng buổi.")
        elif primary == "MAX_PER_DAY":
            suggestions.append("Tăng «tối đa môn/SV/ngày» hoặc kéo dài đợt thi.")
        else:
            suggestions.append("Xem chi tiết từng dòng chặn ở trên và nới đúng ràng buộc tương ứng.")

        reports.append(
            UnplacedExamDiagnostic(
                exam_id=eid,
                course_name=ex.course_name,
                exam_type=ex.exam_type,
                size=ex.size,
                year1_student_count=y1_count,
                conflict_pair_count=conf_pairs,
                candidate_slots=len(slots_all),
                primary_blocker=primary,
                primary_blocker_vi=primary_vi,
                detail_vi=detail_vi,
                suggestions_vi=suggestions,
                blocker_counts=dict(counts),
                top_conflict_courses=top_names,
            )
        )
    reports.sort(key=lambda r: (-r.year1_student_count, -r.size, r.exam_id))
    return reports


def check_prep_no_time_by_prefix_hard(
    violations: List[PrepViolation],
    exams: List[Exam],
    max_no_prep_ratio: float = 0.10,
) -> List[str]:
    """Điều kiện cứng: với mỗi học phần (7 ký tự đầu MalopHP), nếu > max_no_prep_ratio
    số sinh viên của học phần đó có ít nhất một lần «không có thời gian ôn» trước môn thi
    (required > 0 và actual_days ≤ 0) thì báo lỗi.

    Trả về danh sách chuỗi mô tả từng học phần vi phạm (rỗng nếu pass).
    """
    exam_by_id = {e.exam_id: e for e in exams}
    by_name: Dict[str, List[Exam]] = defaultdict(list)
    for e in exams:
        by_name[e.course_name].append(e)

    prefix_students: Dict[str, set[str]] = {}
    prefix_label: Dict[str, str] = {}
    for e in exams:
        pfx = _malop_prefix_7_for_exam(e)
        if not pfx:
            continue
        if pfx not in prefix_students:
            prefix_students[pfx] = set()
            prefix_label[pfx] = e.course_name
        prefix_students[pfx].update(e.student_ids)

    prefix_bad: Dict[str, set[str]] = defaultdict(set)
    for v in violations:
        if v.required_days <= 0 or v.actual_days > 0:
            continue
        exam: Exam | None = None
        eid = (v.later_exam_id or "").strip()
        if eid and eid in exam_by_id:
            exam = exam_by_id[eid]
        else:
            cand = by_name.get(v.later_exam, [])
            if len(cand) == 1:
                exam = cand[0]
            else:
                continue
        pfx = _malop_prefix_7_for_exam(exam)
        if not pfx or pfx not in prefix_students:
            continue
        if v.student_id in prefix_students[pfx]:
            prefix_bad[pfx].add(v.student_id)

    errors: List[str] = []
    for pfx, total_set in sorted(prefix_students.items(), key=lambda x: x[0]):
        total = len(total_set)
        if total <= 0:
            continue
        bad = len(prefix_bad.get(pfx, set()))
        if bad / total > max_no_prep_ratio + 1e-15:
            pct = 100.0 * bad / total
            errors.append(
                f"Học phần mã 7 ký tự «{pfx}» ({prefix_label.get(pfx, '')}): "
                f"{bad}/{total} sinh viên ({pct:.1f}%) không có thời gian ôn trước (ít nhất một môn thi) "
                f"— vượt ngưỡng cứng {max_no_prep_ratio:.0%}."
            )
    return errors


def compute_kpi(
    scheduled: List[ScheduledExam],
    exams: List[Exam],
    window: ScheduleWindow,
    violations: List[PrepViolation],
    student_cohort: Dict[str, int] | None = None,
    student_cohort_codes: Dict[str, str] | None = None,
    year1_cohort_anchor: int = 0,
    year1_allow_same_day: bool = True,
) -> SchedulingKPI:
    kpi = SchedulingKPI()
    if not scheduled:
        return kpi
    exam_map = {e.exam_id: e for e in exams}
    by_slot: Dict[Tuple[str, int], int] = defaultdict(int)
    days = set()
    for item in scheduled:
        exam = exam_map.get(item.exam_id)
        size = exam.size if exam else 0
        by_slot[(item.exam_date.isoformat(), item.session)] += size
        days.add(item.exam_date)
    kpi.days_used = len(days)
    kpi.slots_used = len(by_slot)
    kpi.slot_utilization = kpi.slots_used / max(1, window.total_slots)
    if by_slot:
        loads = list(by_slot.values())
        kpi.avg_students_per_slot = sum(loads) / len(loads)
        kpi.max_students_per_slot = max(loads)
    kpi.prep_violation_count = len(violations)
    kpi.prep_violation_students = len({v.student_id for v in violations})
    code_map = student_cohort_codes or build_student_cohort_code_map(
        exams, year1_anchor=year1_cohort_anchor
    )
    anchor = resolve_year1_cohort_anchor(year1_cohort_anchor, exams, code_map)
    kpi.newest_cohort_code = anchor
    if anchor > 0:
        # Khóa năm 1: thiếu ngày ôn giữa hai ngày khác nhau (thi cùng ngày không tính nếu cho phép).
        y1_vios = [
            v
            for v in violations
            if is_year1_anchor_student(v.student_id, code_map, anchor)
            and v.required_days > 0
            and v.actual_days + 1e-9 < v.required_days
            and not (year1_allow_same_day and v.actual_days <= 0)
        ]
        kpi.prep_violation_count_year1 = len(y1_vios)
        kpi.prep_violation_students_year1 = len({v.student_id for v in y1_vios})
    # Vi phạm "0 ngày ôn" giữa hai môn khác ngày (cùng ngày chỉ tính khi không cho phép năm 1).
    same_day_vios = [
        v
        for v in violations
        if v.required_days > 0
        and v.actual_days <= 0
        and not (
            year1_allow_same_day
            and anchor > 0
            and is_year1_anchor_student(v.student_id, code_map, anchor)
        )
    ]
    kpi.same_day_violation_count = len(same_day_vios)
    kpi.same_day_violation_students = len({v.student_id for v in same_day_vios})

    # Thống kê gap (khoảng cách thực tế giữa các cặp môn vi phạm)
    if violations:
        gaps = [float(v.actual_days) for v in violations]
        kpi.avg_prep_gap = sum(gaps) / len(gaps)
        kpi.min_prep_gap = min(gaps)

    # PBL position score
    pbl_items = [item for item in scheduled if exam_map.get(item.exam_id) and exam_map[item.exam_id].priority > 0]
    if pbl_items and window.total_days > 1:
        max_day = window.total_days - 1
        positions = [
            (item.exam_date - window.start_date).days / max_day for item in pbl_items
        ]
        kpi.pbl_position_score = sum(positions) / len(positions)

    by_day: Dict[str, int] = defaultdict(int)
    for (day_str, _), load in by_slot.items():
        by_day[day_str] += load
    kpi.by_day_load = sorted(by_day.items())
    kpi.prep_prefix_hard_errors = check_prep_no_time_by_prefix_hard(violations, exams)
    return kpi
