"""Phân phòng & giám thị sau khi đã xếp lịch slot (day/session).

Tách module riêng để dễ thay đổi quy tắc bin-packing trong tương lai (FFD, BFD, hoặc CP).
Triết lý: ưu tiên phòng to cho môn đông, cân bằng tải giám thị theo ngày.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .models import Exam, Invigilator, Room, ScheduledExam


@dataclass
class RoomAssignmentReport:
    overflows: List[str] = field(default_factory=list)         # mô tả slot/môn thiếu phòng
    invigilator_shortage: List[str] = field(default_factory=list)
    room_usage: Dict[str, int] = field(default_factory=dict)   # room_id -> số ca dùng
    invigilator_usage: Dict[str, int] = field(default_factory=dict)


def assign_rooms_and_invigilators(
    scheduled: List[ScheduledExam],
    exams: List[Exam],
    rooms: List[Room],
    invigilators: List[Invigilator],
    invigilators_per_room: int = 2,
) -> RoomAssignmentReport:
    """Phân phòng & giám thị in-place vào `scheduled`. Trả về báo cáo lỗi/quá tải."""
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
        used_rooms: set[str] = set()
        for sched_exam in sorted(
            exam_list,
            key=lambda x: exam_map[x.exam_id].size if x.exam_id in exam_map else 0,
            reverse=True,
        ):
            exam = exam_map.get(sched_exam.exam_id)
            if exam is None:
                continue
            need = exam.size
            assigned: List[Room] = []
            seats = 0
            for room in room_pool:
                if room.room_id in used_rooms:
                    continue
                assigned.append(room)
                used_rooms.add(room.room_id)
                seats += room.capacity
                if seats >= need:
                    break
            if seats < need:
                report.overflows.append(
                    f"Thiếu phòng cho '{exam.course_name}' ngày {slot_day} ca {slot_session} "
                    f"(cần {need}, có {seats})."
                )
                # vẫn ghi lại các phòng đã chiếm để có dấu vết
            sched_exam.room_ids = [r.room_id for r in assigned]
            for r in assigned:
                report.room_usage[r.room_id] = report.room_usage.get(r.room_id, 0) + 1

            # Giám thị
            if not invigilators:
                sched_exam.invigilator_ids = []
                continue
            needed = max(1, len(assigned)) * invigilators_per_room
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
