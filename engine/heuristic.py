"""Greedy DSATUR scheduler — luôn cho ra một lịch khả thi (hoặc gần khả thi).

Ý tưởng:
- Xây đồ thị xung đột (môn nào có chung SV thì có cạnh, trọng số = số SV trùng).
- Sắp xếp môn theo độ "saturation" (bậc còn lại) giảm dần — giống DSATUR cho graph coloring.
- Với mỗi môn, chọn slot tốt nhất theo điểm tổng hợp:
    * Không xung đột (hard).
    * Không vi phạm max-exams/day (hard).
    * Tránh quá tải sức chứa (soft, càng cân bằng càng tốt).
    * Đảm bảo prep-days với các môn đã xếp của SV (soft).
    * PBL push-late: ưu tiên ngày cuối đợt cho môn priority cao.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Dict, List, Optional, Tuple

from .diagnostics import build_conflict_index
from .models import Exam, ScheduledExam, ScheduleWindow


@dataclass
class HeuristicResult:
    assignment: Dict[str, int]                # exam_id -> slot index
    unplaced: List[str]                       # exam_id không đặt được (sau khi đã thử nới)
    relaxations: List[str]                    # ghi nhận đã nới gì


def _slot_to_day_session(slot: int, sessions_per_day: int) -> Tuple[int, int]:
    return slot // sessions_per_day, slot % sessions_per_day


def _build_allowed_slots(
    exam_id: str,
    allowed_sessions_by_exam_id: Dict[str, List[int]],
    window: ScheduleWindow,
    fixed_slots: Dict[str, int] | None = None,
) -> List[int]:
    if fixed_slots and exam_id in fixed_slots:
        return [int(fixed_slots[exam_id])]
    sessions = allowed_sessions_by_exam_id.get(
        exam_id, list(range(window.sessions_per_day))
    )
    sessions = sorted({int(s) for s in sessions if 0 <= int(s) < window.sessions_per_day})
    if not sessions:
        return []
    return [d * window.sessions_per_day + s for d in range(window.total_days) for s in sessions]


def schedule_greedy(
    exams: List[Exam],
    window: ScheduleWindow,
    allowed_sessions_by_exam_id: Dict[str, List[int]] | None = None,
    max_exams_per_day: int = 2,
    min_prep_days: float = 0.0,
    prep_day_per_credit: float = 0.6,
    total_capacity: int | None = None,
    fixed_slots: Dict[str, int] | None = None,
    base_slots: Dict[str, int] | None = None,
    balance_weight: float = 1.0,
    soft_slot_cap: int | None = None,
) -> HeuristicResult:
    """Trả về HeuristicResult với map exam_id -> slot.

    Greedy không bao giờ raise: nếu không tìm được slot không xung đột cho 1 môn,
    sẽ thử nới lần lượt: (1) bỏ kiểm tra capacity, (2) bỏ kiểm tra max_exams_per_day,
    (3) chấp nhận xung đột ít người nhất. Cuối cùng ghi nhận vào `relaxations`.
    """
    allowed_sessions_by_exam_id = allowed_sessions_by_exam_id or {}
    fixed_slots = dict(fixed_slots or {})
    base_slots = dict(base_slots or {})

    conflicts = build_conflict_index(exams)
    # adjacency: exam_idx -> {exam_idx: overlap}
    adj: Dict[int, Dict[int, int]] = defaultdict(dict)
    for (i, j), w in conflicts.items():
        adj[i][j] = w
        adj[j][i] = w

    # Tính bậc & student_count để sắp xếp ban đầu
    n = len(exams)
    exam_index = {e.exam_id: i for i, e in enumerate(exams)}

    # Cận trên kích cỡ ca thi
    if total_capacity is None:
        total_capacity = 10**9

    # Trạng thái sau khi đặt
    slot_load_students: Dict[int, int] = defaultdict(int)   # slot -> tổng SV
    slot_used_students: Dict[int, set] = defaultdict(set)   # slot -> set student_id
    day_load_students: Dict[int, int] = defaultdict(int)    # day -> tổng SV (cho balance)
    day_exams_per_student: Dict[Tuple[str, int], int] = defaultdict(int)  # (sid, day) -> count
    student_days_taken: Dict[str, List[int]] = defaultdict(list)  # sid -> sorted day list
    assignment: Dict[str, int] = {}

    # Mục tiêu phân bố đều: tổng SV / số ngày (giúp scoring biết khi nào "quá tải")
    total_student_demand = sum(e.size for e in exams)
    target_per_day = total_student_demand / max(1, window.total_days)
    target_per_slot = total_student_demand / max(1, window.total_slots)

    relaxations: List[str] = []
    unplaced: List[str] = []

    # --- Đặt sẵn fixed_slots trước ---
    for exam_id, slot in fixed_slots.items():
        if exam_id not in exam_index:
            continue
        idx = exam_index[exam_id]
        exam = exams[idx]
        day, _ = _slot_to_day_session(slot, window.sessions_per_day)
        for sid in exam.student_ids:
            slot_used_students[slot].add(sid)
            day_exams_per_student[(sid, day)] += 1
            student_days_taken[sid].append(day)
        slot_load_students[slot] += exam.size
        day_load_students[day] += exam.size
        assignment[exam_id] = slot

    # --- Sắp xếp môn theo (priority DESC, degree DESC, size DESC) ---
    remaining = [i for i, e in enumerate(exams) if e.exam_id not in assignment]

    def _order_key(i: int) -> Tuple[int, int, int, int]:
        e = exams[i]
        # priority cao xếp trước (để được "cuối đợt"); rồi đến degree, size, neg-id (stable).
        return (-e.priority, -len(adj[i]), -e.size, i)

    remaining.sort(key=_order_key)

    def _try_place(idx: int, slot: int, allow_conflict: bool, ignore_capacity: bool, ignore_per_day: bool) -> bool:
        exam = exams[idx]
        day, _ = _slot_to_day_session(slot, window.sessions_per_day)
        # Xung đột cứng: cùng slot có SV trùng
        if not allow_conflict:
            for sid in exam.student_ids:
                if sid in slot_used_students[slot]:
                    return False
        # Capacity
        if not ignore_capacity:
            if slot_load_students[slot] + exam.size > total_capacity:
                return False
        # max_exams_per_day
        if not ignore_per_day and max_exams_per_day > 0:
            for sid in exam.student_ids:
                if day_exams_per_student[(sid, day)] >= max_exams_per_day:
                    return False
        # min_prep_days hard (chỉ kiểm khi >0)
        if min_prep_days > 0:
            req = ceil(min_prep_days)
            for sid in exam.student_ids:
                for d in student_days_taken[sid]:
                    if abs(d - day) < req:
                        return False
        return True

    def _commit(idx: int, slot: int) -> None:
        exam = exams[idx]
        day, _ = _slot_to_day_session(slot, window.sessions_per_day)
        for sid in exam.student_ids:
            slot_used_students[slot].add(sid)
            day_exams_per_student[(sid, day)] += 1
            student_days_taken[sid].append(day)
        slot_load_students[slot] += exam.size
        day_load_students[day] += exam.size
        assignment[exam.exam_id] = slot

    def _score_slot(idx: int, slot: int) -> float:
        """Càng thấp càng tốt. Tổng hợp 4 thành phần điểm:
        1. PBL push-late (ưu tiên ngày cuối cho môn priority)
        2. Load balance theo ngày & ca (chính – ép trải đều)
        3. Prep-day mềm (tránh thi liên tiếp cho SV trùng)
        4. Repair distance (giữ gần lịch cũ khi đổi thủ công)
        """
        exam = exams[idx]
        day, sess = _slot_to_day_session(slot, window.sessions_per_day)
        score = 0.0

        # (1) PBL push-late: muốn day càng lớn
        if exam.priority > 0:
            score += (window.total_days - 1 - day) * exam.priority * 0.5

        # (2) Load-balance — đây là phần mạnh nhất:
        # Phạt theo TỈ LỆ vượt mục tiêu (square để ép cân bằng).
        projected_day = day_load_students[day] + exam.size
        projected_slot = slot_load_students[slot] + exam.size
        day_ratio = projected_day / max(1.0, target_per_day)
        slot_ratio = projected_slot / max(1.0, target_per_slot)
        balance_pen = 0.0
        if day_ratio > 1.0:
            balance_pen += (day_ratio - 1.0) ** 2 * 100.0
        else:
            balance_pen += (1.0 - day_ratio) * 1.0
        if slot_ratio > 1.0:
            balance_pen += (slot_ratio - 1.0) ** 2 * 50.0

        # Soft cap = "nearly hard": phạt bậc 3 khi vượt, hệ số rất lớn.
        # Nếu cap đặt và slot này đã có exam.size làm vượt → coi như cấm trừ khi không còn lựa chọn.
        if soft_slot_cap and projected_slot > soft_slot_cap:
            overflow = projected_slot - soft_slot_cap
            balance_pen += (overflow ** 1.5) * 1.0 + 10_000.0  # base 10k để gần như cấm

        score += balance_pen * balance_weight

        # (3) Prep-day soft penalty: SV có môn gần đây thì giảm điểm slot này
        req = max(min_prep_days, exam.credits * prep_day_per_credit)
        if req > 0:
            penalty = 0.0
            # sampling cho môn rất đông SV để giữ O(n)
            sample = exam.student_ids if len(exam.student_ids) <= 300 else exam.student_ids[::max(1, len(exam.student_ids) // 300)]
            for sid in sample:
                for d in student_days_taken[sid]:
                    diff = abs(d - day)
                    if diff < req:
                        penalty += (req - diff)
            scale = len(exam.student_ids) / max(1, len(sample))
            score += penalty * scale * 0.05

        # (4) Giữ gần base_slots khi repair
        prev = base_slots.get(exam.exam_id)
        if prev is not None:
            score += abs(prev - slot) * 0.001

        return score

    def _place_with_policy(allow_conflict: bool, ignore_capacity: bool, ignore_per_day: bool) -> List[int]:
        """Thử xếp lần lượt các môn còn lại; trả về list các môn vẫn không đặt được."""
        still_unplaced: List[int] = []
        ordered = list(remaining_state["pending"])
        ordered.sort(key=_order_key)
        for idx in ordered:
            exam = exams[idx]
            allowed = _build_allowed_slots(
                exam.exam_id, allowed_sessions_by_exam_id, window, fixed_slots
            )
            if not allowed:
                still_unplaced.append(idx)
                continue
            best_slot: Optional[int] = None
            best_score = float("inf")
            for slot in allowed:
                if not _try_place(idx, slot, allow_conflict, ignore_capacity, ignore_per_day):
                    continue
                s = _score_slot(idx, slot)
                if s < best_score:
                    best_score = s
                    best_slot = slot
            if best_slot is None:
                still_unplaced.append(idx)
            else:
                _commit(idx, best_slot)
        return still_unplaced

    remaining_state = {"pending": remaining}

    # 1) Thử strict
    leftover = _place_with_policy(allow_conflict=False, ignore_capacity=False, ignore_per_day=False)
    remaining_state["pending"] = leftover

    # 2) Nới capacity (post-room sẽ kiểm sau)
    if leftover:
        relaxations.append("Bỏ qua giới hạn sức chứa tổng phòng ở pha greedy (sẽ phân phòng sau).")
        leftover = _place_with_policy(allow_conflict=False, ignore_capacity=True, ignore_per_day=False)
        remaining_state["pending"] = leftover

    # 3) Nới max_exams_per_day
    if leftover:
        relaxations.append("Cho phép vượt giới hạn môn/SV/ngày để đảm bảo có lịch.")
        leftover = _place_with_policy(allow_conflict=False, ignore_capacity=True, ignore_per_day=True)
        remaining_state["pending"] = leftover

    # 4) Cuối cùng: chấp nhận xung đột tối thiểu (last-resort)
    if leftover:
        relaxations.append("Chấp nhận một số môn bị xung đột SV — cần rà soát thủ công.")
        # Chấm điểm thêm phạt xung đột
        for idx in list(leftover):
            exam = exams[idx]
            allowed = _build_allowed_slots(
                exam.exam_id, allowed_sessions_by_exam_id, window, fixed_slots
            )
            best_slot, best_pen = None, float("inf")
            for slot in allowed:
                overlap = sum(1 for sid in exam.student_ids if sid in slot_used_students[slot])
                pen = overlap * 1000.0 + _score_slot(idx, slot)
                if pen < best_pen:
                    best_pen = pen
                    best_slot = slot
            if best_slot is not None:
                _commit(idx, best_slot)
                leftover.remove(idx)

    unplaced = [exams[i].exam_id for i in leftover]
    return HeuristicResult(assignment=assignment, unplaced=unplaced, relaxations=relaxations)


# ---------------------------------------------------------------------------
# LNS post-improvement (Large Neighborhood Search lite)
# ---------------------------------------------------------------------------

def lns_improve(
    assignment: Dict[str, int],
    exams: List[Exam],
    window: ScheduleWindow,
    allowed_sessions_by_exam_id: Dict[str, List[int]],
    max_exams_per_day: int = 2,
    min_prep_days: float = 0.0,
    prep_day_per_credit: float = 0.6,
    iterations: int = 3,
    pool_size: int = 100,
    balance_weight: float = 1.0,
    soft_slot_cap: int | None = None,
    fixed_slots: Dict[str, int] | None = None,
    progress_cb=None,
) -> Tuple[Dict[str, int], List[str]]:
    """Cải thiện assignment bằng cách lặp lại: chọn pool_size môn vi phạm prep
    nhiều nhất, gỡ ra, gọi lại greedy để re-place. Trả về (assignment mới, log).
    """
    if not assignment or pool_size <= 0:
        return assignment, []

    logs: List[str] = []
    fixed_slots = fixed_slots or {}
    exam_index = {e.exam_id: i for i, e in enumerate(exams)}
    current = dict(assignment)

    def _compute_violation_score(asgn: Dict[str, int]) -> Tuple[int, Dict[str, int]]:
        """Trả về (total_violation_count, per_exam_violation_count)."""
        # Map exam_id -> day
        exam_day: Dict[str, int] = {
            eid: slot // window.sessions_per_day for eid, slot in asgn.items()
        }
        # Per-student exam list (sorted by day)
        per_student: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
        for eid, day in exam_day.items():
            if eid not in exam_index:
                continue
            exam = exams[exam_index[eid]]
            for sid in exam.student_ids:
                per_student[sid].append((day, eid))
        per_exam_vio: Dict[str, int] = defaultdict(int)
        total = 0
        for sid, entries in per_student.items():
            entries.sort()
            for i in range(1, len(entries)):
                prev_day, prev_eid = entries[i - 1]
                curr_day, curr_eid = entries[i]
                curr_exam = exams[exam_index[curr_eid]]
                req = curr_exam.credits * prep_day_per_credit
                if (curr_day - prev_day) + 1e-9 < req:
                    per_exam_vio[curr_eid] += 1
                    per_exam_vio[prev_eid] += 1
                    total += 1
        return total, per_exam_vio

    base_vio, _ = _compute_violation_score(current)
    logs.append(f"Cải tiến LNS: số vi phạm ngày ôn ban đầu = {base_vio:,}")

    # Move-based local search: với mỗi môn vi phạm nhiều, thử di chuyển sang slot tốt hơn.
    # Đánh giá delta = vi phạm prep & xung đột cứng (no-overlap) & max-day.
    # Cấu trúc lưu trữ tăng cường để O(1) check.

    sessions_per_day = window.sessions_per_day

    # Build per-student exam list
    student_to_exams: Dict[str, List[str]] = defaultdict(list)
    for eid in current.keys():
        if eid not in exam_index:
            continue
        for sid in exams[exam_index[eid]].student_ids:
            student_to_exams[sid].append(eid)

    # Build slot -> set(student_id) and (sid, day) -> count
    slot_students: Dict[int, set] = defaultdict(set)
    student_day_count: Dict[Tuple[str, int], int] = defaultdict(int)
    for eid, slot in current.items():
        if eid not in exam_index:
            continue
        day = slot // sessions_per_day
        for sid in exams[exam_index[eid]].student_ids:
            slot_students[slot].add(sid)
            student_day_count[(sid, day)] += 1

    def _vio_for_exam(eid: str, slot: int) -> int:
        """Số vi phạm prep liên quan đến exam này (nếu nó ở slot)."""
        if eid not in exam_index:
            return 0
        exam = exams[exam_index[eid]]
        day = slot // sessions_per_day
        req = exam.credits * prep_day_per_credit
        vio = 0
        for sid in exam.student_ids:
            for other_eid in student_to_exams[sid]:
                if other_eid == eid:
                    continue
                other_slot = current.get(other_eid)
                if other_slot is None:
                    continue
                other_day = other_slot // sessions_per_day
                gap = abs(day - other_day)
                if gap + 1e-9 < req:
                    vio += 1
                other_exam = exams[exam_index[other_eid]]
                req_other = other_exam.credits * prep_day_per_credit
                # Đếm thêm chiều ngược (other vs this); nhưng nếu req khác có thể khác
                # Để đơn giản, dùng max(req, req_other) sẽ overcount; ta tách 2 chiều:
                if other_eid != eid and gap + 1e-9 < req_other:
                    # đã được tính khi xét exam khác, đừng double-count
                    pass
        return vio

    def _conflict_at_slot(eid: str, slot: int) -> bool:
        exam = exams[exam_index[eid]]
        old_slot = current.get(eid)
        for sid in exam.student_ids:
            if sid in slot_students[slot] and old_slot != slot:
                return True
        return False

    def _violates_max_per_day(eid: str, new_slot: int) -> bool:
        if max_exams_per_day <= 0:
            return False
        exam = exams[exam_index[eid]]
        old_slot = current[eid]
        old_day = old_slot // sessions_per_day
        new_day = new_slot // sessions_per_day
        if old_day == new_day:
            return False
        for sid in exam.student_ids:
            if student_day_count[(sid, new_day)] >= max_exams_per_day:
                return True
        return False

    def _move(eid: str, new_slot: int) -> None:
        exam = exams[exam_index[eid]]
        old_slot = current[eid]
        old_day = old_slot // sessions_per_day
        new_day = new_slot // sessions_per_day
        for sid in exam.student_ids:
            slot_students[old_slot].discard(sid)
            slot_students[new_slot].add(sid)
            student_day_count[(sid, old_day)] -= 1
            student_day_count[(sid, new_day)] += 1
        slot_size_load[old_slot] -= exam.size
        slot_size_load[new_slot] += exam.size
        current[eid] = new_slot

    def _peak_cost(eid: str, slot: int) -> float:
        """Chi phí 'vượt cap' nếu đặt eid vào slot. 0 nếu không có cap hoặc còn dưới cap."""
        if not soft_slot_cap:
            return 0.0
        old_slot = current[eid]
        size = exams[exam_index[eid]].size
        # Nếu di chuyển vào slot này thì load mới là:
        projected = slot_size_load[slot] + (0 if slot == old_slot else size)
        if projected <= soft_slot_cap:
            return 0.0
        return (projected - soft_slot_cap) * PEAK_WEIGHT

    # Pre-compute allowed slots per exam
    allowed_by_exam: Dict[str, List[int]] = {}
    for eid in current.keys():
        sessions = allowed_sessions_by_exam_id.get(eid, list(range(sessions_per_day)))
        sessions = sorted({int(s) for s in sessions if 0 <= int(s) < sessions_per_day})
        allowed_by_exam[eid] = [d * sessions_per_day + s for d in range(window.total_days) for s in sessions]

    # Slot load tracking (SV per slot) — để LNS biết peak nào đang đông
    slot_size_load: Dict[int, int] = defaultdict(int)
    for eid, slot in current.items():
        if eid in exam_index:
            slot_size_load[slot] += exams[exam_index[eid]].size

    PEAK_WEIGHT = 0.001  # mỗi SV vượt cap đáng giá 0.001 vi phạm

    no_improve_count = 0
    for it in range(iterations):
        if progress_cb:
            progress_cb(
                int(50 + (it / max(1, iterations)) * 20),
                f"Bước 2/3: vòng cải tiến LNS thứ {it + 1}/{iterations}…",
            )
        total_vio, per_exam_vio = _compute_violation_score(current)
        if total_vio == 0:
            break
        # Sắp xếp ứng viên: nhiều vi phạm trước
        ranked = sorted(
            (
                (eid, v)
                for eid, v in per_exam_vio.items()
                if eid not in fixed_slots
            ),
            key=lambda x: -x[1],
        )[:pool_size]
        moved = 0
        before = total_vio
        for eid, _ in ranked:
            old_slot = current[eid]
            cur_vio = _vio_for_exam(eid, old_slot)
            cur_peak = _peak_cost(eid, old_slot)
            # Cho phép move nếu slot hiện tại có peak cost (slot đông) — kể cả vio=0
            if cur_vio == 0 and cur_peak == 0:
                continue
            best_slot = old_slot
            best_score = cur_vio + cur_peak
            for slot in allowed_by_exam[eid]:
                if slot == old_slot:
                    continue
                if _conflict_at_slot(eid, slot):
                    continue
                if _violates_max_per_day(eid, slot):
                    continue
                v = _vio_for_exam(eid, slot)
                p = _peak_cost(eid, slot)
                s = v + p
                if s < best_score - 1e-9:
                    best_score = s
                    best_slot = slot
            if best_slot != old_slot:
                _move(eid, best_slot)
                moved += 1
        after, _ = _compute_violation_score(current)
        if after < before:
            logs.append(
                f"Vòng LNS thứ {it + 1}: {before:,} → {after:,} (giảm {before - after:,}, đã chuyển {moved} môn)"
            )
            base_vio = after
            no_improve_count = 0
        else:
            logs.append(f"Vòng LNS thứ {it + 1}: {before:,} (không cải thiện, đã thử chuyển {moved} môn).")
            no_improve_count += 1
            if no_improve_count >= 2:
                logs.append("Dừng cải tiến LNS sớm: hai vòng liên tiếp không cải thiện.")
                break

    return current, logs


def heuristic_to_scheduled(
    heuristic: HeuristicResult,
    exams: List[Exam],
    window: ScheduleWindow,
    session_labels: List[str] | None = None,
) -> List[ScheduledExam]:
    out: List[ScheduledExam] = []
    for exam in exams:
        slot = heuristic.assignment.get(exam.exam_id)
        if slot is None:
            continue
        day_idx, sess_idx = _slot_to_day_session(slot, window.sessions_per_day)
        out.append(
            ScheduledExam(
                exam_id=exam.exam_id,
                course_id=exam.course_id,
                course_name=exam.course_name,
                exam_date=window.start_date.fromordinal(window.start_date.toordinal() + day_idx),
                session=sess_idx + 1,
                session_label=(session_labels[sess_idx] if session_labels and sess_idx < len(session_labels) else ""),
            )
        )
    return sorted(out, key=lambda x: (x.exam_date, x.session, x.course_name))
