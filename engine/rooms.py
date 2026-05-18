"""Phân phòng & giám thị sau khi đã có lịch slot (ngày + ca).

Mã hình thức thi (Ma_hinh_thuc) — khớp bảng quy định / cột «Mã» trong kế hoạch:
- 1 — Tự luận → ưu tiên phòng loại lý thuyết (theory); độ lấp đầy mục tiêu 90%–100%.
- 2 — Trắc nghiệm → ưu tiên phòng máy (computer); độ lấp đầy 85%–95%.
- 3 — Vấn đáp → không gò sức chứa theo số SV; gán tối thiểu một phòng phù hợp.

File phòng (cột «Mã ghép hình thức thi» hoặc RoomType) dùng cùng bộ mã 1/2/3 để gợi ý
loại phòng tương ứng; có thể dùng thêm chữ theory / computer / any.

File phòng: khi một môn cần nhiều phòng, ưu tiên gom phòng **cùng khu** — khu = **ký tự đầu**
của mã phòng (RoomID), ví dụ B101 và B205 cùng khu «B».
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from .models import Exam, Invigilator, Room, ScheduledExam


def format_ma_phong_chia(
    course_prefix_7: str,
    session_label: str,
    session_1based: int,
    room_seq_1based: int,
) -> str:
    """Ghép mã phòng/ca theo quy ước: 7 ký tự đầu học phần + ký hiệu ca (bỏ gạch dưới) + STT phòng 2 số.

    Ví dụ: ``1012107`` + ca ``C1`` + phòng thứ 1 → ``1012107C101`` (tương đương ``1012107_C1_01``).
    """
    pfx = str(course_prefix_7 or "").strip()
    if len(pfx) < 7:
        pfx = (pfx + "0" * 7)[:7]
    else:
        pfx = pfx[:7]
    raw = str(session_label or "").strip()
    compact = re.sub(r"[_\s\-.]+", "", raw)
    if not compact:
        compact = f"S{int(session_1based)}"
    seq = max(1, int(room_seq_1based))
    return f"{pfx}{compact}{seq:02d}"


def _partition_students_across_rooms(student_ids: List[str], assigned: List[Room]) -> List[List[str]]:
    """Chia SV đã sắp xếp theo mã vào từng phòng theo sức chứa; phòng cuối nhận phần dư."""
    students = sorted(str(s) for s in student_ids)
    if not assigned:
        return []
    n = len(assigned)
    groups: List[List[str]] = [[] for _ in range(n)]
    idx = 0
    for ri in range(n):
        cap = max(1, int(assigned[ri].capacity))
        if ri == n - 1:
            groups[ri] = students[idx:]
            break
        take = min(cap, len(students) - idx)
        groups[ri] = students[idx : idx + take]
        idx += take
    return groups


_THEORY_TYPES = frozenset(
    {
        "theory",
        "ly_thuyet",
        "ly_thuyết",
        "classroom",
        "phong_ly_thuyet",
        "phòng_lý_thuyết",
        "any",
        "",
    }
)
_COMP_TYPES = frozenset(
    {"computer", "may_tinh", "máy_tính", "lab", "phong_may", "phòng_máy", "any", ""}
)
_ORAL_TYPES = frozenset({"oral", "vandap", "vấn_đáp", "pbl", "any", ""})


def _norm_room_type(rt: str) -> str:
    return str(rt or "any").strip().lower()


def _room_format_code(room: Room) -> int | None:
    code = getattr(room, "room_format_code", None)
    if code is not None:
        try:
            v = int(code)
            return v if v in (1, 2, 3) else None
        except (TypeError, ValueError):
            return None
    return None


def _room_matches_exam_format(fmt: int, room: Room) -> bool:
    """Khớp mã ghép hình thức thi (1/2/3) giữa ca thi và phòng; không có mã → gợi ý theo loại phòng."""
    code = _room_format_code(room)
    if code is not None:
        return code == int(fmt)
    rt = _norm_room_type(room.room_type)
    if fmt == 2:
        return rt in _COMP_TYPES
    if fmt == 3:
        return rt in _ORAL_TYPES
    if fmt == 1:
        return rt in _THEORY_TYPES
    return True


def _eligible_rooms(fmt: int, pool: List[Room]) -> List[Room]:
    if not pool:
        return []
    matched = [r for r in pool if _room_matches_exam_format(fmt, r)]
    if matched:
        return matched
    # Không còn phòng đúng mã: nới theo quy tắc cũ (theory/computer/any) để tránh treo lịch
    if fmt == 2:
        out = [r for r in pool if _norm_room_type(r.room_type) in _COMP_TYPES]
        return out or list(pool)
    if fmt == 1:
        out = [r for r in pool if _norm_room_type(r.room_type) in _THEORY_TYPES]
        return out or list(pool)
    if fmt == 3:
        out = [r for r in pool if _norm_room_type(r.room_type) in _ORAL_TYPES]
        return out or list(pool)
    return list(pool)


def _capacity_target_range(need: int, util_low: float, util_high: float) -> Tuple[int, int]:
    """Tổng chỗ ngồi mong muốn sao cho need/sum ∈ [util_low, util_high]."""
    if need <= 0:
        return 0, 0
    lo = int(math.ceil(need / util_high))
    hi = int(math.floor(need / util_low + 1e-9)) if util_low > 1e-9 else need * 3
    hi = max(hi, lo)
    return lo, hi


def _pick_rooms_utilization(
    need: int,
    pool: List[Room],
    util_low: float,
    util_high: float,
) -> List[Room]:
    """FFD: ưu tiên phòng lớn trước, đạt tổng capacity trong [lo, hi] nếu có thể, luôn ≥ need."""
    if need <= 0 or not pool:
        return []
    lo, hi = _capacity_target_range(need, util_low, util_high)
    ordered = sorted(pool, key=lambda r: -r.capacity)
    chosen: List[Room] = []
    total = 0
    for r in ordered:
        chosen.append(r)
        total += r.capacity
        if total >= need and total >= lo:
            if total <= hi or total >= need * 1.2:
                break
    while total < need:
        for r in ordered:
            if r in chosen:
                continue
            chosen.append(r)
            total += r.capacity
            if total >= need:
                break
        else:
            break
    return chosen


def _zone_key(room: Room) -> str:
    """Khu = ký tự đầu tiên của mã phòng (RoomID), thống nhất khi gom nhiều phòng."""
    rid = str(room.room_id or "").strip()
    if not rid:
        return "_"
    return rid[0].upper()


def _pick_rooms_utilization_prefer_same_zone(
    need: int,
    pool: List[Room],
    util_low: float,
    util_high: float,
) -> List[Room]:
    """Giống FFD utilization nhưng ưu tiên tất cả phòng lấy từ cùng một khu nếu đủ sức chứa."""
    if need <= 0 or not pool:
        return []
    by_zone: Dict[str, List[Room]] = defaultdict(list)
    for r in pool:
        by_zone[_zone_key(r)].append(r)
    best: List[Room] | None = None
    best_score: Tuple[int, int, int] | None = None  # (num_rooms, -zone_cap, -picked_seats)
    for _zk, rs in by_zone.items():
        cap = sum(r.capacity for r in rs)
        if cap < need:
            continue
        picked = _pick_rooms_utilization(need, rs, util_low, util_high)
        seats = sum(r.capacity for r in picked)
        if seats < need:
            continue
        n_rooms = len(picked)
        score = (n_rooms, -cap, -seats)
        if best_score is None or score < best_score:
            best = picked
            best_score = score
    if best is not None:
        return best
    return _pick_rooms_utilization(need, pool, util_low, util_high)


def _pick_rooms_oral(pool: List[Room], need: int) -> List[Room]:
    """Vấn đáp: không tối ưu theo số SV — một phòng đủ lớn hoặc phòng nhỏ nhất có thể."""
    if not pool:
        return []
    if need <= 0:
        return [min(pool, key=lambda r: r.capacity)]
    asc = sorted(pool, key=lambda r: r.capacity)
    for r in asc:
        if r.capacity >= need:
            return [r]
    return [max(pool, key=lambda r: r.capacity)]


@dataclass
class RoomAssignmentReport:
    overflows: List[str] = field(default_factory=list)
    invigilator_shortage: List[str] = field(default_factory=list)
    room_usage: Dict[str, int] = field(default_factory=dict)
    invigilator_usage: Dict[str, int] = field(default_factory=dict)


def assign_rooms_and_invigilators(
    scheduled: List[ScheduledExam],
    exams: List[Exam],
    rooms: List[Room],
    invigilators: List[Invigilator],
    invigilators_per_room: int = 2,
    theory_fill_low: float = 0.90,
    theory_fill_high: float = 1.00,
    computer_fill_low: float = 0.85,
    computer_fill_high: float = 0.95,
) -> RoomAssignmentReport:
    """Phân phòng & giám thị in-place vào `scheduled`."""
    report = RoomAssignmentReport()
    exam_map = {e.exam_id: e for e in exams}
    if not rooms:
        return report

    room_pool = sorted(rooms, key=lambda r: r.capacity, reverse=True)
    inv_day_usage: Dict[Tuple[str, str], int] = defaultdict(int)
    inv_total_usage: Dict[str, int] = defaultdict(int)

    by_slot: Dict[Tuple[str, int], List[ScheduledExam]] = defaultdict(list)
    for s in scheduled:
        by_slot[(s.exam_date.isoformat(), s.session)].append(s)

    for (slot_day, slot_session), exam_list in by_slot.items():
        used_ids: Set[str] = set()
        for sched_exam in sorted(
            exam_list,
            key=lambda x: exam_map[x.exam_id].size if x.exam_id in exam_map else 0,
            reverse=True,
        ):
            exam = exam_map.get(sched_exam.exam_id)
            if exam is None:
                continue
            need = exam.size
            fmt = int(getattr(exam, "exam_format", 1) or 1)
            eligible = [r for r in room_pool if r.room_id not in used_ids]
            eligible = _eligible_rooms(fmt, eligible)

            if fmt == 3:
                assigned = _pick_rooms_oral(eligible, need)
            elif fmt == 2:
                assigned = _pick_rooms_utilization_prefer_same_zone(
                    need, eligible, computer_fill_low, computer_fill_high
                )
            else:
                assigned = _pick_rooms_utilization_prefer_same_zone(
                    need, eligible, theory_fill_low, theory_fill_high
                )

            seats = sum(r.capacity for r in assigned)
            if fmt != 3 and seats < need:
                report.overflows.append(
                    f"Thiếu phòng cho '{exam.course_name}' ngày {slot_day} ca {slot_session} "
                    f"(cần chỗ cho {need} SV, tổng sức chứa {seats})."
                )
            for r in assigned:
                used_ids.add(r.room_id)
            sched_exam.room_ids = [r.room_id for r in assigned]
            groups = _partition_students_across_rooms(exam.student_ids, assigned)
            sched_exam.room_student_groups = groups
            sess_lbl = str(getattr(sched_exam, "session_label", "") or "")
            sess_no = int(getattr(sched_exam, "session", 1) or 1)
            pfx7 = str(getattr(exam, "course_prefix_7", "") or "")
            sched_exam.room_split_codes = [
                format_ma_phong_chia(pfx7, sess_lbl, sess_no, ri + 1) for ri in range(len(assigned))
            ]
            for r in assigned:
                report.room_usage[r.room_id] = report.room_usage.get(r.room_id, 0) + 1

            if not invigilators:
                sched_exam.invigilator_ids = []
                continue
            needed = max(1, len(assigned)) * int(invigilators_per_room)
            candidates = sorted(
                invigilators,
                key=lambda i: (
                    inv_day_usage[(slot_day, i.invigilator_id)],
                    inv_total_usage[i.invigilator_id],
                ),
            )
            chosen: List[Invigilator] = []
            for inv in candidates:
                if inv_day_usage[(slot_day, inv.invigilator_id)] >= inv.max_sessions_per_day:
                    continue
                if inv_total_usage[inv.invigilator_id] >= inv.max_sessions_total:
                    continue
                chosen.append(inv)
                inv_day_usage[(slot_day, inv.invigilator_id)] += 1
                inv_total_usage[inv.invigilator_id] += 1
                if len(chosen) >= needed:
                    break
            if len(chosen) < needed:
                report.invigilator_shortage.append(
                    f"Thiếu giám thị cho '{exam.course_name}' ngày {slot_day} ca {slot_session} "
                    f"(cần {needed}, có {len(chosen)})."
                )
            sched_exam.invigilator_ids = [i.invigilator_id for i in chosen]

    report.invigilator_usage = dict(inv_total_usage)
    return report
