"""Chẩn đoán tiền-giải & KPI hậu-giải.

Mục tiêu:
- Trước khi gọi solver: cho người dùng thấy bài toán có khả thi không, ở đâu chật.
- Sau khi solve: cung cấp KPI để đánh giá chất lượng lịch.
"""
from __future__ import annotations

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
    pbl_position_score: float = 1.0   # 1.0 = tất cả PBL ở cuối đợt
    by_day_load: List[Tuple[str, int]] = field(default_factory=list)


def compute_kpi(
    scheduled: List[ScheduledExam],
    exams: List[Exam],
    window: ScheduleWindow,
    violations: List[PrepViolation],
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
    return kpi
