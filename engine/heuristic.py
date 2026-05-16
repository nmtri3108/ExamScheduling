"""Greedy DSATUR scheduler — luôn cho ra một lịch khả thi (hoặc gần khả thi).

Ý tưởng:
- Xây đồ thị xung đột (môn nào có chung SV thì có cạnh, trọng số = số SV trùng).
- Sắp xếp môn theo **sóng khóa** (MalopHP): khóa mới nhất trong đợt (≈ năm 1, 2 số đầu của 4 ký tự cuối lớn hơn) **xếp trước**,
  hết một sóng mới sang sóng khóa cũ hơn; trong cùng sóng: priority / bậc / quy mô.
- Với mỗi môn, chọn slot tốt nhất theo điểm tổng hợp:
    * Không xung đột (hard).
    * Không vi phạm max-exams/day (hard).
    * Khoa_nhom (4 ký tự cuối MalopHP): hai môn khác nhau cùng hậu tố không cùng ngày (hard; ca tách cùng học phần miễn).
    * Tránh quá tải sức chứa (soft, càng cân bằng càng tốt).
    * Ôn theo cặp môn: max(tín chỉ) × prep_day_per_credit (cứng trước, nới dần nếu chật).
    * Sunday-spread (mềm): môn có SV trùng môn thi Chủ nhật → tránh xếp quá gần CN.
    * PBL push-late: ưu tiên ngày cuối đợt cho môn priority cao.

Nới dần khi không xếp hết: capacity → môn/SV/ngày → trùng SV (giữ ôn+CN) → Khoa_nhom → ép cuối.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import ceil
from typing import Dict, List, Optional, Tuple

from .diagnostics import (
    build_conflict_index,
    build_prefix_student_totals,
    diagnose_unplaced_exams,
    enumerate_feasible_slots_for_exam,
    build_student_cohort_code_map,
    build_student_cohort_map,
    cohort_wave_index,
    exam_khoa_nhom_keys,
    exam_min_cohort_wave,
    is_year1_anchor_student,
    resolve_year1_cohort_anchor,
    prep_days_required_for_pair,
    prep_hard_gap_days_for_pair,
    same_course_khoa_nhom_waiver,
    weekday_at_day_index,
    UnplacedExamDiagnostic,
)
from .models import Exam, ScheduledExam, ScheduleWindow


@dataclass
class HeuristicResult:
    assignment: Dict[str, int]                # exam_id -> slot index
    unplaced: List[str]                       # exam_id không đặt được (sau khi đã thử nới)
    relaxations: List[str]                    # ghi nhận đã nới gì
    unplaced_diagnostics: List[UnplacedExamDiagnostic] = field(default_factory=list)


def _slot_to_day_session(slot: int, sessions_per_day: int) -> Tuple[int, int]:
    return slot // sessions_per_day, slot % sessions_per_day


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
    session_half: List[int] | None = None,
    weekend_large_course_min_students: int = 0,
    prefix_student_totals: Dict[str, int] | None = None,
    spread_prep_factor: float = 1.0,
    student_cohort: Dict[str, int] | None = None,
    student_cohort_codes: Dict[str, str] | None = None,
    year1_cohort_anchor: int = 0,
    year1_allow_same_day: bool = True,
) -> HeuristicResult:
    """Trả về HeuristicResult với map exam_id -> slot.

    Greedy không bao giờ raise: nới lần lượt capacity → max_exams_per_day → ôn theo tín chỉ
    → xung đột SV tối thiểu; ghi nhận trong `relaxations`.
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
    # sid -> day_idx -> các exam_idx đã xếp (ôn theo cặp / max tín chỉ)
    student_day_exams: Dict[str, Dict[int, List[int]]] = defaultdict(lambda: defaultdict(list))
    # Ngày Chủ nhật đã có môn đông xếp vào → các môn khác nên cách ít nhất _SUNDAY_SPREAD_MIN ngày.
    sunday_exam_days: set = set()   # day_idx (weekday==6) của môn đã xếp vào CN
    _SUNDAY_SPREAD_MIN = 3          # giãn tối thiểu so với môn CN (vd Vật lý CN → Giải tích ≥3 ngày)
    _SUNDAY_SPREAD_WEIGHT = 8.0    # phạt mềm mạnh khi SV trùng môn thi CN mà xếp quá gần
    assignment: Dict[str, int] = {}

    # Mục tiêu phân bố đều: tổng SV / số ngày (giúp scoring biết khi nào "quá tải")
    total_student_demand = sum(e.size for e in exams)
    target_per_day = total_student_demand / max(1, window.total_days)
    target_per_slot = total_student_demand / max(1, window.total_slots)

    relaxations: List[str] = []
    prefix_tot: Dict[str, int] = dict(prefix_student_totals or {})
    if weekend_large_course_min_students > 0 and not prefix_tot:
        prefix_tot = build_prefix_student_totals(exams)
    weekday_relax_logged = False
    # Cùng 7 ký tự HP: các ca tách cùng buổi (0=sáng, 1=chiều); có thể khác ngày.
    prefix_half_anchor: Dict[str, int] = {}

    # Khoa_nhom = 4 ký tự cuối MalopHP: hai môn khác nhau cùng hậu tố không cùng ngày
    # (trừ ca tách cùng học phần — same_course_khoa_nhom_waiver).
    exam_khoa_keys = [exam_khoa_nhom_keys(e) for e in exams]
    if not student_cohort_codes:
        student_cohort_codes = build_student_cohort_code_map(
            exams, year1_anchor=year1_cohort_anchor
        )
    if not student_cohort:
        student_cohort = build_student_cohort_map(
            exams, student_cohort_codes=student_cohort_codes, year1_anchor=year1_cohort_anchor
        )
    year1_anchor = resolve_year1_cohort_anchor(
        year1_cohort_anchor, exams, student_cohort_codes
    )
    exam_wave = [exam_min_cohort_wave(exams[i], year1_anchor) for i in range(n)]
    if year1_anchor > 0:
        relaxations.append(
            f"Xếp theo niên khóa (năm 1 = {year1_anchor:02d}, 2 số đầu của 4 ký tự cuối MalopHP): "
            f"ưu tiên {year1_anchor:02d} → … → mã lạ (yy, zz, >{year1_anchor:02d}) cuối; "
            f"SV khóa {year1_anchor:02d} bắt buộc "
            f"{'được thi cùng ngày (max môn/ngày); giữa hai ngày khác nhau cần đủ ngày ôn' if year1_allow_same_day else 'đủ ngày ôn theo tín chỉ'} "
            f"khi nới lịch."
        )

    def _is_year1_student(sid: str) -> bool:
        return is_year1_anchor_student(sid, student_cohort_codes, year1_anchor)

    def _prep_weight_for_student(sid: str) -> float:
        wave = cohort_wave_index(student_cohort_codes.get(str(sid), ""), year1_anchor)
        if wave == 0:
            return 3.0
        if wave < 1_000_000:
            return max(0.5, 2.5 - wave * 0.12)
        return 0.35

    def _is_anchor_wave_exam(idx: int) -> bool:
        return exam_wave[idx] == 0

    def _flatten_pending_by_wave(pending: List[int]) -> List[int]:
        """Sóng khóa trước; trong sóng gom theo 7 ký tự HP để ca tách cùng buổi."""
        buckets: Dict[int, List[int]] = defaultdict(list)
        for idx in pending:
            buckets[exam_wave[idx]].append(idx)
        out: List[int] = []
        for w in sorted(buckets.keys()):
            by_pfx: Dict[str, List[int]] = defaultdict(list)
            for idx in buckets[w]:
                pfx = str(exams[idx].course_prefix_7 or "").strip() or f"__{idx}"
                by_pfx[pfx].append(idx)
            pfx_keys = sorted(
                by_pfx.keys(),
                key=lambda p: (-sum(exams[i].size for i in by_pfx[p]), p),
            )
            for pfx in pfx_keys:
                sub = by_pfx[pfx]
                sub.sort(key=lambda i: (-exams[i].priority, -len(adj[i]), -exams[i].size, i))
                out.extend(sub)
        return out

    day_khoa_registry: Dict[Tuple[int, str], List[int]] = defaultdict(list)

    # --- Đặt sẵn fixed_slots trước ---
    for exam_id, slot in fixed_slots.items():
        if exam_id not in exam_index:
            continue
        idx = exam_index[exam_id]
        exam = exams[idx]
        slot = int(slot)
        day, _ = _slot_to_day_session(slot, window.sessions_per_day)
        kko = exam_khoa_keys[idx]
        conflict = False
        for k in kko:
            for oidx in day_khoa_registry.get((day, k), []):
                if not same_course_khoa_nhom_waiver(exam, exams[oidx]):
                    conflict = True
                    break
            if conflict:
                break
        if conflict:
            relaxations.append(
                f"Lịch cố định không tương thích Khoa_nhom (4 ký tự cuối MalopHP): bỏ ghim {exam_id}."
            )
            continue
        for sid in exam.student_ids:
            slot_used_students[slot].add(sid)
            day_exams_per_student[(sid, day)] += 1
            student_day_exams[sid][day].append(idx)
        slot_load_students[slot] += exam.size
        day_load_students[day] += exam.size
        assignment[exam_id] = slot
        if weekday_at_day_index(window, day) == 6:
            sunday_exam_days.add(day)
        if session_half and exam.course_prefix_7:
            sess = slot % window.sessions_per_day
            h = int(session_half[sess]) if sess < len(session_half) else 0
            pfx = exam.course_prefix_7
            if pfx not in prefix_half_anchor:
                prefix_half_anchor[pfx] = h
        for k in kko:
            day_khoa_registry[(day, k)].append(idx)

    # --- Sắp xếp môn theo sóng khóa (khóa mới / ≈năm 1 trước), rồi priority → degree → size ---
    remaining = [i for i, e in enumerate(exams) if e.exam_id not in assignment]
    remaining = _flatten_pending_by_wave(remaining)

    def _slots_for_exam(idx: int, relax_weekday: bool) -> List[int]:
        """Ô thời gian khả dĩ; nếu lọc thứ quá chặt → mở toàn bộ cửa sổ (đảm bảo có chỗ để đặt)."""
        exam = exams[idx]
        slots = enumerate_feasible_slots_for_exam(
            exam,
            window,
            allowed_sessions_by_exam_id,
            fixed_slots,
            weekend_large_min_students=weekend_large_course_min_students,
            prefix_totals=prefix_tot,
            relax_weekday_rule=False,
        )
        if not slots and relax_weekday:
            slots = enumerate_feasible_slots_for_exam(
                exam,
                window,
                allowed_sessions_by_exam_id,
                fixed_slots,
                weekend_large_min_students=weekend_large_course_min_students,
                prefix_totals=prefix_tot,
                relax_weekday_rule=True,
            )
        if not slots and relax_weekday:
            spd = window.sessions_per_day
            sessions = allowed_sessions_by_exam_id.get(
                exam.exam_id, list(range(spd))
            )
            sessions = sorted({int(s) for s in sessions if 0 <= int(s) < spd})
            if sessions:
                slots = [
                    d * spd + s
                    for d in range(window.total_days)
                    for s in sessions
                ]
        return slots

    def _try_place(
        idx: int,
        slot: int,
        allow_conflict: bool,
        ignore_capacity: bool,
        ignore_per_day: bool,
        ignore_credit_prep: bool = False,
        ignore_khoa_nhom: bool = False,
        ignore_prefix_anchor: bool = False,
        ignore_sunday_spread: bool = False,
    ) -> bool:
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
        # Ôn cứng: mọi SV khi chưa nới; nới chỉ bỏ ôn khóa cũ, giữ cứng cho năm 1.
        # Năm 1 được thi cùng ngày; giữa hai ngày khác nhau cần đủ ngày ôn theo tín chỉ.
        if min_prep_days > 0 or prep_day_per_credit > 0:
            for sid in exam.student_ids:
                y1 = _is_year1_student(sid)
                if ignore_credit_prep and not y1:
                    continue
                for d, oidx_list in student_day_exams[sid].items():
                    for oidx in oidx_list:
                        if oidx == idx:
                            continue
                        req_gap = prep_hard_gap_days_for_pair(
                            exam,
                            exams[oidx],
                            prep_day_per_credit,
                            min_prep_days,
                            year1_allow_same_day=year1_allow_same_day,
                            for_year1_student=y1,
                            same_calendar_day=(d == day),
                        )
                        if req_gap > 0 and abs(d - day) + 1e-9 < req_gap:
                            return False
        # Giãn khỏi CN (mọi SV khi chưa nới; nới chỉ bỏ với khóa sau)
        if sunday_exam_days and weekday_at_day_index(window, day) != 6:
            for sun_day in sunday_exam_days:
                if abs(day - sun_day) >= _SUNDAY_SPREAD_MIN:
                    continue
                for sid in exam.student_ids:
                    if ignore_sunday_spread and not _is_year1_student(sid):
                        continue
                    if sun_day in student_day_exams[sid]:
                        return False
        if not ignore_prefix_anchor and session_half and exam.course_prefix_7:
            sess = slot % window.sessions_per_day
            h = int(session_half[sess]) if sess < len(session_half) else 0
            ah = prefix_half_anchor.get(exam.course_prefix_7)
            if ah is not None and h != ah:
                return False
        if not ignore_khoa_nhom:
            keys = exam_khoa_keys[idx]
            if keys:
                for k in keys:
                    for oidx in day_khoa_registry.get((day, k), []):
                        if oidx == idx:
                            continue
                        if not same_course_khoa_nhom_waiver(exam, exams[oidx]):
                            return False
        return True

    def _exam_has_year1(idx: int) -> bool:
        return any(_is_year1_student(sid) for sid in exams[idx].student_ids)

    def _force_commit(idx: int, slot: int) -> None:
        """Đặt bắt buộc — không kiểm ràng buộc (chỉ dùng khi ca không có SV khóa năm 1)."""
        _commit(idx, int(slot))

    def _safe_force_place(
        idx: int,
        slot: int,
        *,
        ignore_khoa: bool,
        ignore_prefix: bool,
    ) -> bool:
        """Ép chỗ nhưng vẫn giữ ngày ôn cho SV khóa anchor (năm 1)."""
        tiers: List[Tuple[bool, bool, bool, bool, bool]] = [
            (False, False, False, False, False),
            (True, False, False, False, False),
            (True, True, False, False, False),
            (True, True, True, ignore_prefix, False),
            (True, True, True, ignore_prefix, True),
        ]
        for allow_conf, ign_cap, ign_day, ign_pfx, ign_sun in tiers:
            if _try_place(
                idx,
                int(slot),
                allow_conflict=allow_conf,
                ignore_capacity=ign_cap,
                ignore_per_day=ign_day,
                ignore_credit_prep=False,
                ignore_khoa_nhom=ignore_khoa,
                ignore_prefix_anchor=ign_pfx,
                ignore_sunday_spread=ign_sun,
            ):
                _commit(idx, int(slot))
                return True
        if not _exam_has_year1(idx):
            _force_commit(idx, int(slot))
            return True
        # Cực chót: vẫn xếp (ưu tiên cùng ngày / ít vi phạm nhất) — 100% môn bắt buộc
        if _try_place(
            idx,
            int(slot),
            allow_conflict=True,
            ignore_capacity=True,
            ignore_per_day=True,
            ignore_credit_prep=True,
            ignore_khoa_nhom=ignore_khoa,
            ignore_prefix_anchor=ignore_prefix,
            ignore_sunday_spread=True,
        ):
            _commit(idx, int(slot))
            return True
        _force_commit(idx, int(slot))
        return True

    def _commit(idx: int, slot: int) -> None:
        exam = exams[idx]
        day, sess = _slot_to_day_session(slot, window.sessions_per_day)
        if session_half and exam.course_prefix_7:
            h = int(session_half[sess]) if sess < len(session_half) else 0
            pfx = exam.course_prefix_7
            if pfx not in prefix_half_anchor:
                prefix_half_anchor[pfx] = h
        for sid in exam.student_ids:
            slot_used_students[slot].add(sid)
            day_exams_per_student[(sid, day)] += 1
            student_day_exams[sid][day].append(idx)
        slot_load_students[slot] += exam.size
        day_load_students[day] += exam.size
        assignment[exam.exam_id] = slot
        for k in exam_khoa_keys[idx]:
            day_khoa_registry[(day, k)].append(idx)
        if weekday_at_day_index(window, day) == 6:
            sunday_exam_days.add(day)

    def _uncommit(idx: int) -> Optional[int]:
        """Gỡ ca đã đặt (phục vụ sửa ôn / đổi slot)."""
        exam = exams[idx]
        eid = exam.exam_id
        slot = assignment.pop(eid, None)
        if slot is None:
            return None
        day, _ = _slot_to_day_session(slot, window.sessions_per_day)
        spd = window.sessions_per_day
        for sid in exam.student_ids:
            slot_used_students[slot].discard(sid)
            key = (sid, day)
            day_exams_per_student[key] -= 1
            if day_exams_per_student[key] <= 0:
                del day_exams_per_student[key]
            olist = student_day_exams[sid].get(day, [])
            student_day_exams[sid][day] = [i for i in olist if i != idx]
            if not student_day_exams[sid][day]:
                del student_day_exams[sid][day]
            if not student_day_exams[sid]:
                del student_day_exams[sid]
        slot_load_students[slot] -= exam.size
        day_load_students[day] -= exam.size
        for k in exam_khoa_keys[idx]:
            reg = day_khoa_registry.get((day, k), [])
            day_khoa_registry[(day, k)] = [i for i in reg if i != idx]
        if weekday_at_day_index(window, day) == 6:
            if not any(s // spd == day for s in assignment.values()):
                sunday_exam_days.discard(day)
        return int(slot)

    def _greedy_repair_prep(rounds: int = 2, year1_only: bool = False) -> int:
        """Thử chuyển ca sang slot khác để giảm điểm phạt ôn/CN (sau khi đã xếp hết)."""
        moved = 0
        fixed_set = set(fixed_slots.keys())
        for _ in range(rounds):
            candidates = [
                i
                for i in range(n)
                if exams[i].exam_id in assignment and exams[i].exam_id not in fixed_set
            ]
            if year1_only:
                candidates = [i for i in candidates if _exam_has_year1(i)]
            candidates.sort(
                key=lambda i: -_score_slot(i, assignment[exams[i].exam_id])
            )
            for idx in candidates:
                eid = exams[idx].exam_id
                old_slot = assignment[eid]
                old_pen = _score_slot(idx, old_slot)
                if _uncommit(idx) is None:
                    continue
                allowed = _slots_for_exam(idx, relax_weekday=True)
                best_slot = old_slot
                best_pen = old_pen
                for slot in allowed:
                    if not _try_place(
                        idx,
                        slot,
                        allow_conflict=False,
                        ignore_capacity=True,
                        ignore_per_day=True,
                        ignore_credit_prep=False,
                    ):
                        continue
                    pen = _score_slot(idx, slot)
                    if pen < best_pen - 1e-6:
                        best_pen = pen
                        best_slot = slot
                if best_slot != old_slot:
                    _commit(idx, best_slot)
                    moved += 1
                else:
                    _commit(idx, old_slot)
        return moved

    def _score_slot(idx: int, slot: int) -> float:
        """Càng thấp càng tốt. Tổng hợp 5 thành phần điểm:
        1. PBL push-late (ưu tiên ngày cuối cho môn priority)
        2. Load balance theo ngày & ca (chính – ép trải đều)
        3. Prep-day mềm (tránh thi liên tiếp cho SV trùng)
        4. Repair distance (giữ gần lịch cũ khi đổi thủ công)
        5. Sunday-spread: phạt môn có SV trùng với môn Chủ nhật nếu ngày quá gần CN
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

        # (3) Prep-day mềm — theo cặp môn (max tín chỉ), khớp ràng buộc cứng & báo cáo vi phạm.
        if prep_day_per_credit > 0:
            deficit_sq_sum = 0.0
            same_day_count = 0
            high_credit_pen = 0.0
            exam_wave_idx = exam_wave[idx]
            if year1_anchor <= 0 or exam_wave_idx >= 1_000_000:
                prep_w = 0.75
            elif exam_wave_idx == 0:
                prep_w = 1.0
            else:
                prep_w = max(0.75, 1.0 - exam_wave_idx * 0.06)
            sample = exam.student_ids if len(exam.student_ids) <= 300 else exam.student_ids[::max(1, len(exam.student_ids) // 300)]
            for sid in sample:
                sv_w = max(_prep_weight_for_student(sid), prep_w)
                for d, oidx_list in student_day_exams[sid].items():
                    for oidx in oidx_list:
                        if oidx == idx:
                            continue
                        other = exams[oidx]
                        req = prep_days_required_for_pair(
                            exam, other, prep_day_per_credit, min_prep_days
                        )
                        if req <= 0:
                            continue
                        diff = abs(d - day)
                        if diff < req:
                            deficit = req - diff
                            deficit_sq_sum += deficit * deficit * sv_w
                            if max(exam.credits, other.credits) >= 4.0:
                                high_credit_pen += deficit * deficit * 2.0 * sv_w
                            if diff == 0:
                                same_day_count += sv_w
            scale = len(exam.student_ids) / max(1, len(sample))
            spread_w = max(0.01, float(spread_prep_factor))
            score += deficit_sq_sum * scale * 0.20 * spread_w * prep_w
            score += high_credit_pen * scale * 0.12 * spread_w * prep_w
            score += same_day_count * scale * 2.0 * spread_w * prep_w

        # (4) Giữ gần base_slots khi repair
        prev = base_slots.get(exam.exam_id)
        if prev is not None:
            score += abs(prev - slot) * 0.001

        # (5) Sunday-spread: SV trùng môn thi CN → phạt mạnh nếu xếp < _SUNDAY_SPREAD_MIN ngày
        if sunday_exam_days and weekday_at_day_index(window, day) != 6:
            sample_sv = (
                exam.student_ids
                if len(exam.student_ids) <= 300
                else exam.student_ids[:: max(1, len(exam.student_ids) // 300)]
            )
            sunday_deficit_sq = 0.0
            for sun_day in sunday_exam_days:
                gap = abs(day - sun_day)
                if gap >= _SUNDAY_SPREAD_MIN:
                    continue
                deficit = _SUNDAY_SPREAD_MIN - gap
                for sid in sample_sv:
                    if sun_day in student_day_exams[sid]:
                        sunday_deficit_sq += deficit * deficit
            if sunday_deficit_sq > 0:
                scale_sv = len(exam.student_ids) / max(1, len(sample_sv))
                spread_w = max(0.01, float(spread_prep_factor))
                score += sunday_deficit_sq * scale_sv * _SUNDAY_SPREAD_WEIGHT * spread_w

        return score

    def _pick_force_slot(
        idx: int,
        allowed: List[int],
        *,
        ignore_khoa: bool,
        ignore_prefix: bool,
    ) -> Optional[int]:
        """Chọn slot ép cuối: ưu tiên còn ôn/CN cứng, rồi mới nới; luôn minimize _score_slot."""
        tiers: List[Tuple[bool, bool, bool]] = [
            (False, False, False),
            (False, True, False),
            (True, False, False),
            (True, True, False),
        ]
        if not ignore_prefix:
            tiers.append((True, True, True))
        for ignore_prep, ignore_sunday, ignore_pfx in tiers:
            best_slot: Optional[int] = None
            best_pen = float("inf")
            for slot in allowed:
                if not _try_place(
                    idx,
                    slot,
                    allow_conflict=True,
                    ignore_capacity=True,
                    ignore_per_day=True,
                    ignore_credit_prep=ignore_prep,
                    ignore_khoa_nhom=ignore_khoa,
                    ignore_prefix_anchor=ignore_pfx,
                    ignore_sunday_spread=ignore_sunday,
                ):
                    continue
                pen = _score_slot(idx, slot)
                overlap = sum(
                    1 for sid in exams[idx].student_ids if sid in slot_used_students[slot]
                )
                pen += overlap * 1000.0
                if pen < best_pen:
                    best_pen = pen
                    best_slot = slot
            if best_slot is not None:
                return best_slot
        if allowed:
            return min(allowed, key=lambda sl: _score_slot(idx, sl))
        return None

    def _place_with_policy(
        allow_conflict: bool,
        ignore_capacity: bool,
        ignore_per_day: bool,
        ignore_credit_prep: bool = False,
    ) -> List[int]:
        """Thử xếp lần lượt các môn còn lại; trả về list các môn vẫn không đặt được."""
        still_unplaced: List[int] = []
        ordered = _flatten_pending_by_wave(list(remaining_state["pending"]))
        for idx in ordered:
            exam = exams[idx]
            allowed = enumerate_feasible_slots_for_exam(
                exam,
                window,
                allowed_sessions_by_exam_id,
                fixed_slots,
                weekend_large_min_students=weekend_large_course_min_students,
                prefix_totals=prefix_tot,
                relax_weekday_rule=False,
            )
            if not allowed and weekend_large_course_min_students > 0:
                allowed = enumerate_feasible_slots_for_exam(
                    exam,
                    window,
                    allowed_sessions_by_exam_id,
                    fixed_slots,
                    weekend_large_min_students=weekend_large_course_min_students,
                    prefix_totals=prefix_tot,
                    relax_weekday_rule=True,
                )
                if allowed and not weekday_relax_logged:
                    relaxations.append(
                        "Không đủ ô thời gian theo quy tắc thứ trong tuần (T2–T7 / T7–CN cho môn đông): "
                        "tạm bỏ lọc ngày để vẫn có lịch."
                    )
                    weekday_relax_logged = True
            if not allowed:
                still_unplaced.append(idx)
                continue
            best_slot: Optional[int] = None
            best_score = float("inf")
            for slot in allowed:
                if not _try_place(
                    idx,
                    slot,
                    allow_conflict,
                    ignore_capacity,
                    ignore_per_day,
                    ignore_credit_prep=ignore_credit_prep,
                ):
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

    # 1) Thử strict (gồm ôn theo max tín chỉ cặp môn)
    leftover = _place_with_policy(
        allow_conflict=False,
        ignore_capacity=False,
        ignore_per_day=False,
        ignore_credit_prep=False,
    )
    remaining_state["pending"] = leftover

    # 2) Nới capacity (post-room sẽ kiểm sau)
    if leftover:
        relaxations.append("Bỏ qua giới hạn sức chứa tổng phòng ở pha greedy (sẽ phân phòng sau).")
        leftover = _place_with_policy(
            allow_conflict=False,
            ignore_capacity=True,
            ignore_per_day=False,
            ignore_credit_prep=False,
        )
        remaining_state["pending"] = leftover

    # 3) Nới max_exams_per_day
    if leftover:
        relaxations.append("Cho phép vượt giới hạn môn/SV/ngày để đảm bảo có lịch.")
        leftover = _place_with_policy(
            allow_conflict=False,
            ignore_capacity=True,
            ignore_per_day=True,
            ignore_credit_prep=False,
        )
        remaining_state["pending"] = leftover

    def _place_leftover_loop(
        indices: List[int],
        *,
        allow_conflict: bool,
        ignore_credit_prep: bool,
        ignore_khoa: bool,
        ignore_prefix: bool,
        ignore_sunday: bool = False,
        force_if_needed: bool,
    ) -> List[int]:
        still: List[int] = []
        for idx in indices:
            allowed = _slots_for_exam(idx, relax_weekday=True)
            if not allowed:
                still.append(idx)
                continue
            best_slot: Optional[int] = None
            best_pen = float("inf")
            for slot in allowed:
                if _try_place(
                    idx,
                    slot,
                    allow_conflict=allow_conflict,
                    ignore_capacity=True,
                    ignore_per_day=True,
                    ignore_credit_prep=ignore_credit_prep,
                    ignore_khoa_nhom=ignore_khoa,
                    ignore_prefix_anchor=ignore_prefix,
                    ignore_sunday_spread=ignore_sunday,
                ):
                    pen = _score_slot(idx, slot)
                    if allow_conflict:
                        overlap = sum(
                            1
                            for sid in exams[idx].student_ids
                            if sid in slot_used_students[slot]
                        )
                        pen += overlap * 1000.0
                    if pen < best_pen:
                        best_pen = pen
                        best_slot = slot
            if best_slot is not None:
                _commit(idx, best_slot)
            elif force_if_needed and allowed:
                forced = _pick_force_slot(
                    idx, allowed, ignore_khoa=ignore_khoa, ignore_prefix=ignore_prefix
                )
                if forced is not None:
                    _safe_force_place(
                        idx, forced, ignore_khoa=ignore_khoa, ignore_prefix=ignore_prefix
                    )
                else:
                    still.append(idx)
            else:
                still.append(idx)
        return still

    # 3b) Sóng khóa năm 1 còn lại: nới Khoa_nhom sớm (ôn chỉ áp khóa anchor)
    wave1_left = [i for i in leftover if _is_anchor_wave_exam(i)]
    if wave1_left:
        relaxations.append(
            f"Ưu tiên khóa {year1_anchor:02d}: nới Khoa_nhom cho {len(wave1_left)} môn sóng đầu "
            f"(ôn chỉ cứng với SV khóa {year1_anchor:02d})."
        )
        _place_leftover_loop(
            wave1_left,
            allow_conflict=False,
            ignore_credit_prep=False,
            ignore_khoa=True,
            ignore_prefix=False,
            ignore_sunday=False,
            force_if_needed=False,
        )
        leftover = [i for i in leftover if exams[i].exam_id not in assignment]

    # 4) Trùng SV cùng ca — ôn chỉ khóa anchor; giữ Khoa_nhom
    if leftover:
        relaxations.append(
            "Cho phép trùng SV cùng ca — ôn cứng chỉ SV khóa năm 1; vẫn giữ Khoa_nhom."
        )
        leftover = _place_leftover_loop(
            list(leftover),
            allow_conflict=True,
            ignore_credit_prep=False,
            ignore_khoa=False,
            ignore_prefix=False,
            ignore_sunday=False,
            force_if_needed=False,
        )

    # 5) Nới Khoa_nhom + buổi HP (ca tách) — ôn chỉ khóa anchor
    if leftover:
        relaxations.append(
            f"Nới Khoa_nhom & buổi HP cho {len(leftover)} môn — khóa sau có thể vi phạm ôn khi cần."
        )
        leftover = _place_leftover_loop(
            list(leftover),
            allow_conflict=True,
            ignore_credit_prep=False,
            ignore_khoa=True,
            ignore_prefix=True,
            ignore_sunday=False,
            force_if_needed=False,
        )

    # 5b) Nới ôn cho khóa cũ — SV năm 1 vẫn giữ ngày ôn (cùng ngày được)
    if leftover:
        relaxations.append(
            f"Nới ngày ôn cho khóa sau ({len(leftover)} môn) — SV khóa {year1_anchor:02d} vẫn giữ ôn."
        )
        leftover = _place_leftover_loop(
            list(leftover),
            allow_conflict=True,
            ignore_credit_prep=True,
            ignore_khoa=True,
            ignore_prefix=True,
            ignore_sunday=False,
            force_if_needed=False,
        )

    # 6) Bắt buộc 100% — nới giãn CN; vẫn giữ ôn khóa anchor (cùng ngày được)
    if leftover:
        n_force = len(leftover)
        relaxations.append(
            f"Đặt bắt buộc {n_force} môn còn lại — ưu tiên slot khả thi cho SV khóa {year1_anchor:02d}."
        )
        weekday_relax_logged = True
        leftover = _place_leftover_loop(
            list(leftover),
            allow_conflict=True,
            ignore_credit_prep=False,
            ignore_khoa=True,
            ignore_prefix=True,
            ignore_sunday=True,
            force_if_needed=True,
        )

    # 7) Hiếm: không có ô ca hợp lệ (thiếu cấu hình ca) — gán slot 0
    if leftover:
        relaxations.append(
            f"Gán khẩn cấp {len(leftover)} môn vào slot mặc định (kiểm tra cấu hình ca thi / kế hoạch ngày)."
        )
        for idx in leftover:
            eid = exams[idx].exam_id
            allowed_em = _slots_for_exam(idx, relax_weekday=True)
            if allowed_em:
                forced = _pick_force_slot(
                    idx, allowed_em, ignore_khoa=True, ignore_prefix=True
                )
                slot_guess = int(forced) if forced is not None else int(allowed_em[0])
            else:
                slot_guess = int(fixed_slots[eid]) if eid in fixed_slots else 0
            _safe_force_place(idx, slot_guess, ignore_khoa=True, ignore_prefix=True)

    repaired = _greedy_repair_prep(rounds=4) + _greedy_repair_prep(rounds=4, year1_only=True)
    if repaired > 0:
        relaxations.append(
            f"Sửa sau greedy: đã chuyển {repaired} ca để giảm vi phạm ôn/CN (giữ cùng buổi HP)."
        )

    unplaced = [e.exam_id for e in exams if e.exam_id not in assignment]
    unplaced_diag = diagnose_unplaced_exams(
        unplaced,
        exams,
        assignment,
        window,
        allowed_sessions_by_exam_id,
        session_half=session_half,
        fixed_slots=fixed_slots,
        max_exams_per_day=max_exams_per_day,
        min_prep_days=min_prep_days,
        prep_day_per_credit=prep_day_per_credit,
        total_capacity=total_capacity,
        weekend_large_course_min_students=weekend_large_course_min_students,
        prefix_student_totals=prefix_tot,
        student_cohort_codes=student_cohort_codes,
        year1_cohort_anchor=year1_cohort_anchor,
        year1_allow_same_day=year1_allow_same_day,
    )
    if unplaced_diag:
        relaxations.append(
            f"CẢNH BÁO NGHIÊM TRỌNG: {len(unplaced)} môn KHÔNG có lịch thi — xem bảng «Môn chưa xếp»."
        )
    return HeuristicResult(
        assignment=assignment,
        unplaced=unplaced,
        relaxations=relaxations,
        unplaced_diagnostics=unplaced_diag,
    )


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
    soft_slot_cap: int | None = None,
    fixed_slots: Dict[str, int] | None = None,
    session_half: List[int] | None = None,
    progress_cb=None,
    weekend_large_course_min_students: int = 0,
    prefix_student_totals: Dict[str, int] | None = None,
    student_cohort: Dict[str, int] | None = None,
    student_cohort_codes: Dict[str, str] | None = None,
    year1_cohort_anchor: int = 0,
    year1_allow_same_day: bool = True,
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

    prefix_tot: Dict[str, int] = dict(prefix_student_totals or {})
    if weekend_large_course_min_students > 0 and not prefix_tot:
        prefix_tot = build_prefix_student_totals(exams)
    if not student_cohort_codes:
        student_cohort_codes = build_student_cohort_code_map(
            exams, year1_anchor=year1_cohort_anchor
        )
    if not student_cohort:
        student_cohort = build_student_cohort_map(
            exams, student_cohort_codes=student_cohort_codes, year1_anchor=year1_cohort_anchor
        )
    lns_year1_anchor = resolve_year1_cohort_anchor(
        year1_cohort_anchor, exams, student_cohort_codes
    )

    def _lns_is_year1(sid: str) -> bool:
        return is_year1_anchor_student(sid, student_cohort_codes, lns_year1_anchor)

    keys_by_eid = {e.exam_id: exam_khoa_nhom_keys(e) for e in exams}

    def _compute_violation_score(asgn: Dict[str, int]) -> Tuple[int, int, Dict[str, float]]:
        """Trả về (total_count, same_day_count, per_exam_severity).

        • total_count: tổng số cặp vi phạm (giữ semantics cũ để báo cáo).
        • same_day_count: số cặp vi phạm với gap=0 (chỉ số người dùng quan tâm).
        • per_exam_severity: điểm severity gán cho từng exam (dùng để ưu tiên candidate
          khi LNS chọn môn để dịch chuyển). Severity = base count + α·deficit + β·1[gap=0].
        """
        exam_day: Dict[str, int] = {
            eid: slot // window.sessions_per_day for eid, slot in asgn.items()
        }
        per_student: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
        for eid, day in exam_day.items():
            if eid not in exam_index:
                continue
            exam = exams[exam_index[eid]]
            for sid in exam.student_ids:
                per_student[sid].append((day, eid))
        per_exam_sev: Dict[str, float] = defaultdict(float)
        total = 0
        same_day = 0
        for sid, entries in per_student.items():
            entries.sort()
            for i in range(1, len(entries)):
                prev_day, prev_eid = entries[i - 1]
                curr_day, curr_eid = entries[i]
                prev_exam = exams[exam_index[prev_eid]]
                curr_exam = exams[exam_index[curr_eid]]
                req = prep_days_required_for_pair(
                    prev_exam,
                    curr_exam,
                    prep_day_per_credit,
                    min_prep_days,
                )
                gap = curr_day - prev_day
                if gap + 1e-9 < req:
                    total += 1
                    deficit = req - gap
                    sev = 1.0 + 0.3 * deficit
                    if gap == 0:
                        same_day += 1
                        sev += 2.0
                    per_exam_sev[curr_eid] += sev
                    per_exam_sev[prev_eid] += sev
        return total, same_day, per_exam_sev

    base_vio, base_same_day, _ = _compute_violation_score(current)
    logs.append(
        f"Cải tiến LNS: vi phạm ngày ôn ban đầu = {base_vio:,} "
        f"(trong đó cùng-ngày / 0 ngày ôn = {base_same_day:,})"
    )

    # Move-based local search: với mỗi môn vi phạm nhiều, thử di chuyển sang slot tốt hơn.
    # Đánh giá delta = vi phạm prep & xung đột cứng (no-overlap) & max-day.
    # Cấu trúc lưu trữ tăng cường để O(1) check.

    sessions_per_day = window.sessions_per_day

    def _half_of(slot: int) -> int:
        sess = slot % sessions_per_day
        if session_half and 0 <= sess < len(session_half):
            return int(session_half[sess])
        return 0

    def _prefix_feasible(eid: str, slot: int) -> bool:
        """Cùng 7 ký tự HP → cùng buổi (sáng/chiều); được khác ngày."""
        ex = exams[exam_index[eid]]
        pfx = ex.course_prefix_7
        if not session_half or not pfx:
            return True
        h = _half_of(slot)
        for e2 in current:
            if e2 == eid:
                continue
            if exams[exam_index[e2]].course_prefix_7 != pfx:
                continue
            if _half_of(current[e2]) != h:
                return False
        return True

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

    def _vio_for_exam(eid: str, slot: int) -> float:
        """Severity-weighted điểm vi phạm prep liên quan đến exam này (nếu đặt ở slot).

        Quy ước: với cặp (X, Y), môn "sau" là người sort lớn hơn theo (day, eid) — giống
        `_compute_violation_score` để giữ nhất quán. Threshold dùng req của môn sau, đúng
        ý nghĩa "môn sau cần ngày ôn trước nó".

        Điểm = 1.0 (base count) + 0.3·deficit + 2.0·1[gap==0]. Dùng cho LNS so sánh delta
        khi di chuyển; vì cả 2 nhánh (X là môn trước / X là môn sau) đều được xét, LNS sẽ
        nhận ra cơ hội dịch chuyển X kể cả khi X là môn trước trong cặp vi phạm.
        """
        if eid not in exam_index:
            return 0.0
        exam_x = exams[exam_index[eid]]
        day_x = slot // sessions_per_day
        sev = 0.0
        for sid in exam_x.student_ids:
            for other_eid in student_to_exams[sid]:
                if other_eid == eid:
                    continue
                other_slot = current.get(other_eid)
                if other_slot is None:
                    continue
                day_y = other_slot // sessions_per_day
                other_exam = exams[exam_index[other_eid]]
                threshold = prep_days_required_for_pair(
                    exam_x, other_exam, prep_day_per_credit, min_prep_days
                )
                if threshold <= 0:
                    continue
                gap = abs(day_x - day_y)
                if gap + 1e-9 < threshold:
                    deficit = threshold - gap
                    sev += 1.0 + 0.3 * deficit
                    if gap == 0:
                        sev += 2.0
        # Phạt giãn khỏi môn thi Chủ nhật (SV trùng) — khớp greedy Sunday-spread
        if weekday_at_day_index(window, day_x) != 6:
            for sid in exam_x.student_ids:
                for other_eid in student_to_exams[sid]:
                    if other_eid == eid:
                        continue
                    other_slot = current.get(other_eid)
                    if other_slot is None:
                        continue
                    other_day = other_slot // sessions_per_day
                    if weekday_at_day_index(window, other_day) != 6:
                        continue
                    gap_cn = abs(day_x - other_day)
                    if gap_cn < 3:
                        deficit_cn = 3 - gap_cn
                        sev += deficit_cn * deficit_cn * 2.5
        return sev

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

    def _prep_feasible(eid: str, new_slot: int) -> bool:
        """LNS: giữ ngày ôn (năm 1 được cùng ngày nếu bật year1_allow_same_day)."""
        if prep_day_per_credit <= 0 and min_prep_days <= 0:
            return True
        exam = exams[exam_index[eid]]
        new_day = new_slot // sessions_per_day
        for sid in exam.student_ids:
            y1 = _lns_is_year1(sid)
            for other_eid in student_to_exams[sid]:
                if other_eid == eid:
                    continue
                other_slot = current.get(other_eid)
                if other_slot is None:
                    continue
                other_day = other_slot // sessions_per_day
                other_exam = exams[exam_index[other_eid]]
                req_gap = prep_hard_gap_days_for_pair(
                    exam,
                    other_exam,
                    prep_day_per_credit,
                    min_prep_days,
                    year1_allow_same_day=year1_allow_same_day,
                    for_year1_student=y1,
                    same_calendar_day=(new_day == other_day),
                )
                if req_gap > 0 and abs(new_day - other_day) + 1e-9 < req_gap:
                    return False
        return True

    def _khoa_feasible(eid: str, slot: int) -> bool:
        keys = keys_by_eid.get(eid, frozenset())
        if not keys:
            return True
        ex = exams[exam_index[eid]]
        new_day = slot // sessions_per_day
        for other_eid, oslot in current.items():
            if other_eid == eid:
                continue
            if oslot // sessions_per_day != new_day:
                continue
            if same_course_khoa_nhom_waiver(ex, exams[exam_index[other_eid]]):
                continue
            if keys & keys_by_eid.get(other_eid, frozenset()):
                return False
        return True

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

    # Pre-compute allowed slots per exam (thứ trong tuần: T2–T7 / T7–CN cho môn đông)
    allowed_by_exam: Dict[str, List[int]] = {}
    for eid in current.keys():
        if eid not in exam_index:
            continue
        ex = exams[exam_index[eid]]
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
        allowed_by_exam[eid] = slots

    # Slot load tracking (SV per slot) — để LNS biết peak nào đang đông
    slot_size_load: Dict[int, int] = defaultdict(int)
    for eid, slot in current.items():
        if eid in exam_index:
            slot_size_load[slot] += exams[exam_index[eid]].size

    PEAK_WEIGHT = 0.001  # mỗi SV vượt cap đáng giá 0.001 vi phạm

    def _same_day_and_sev_for_exam(eid: str, slot: int) -> Tuple[int, float]:
        """Trả về (số cặp same-day liên quan tới eid, severity tổng) khi đặt eid ở slot.
        Dùng cho pass cuối — tách rõ chỉ số same-day để ưu tiên giảm trước."""
        if eid not in exam_index:
            return 0, 0.0
        exam_x = exams[exam_index[eid]]
        day_x = slot // sessions_per_day
        sd_count = 0
        sev = 0.0
        for sid in exam_x.student_ids:
            for other_eid in student_to_exams[sid]:
                if other_eid == eid:
                    continue
                other_slot = current.get(other_eid)
                if other_slot is None:
                    continue
                day_y = other_slot // sessions_per_day
                other_exam = exams[exam_index[other_eid]]
                threshold = prep_days_required_for_pair(
                    exam_x, other_exam, prep_day_per_credit, min_prep_days
                )
                if threshold <= 0:
                    continue
                gap = abs(day_x - day_y)
                if gap + 1e-9 < threshold:
                    deficit = threshold - gap
                    sev += 1.0 + 0.3 * deficit
                    if gap == 0:
                        sd_count += 1
                        sev += 2.0
        return sd_count, sev

    def _collect_same_day_exam_ids() -> List[str]:
        """Liệt kê (không trùng) các exam_id đang có ≥1 cặp same-day vi phạm. Dùng cho pass cuối."""
        result: List[str] = []
        seen: set[str] = set()
        for sid, eids in student_to_exams.items():
            if len(eids) < 2:
                continue
            entries = sorted(
                ((current[e] // sessions_per_day, e) for e in eids if e in current),
                key=lambda x: (x[0], x[1]),
            )
            for i in range(1, len(entries)):
                d_prev, e_prev = entries[i - 1]
                d_curr, e_curr = entries[i]
                if d_prev != d_curr:
                    continue
                req_curr = prep_days_required_for_pair(
                    exams[exam_index[e_prev]],
                    exams[exam_index[e_curr]],
                    prep_day_per_credit,
                    min_prep_days,
                )
                if req_curr <= 0:
                    continue
                if e_prev not in seen:
                    seen.add(e_prev)
                    result.append(e_prev)
                if e_curr not in seen:
                    seen.add(e_curr)
                    result.append(e_curr)
        return result

    no_improve_count = 0
    for it in range(iterations):
        if progress_cb:
            progress_cb(
                int(50 + (it / max(1, iterations)) * 20),
                f"Bước 2/3: vòng cải tiến LNS thứ {it + 1}/{iterations}…",
            )
        total_vio, same_day_vio, per_exam_sev = _compute_violation_score(current)
        if total_vio == 0:
            break
        # Sắp xếp ứng viên theo severity (nặng nhất trước) — môn liên quan đến same-day
        # sẽ tự động bubble lên vì severity của các cặp gap=0 cao hơn ~3x cặp gap=1.
        ranked = sorted(
            (
                (eid, v)
                for eid, v in per_exam_sev.items()
                if eid not in fixed_slots
            ),
            key=lambda x: -x[1],
        )[:pool_size]
        moved = 0
        before = total_vio
        before_same_day = same_day_vio
        for eid, _ in ranked:
            old_slot = current[eid]
            cur_vio = _vio_for_exam(eid, old_slot)
            cur_peak = _peak_cost(eid, old_slot)
            if cur_vio <= 0 and cur_peak <= 0:
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
                if not _khoa_feasible(eid, slot):
                    continue
                if not _prefix_feasible(eid, slot):
                    continue
                if not _prep_feasible(eid, slot):
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
        after, after_same_day, _ = _compute_violation_score(current)
        improved = after < before or after_same_day < before_same_day
        if improved:
            logs.append(
                f"Vòng LNS thứ {it + 1}: vi phạm {before:,} → {after:,} "
                f"(cùng-ngày {before_same_day:,} → {after_same_day:,}, đã chuyển {moved} môn)"
            )
            base_vio = after
            no_improve_count = 0
        else:
            logs.append(
                f"Vòng LNS thứ {it + 1}: vi phạm {before:,} (cùng-ngày {before_same_day:,}) "
                f"— không cải thiện, đã thử chuyển {moved} môn."
            )
            no_improve_count += 1
            if no_improve_count >= 2:
                logs.append("Dừng cải tiến LNS chính sớm: hai vòng liên tiếp không cải thiện.")
                break

    # ---- Pass cuối: dedicated same-day breaker ----
    # Bounded cost (chỉ duyệt exam còn dính same-day). Chấp nhận move khi giảm cùng-ngày
    # hoặc giữ nguyên cùng-ngày nhưng giảm severity tổng. Đây không phải tinh chỉnh dữ liệu
    # cụ thể — đó là cấu trúc thuật toán: tách giai đoạn tổng quát (LNS chính) và giai đoạn
    # targeted (same-day) để tránh stuck ở local optimum nhẹ.
    _, same_day_left, _ = _compute_violation_score(current)
    if same_day_left > 0:
        if progress_cb:
            progress_cb(
                72,
                f"Bước 2/3: pass cuối — đang phá {same_day_left:,} cặp cùng-ngày…",
            )
        same_day_targets = _collect_same_day_exam_ids()
        same_day_targets = [eid for eid in same_day_targets if eid not in fixed_slots]
        moved_sd = 0
        for eid in same_day_targets:
            old_slot = current[eid]
            cur_sd, cur_sev = _same_day_and_sev_for_exam(eid, old_slot)
            if cur_sd <= 0:
                continue
            cur_peak = _peak_cost(eid, old_slot)
            best_slot = old_slot
            best_pair = (cur_sd, cur_sev + cur_peak)
            for slot in allowed_by_exam[eid]:
                if slot == old_slot:
                    continue
                if _conflict_at_slot(eid, slot):
                    continue
                if _violates_max_per_day(eid, slot):
                    continue
                if not _khoa_feasible(eid, slot):
                    continue
                if not _prefix_feasible(eid, slot):
                    continue
                if not _prep_feasible(eid, slot):
                    continue
                new_sd, new_sev = _same_day_and_sev_for_exam(eid, slot)
                new_peak = _peak_cost(eid, slot)
                pair = (new_sd, new_sev + new_peak)
                # Ưu tiên giảm same_day trước, rồi mới đến severity tổng.
                if pair < best_pair:
                    best_pair = pair
                    best_slot = slot
            if best_slot != old_slot:
                _move(eid, best_slot)
                moved_sd += 1
        after_total, after_sd, _ = _compute_violation_score(current)
        logs.append(
            f"Pass cuối (same-day): cùng-ngày {same_day_left:,} → {after_sd:,} "
            f"(tổng vi phạm hiện {after_total:,}, đã chuyển {moved_sd} môn)"
        )

    # ---- Pass sửa prep (nhanh): dùng điểm cục bộ / môn — không quét lại toàn bộ SV mỗi ô thử ----
    prep_rounds = max(3, min(8, iterations * 2))
    prep_pool = min(pool_size, 80 if base_vio > 2500 else pool_size)
    prep_slot_cap = 40
    for pr in range(prep_rounds):
        total_vio, same_day_vio, per_exam_sev = _compute_violation_score(current)
        if total_vio == 0:
            break
        if progress_cb:
            progress_cb(
                73,
                f"Bước 2/3: pass sửa prep {pr + 1}/{prep_rounds} "
                f"({total_vio:,} vi phạm, cùng-ngày {same_day_vio:,})…",
            )
        ranked = sorted(
            ((eid, v) for eid, v in per_exam_sev.items() if eid not in fixed_slots),
            key=lambda x: -x[1],
        )[:prep_pool]
        moved_prep = 0
        before_total = total_vio
        before_sd = same_day_vio
        for eid, _ in ranked:
            old_slot = current[eid]
            cur_vio = _vio_for_exam(eid, old_slot)
            cur_sd, cur_sev = _same_day_and_sev_for_exam(eid, old_slot)
            cur_peak = _peak_cost(eid, old_slot)
            cur_score = cur_vio + cur_peak + cur_sev
            if cur_vio <= 0 and cur_sd <= 0 and cur_peak <= 0:
                continue
            best_slot = old_slot
            best_pair = (cur_sd, cur_score)
            candidates = [
                s
                for s in allowed_by_exam[eid]
                if s != old_slot
                and not _conflict_at_slot(eid, s)
                and not _violates_max_per_day(eid, s)
                and _khoa_feasible(eid, s)
                and _prefix_feasible(eid, s)
                and _prep_feasible(eid, s)
            ]
            if len(candidates) > prep_slot_cap:
                candidates.sort(key=lambda sl: _vio_for_exam(eid, sl))
                candidates = candidates[:prep_slot_cap]
            for slot in candidates:
                new_sd, new_sev = _same_day_and_sev_for_exam(eid, slot)
                new_s = _vio_for_exam(eid, slot) + _peak_cost(eid, slot) + new_sev
                pair = (new_sd, new_s)
                if pair < best_pair:
                    best_pair = pair
                    best_slot = slot
            if best_slot != old_slot:
                _move(eid, best_slot)
                moved_prep += 1
        after_total, after_sd, _ = _compute_violation_score(current)
        if after_total < before_total or after_sd < before_sd:
            logs.append(
                f"Pass sửa prep {pr + 1}: vi phạm {before_total:,} → {after_total:,} "
                f"(cùng-ngày {before_sd:,} → {after_sd:,}, đã chuyển {moved_prep} môn)"
            )
        elif moved_prep == 0:
            logs.append(
                f"Dừng pass sửa prep sau vòng {pr + 1}: không cải thiện thêm."
            )
            break

    # Pass nhẹ: phá cặp cùng-ngày có SV khóa anchor (nguyên nhân actual=0).
    if lns_year1_anchor > 0:
        y1_sd_targets = [
            eid
            for eid in _collect_same_day_exam_ids()
            if eid not in fixed_slots
            and eid in exam_index
            and any(_lns_is_year1(sid) for sid in exams[exam_index[eid]].student_ids)
        ]
        moved_y1_sd = 0
        for eid in y1_sd_targets[:prep_pool]:
            old_slot = current[eid]
            cur_sd, cur_sev = _same_day_and_sev_for_exam(eid, old_slot)
            if cur_sd <= 0:
                continue
            best_slot = old_slot
            best_pair = (cur_sd, cur_sev)
            for slot in allowed_by_exam.get(eid, []):
                if slot == old_slot:
                    continue
                if _conflict_at_slot(eid, slot):
                    continue
                if _violates_max_per_day(eid, slot):
                    continue
                if not _khoa_feasible(eid, slot):
                    continue
                if not _prefix_feasible(eid, slot):
                    continue
                if not _prep_feasible(eid, slot):
                    continue
                new_sd, new_sev = _same_day_and_sev_for_exam(eid, slot)
                pair = (new_sd, new_sev)
                if pair < best_pair:
                    best_pair = pair
                    best_slot = slot
            if best_slot != old_slot:
                _move(eid, best_slot)
                moved_y1_sd += 1
        if moved_y1_sd > 0:
            logs.append(
                f"Pass khóa {lns_year1_anchor:02d} (cùng-ngày): đã chuyển {moved_y1_sd} môn "
                f"liên quan SV năm 1."
            )

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
