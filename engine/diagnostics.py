"""Chẩn đoán tiền-giải & KPI hậu-giải.

Mục tiêu:
- Trước khi gọi solver: cho người dùng thấy bài toán có khả thi không, ở đâu chật.
- Sau khi solve: cung cấp KPI để đánh giá chất lượng lịch.
"""
from __future__ import annotations

from datetime import timedelta
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


def khoa_nhom_from_malop(section_id: str) -> str:
    """4 ký tự cuối MalopHP (Khoa_nhom): các ca có cùng hậu tố không được thi cùng một ngày."""
    s = str(section_id or "").strip()
    if not s:
        return ""
    return s[-4:] if len(s) >= 4 else s


def cohort_index_from_malop(section_id: str) -> int:
    """Hai ký tự đầu của 4 ký tự cuối MalopHP — mã khóa (vd 25, 22). Chỉ lấy nếu cả hai là chữ số."""
    kn = khoa_nhom_from_malop(section_id)
    if len(kn) < 2:
        return 0
    head = kn[:2]
    return int(head) if head.isdigit() else 0


def exam_khoa_nhom_keys(exam: Exam) -> frozenset[str]:
    """Tập Khoa_nhom từ mọi MalopHP (section_id) của ca thi."""
    keys: set[str] = set()
    for sec in exam.section_ids:
        k = khoa_nhom_from_malop(sec)
        if k:
            keys.add(k)
    return frozenset(keys)


def exam_max_cohort_index(exam: Exam) -> int:
    """Khóa lớn nhất gắn với ca (max trên mọi MalopHP): khóa số lớn hơn → ưu tiên xếp lịch trước."""
    mx = 0
    for sec in exam.section_ids:
        mx = max(mx, cohort_index_from_malop(sec))
    return mx


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


def _weekday_at_day_index(window: ScheduleWindow, day_idx: int) -> int:
    return (window.start_date + timedelta(days=int(day_idx))).weekday()


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
    wd = _weekday_at_day_index(window, day_idx)
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
    slots: List[int] = []
    for d in range(window.total_days):
        if not relax_weekday_rule and weekend_large_min_students > 0:
            if not day_allowed_for_exam_weekday_rule(
                exam, d, window, weekend_large_min_students, totals
            ):
                continue
        for s in sessions:
            slots.append(d * window.sessions_per_day + s)
    return slots


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
    kpi.prep_prefix_hard_errors = check_prep_no_time_by_prefix_hard(violations, exams)
    return kpi
