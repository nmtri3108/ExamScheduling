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
from typing import Dict, List, Optional, Tuple

from .diagnostics import (
    build_conflict_index,
    build_prefix_student_totals,
    diagnose_unplaced_exams,
    enumerate_feasible_slots_for_exam,
    exam_allowed_day_indices,
    build_student_cohort_code_map,
    build_student_cohort_map,
    cohort_wave_index,
    exam_khoa_nhom_keys,
    exam_min_cohort_wave,
    is_year1_anchor_student,
    resolve_year1_cohort_anchor,
    prep_days_required_for_pair,
    min_prep_index_gap_between,
    prep_gap_violated,
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
    balance_weight: float = 0.12,
    soft_slot_cap: int | None = None,
    session_half: List[int] | None = None,
    weekend_large_course_min_students: int = 0,
    prefix_student_totals: Dict[str, int] | None = None,
    spread_prep_factor: float = 2.4,
    student_cohort: Dict[str, int] | None = None,
    student_cohort_codes: Dict[str, str] | None = None,
    year1_cohort_anchor: int = 0,
    year1_allow_same_day: bool = True,
    preferred_session_by_prefix7: Dict[str, int] | None = None,
    weekday_session_bonus: Dict[Tuple[int, int], float] | None = None,
    pattern_weight: float = 1.0,
    max_rooms_per_slot_per_format: int = 50,
    estimated_students_per_room_by_exam_format: Dict[int, float] | None = None,
) -> HeuristicResult:
    """Trả về HeuristicResult với map exam_id -> slot.

    Greedy không bao giờ raise: nới lần lượt capacity → max_exams_per_day → ôn theo tín chỉ
    → xung đột SV tối thiểu; ghi nhận trong `relaxations`.
    """
    allowed_sessions_by_exam_id = allowed_sessions_by_exam_id or {}
    fixed_slots = dict(fixed_slots or {})
    base_slots = dict(base_slots or {})
    preferred_session_by_prefix7 = preferred_session_by_prefix7 or {}
    weekday_session_bonus = weekday_session_bonus or {}
    estimated_students_per_room_by_exam_format = (
        estimated_students_per_room_by_exam_format or {1: 40.0, 2: 35.0, 3: 28.0}
    )

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
    slot_room_demand_by_format: Dict[Tuple[int, int], float] = defaultdict(float)

    def _exam_format_code(idx: int) -> int:
        try:
            return int(getattr(exams[idx], "exam_format", 1) or 1)
        except (TypeError, ValueError):
            return 1

    def _estimated_rooms_for_exam(idx: int) -> float:
        exam = exams[idx]
        fmt = _exam_format_code(idx)
        if fmt == 3:
            return 1.0
        per_room = float(estimated_students_per_room_by_exam_format.get(fmt, 36.0) or 36.0)
        per_room = max(1.0, per_room)
        est = max(1.0, float(exam.size) / per_room)
        # Biên an toàn để tránh under-estimate số phòng thực tế (đặc biệt lý thuyết/máy).
        if fmt in (1, 2):
            est *= 1.08
        return est

    # Mục tiêu phân bố đều: tổng SV / số ngày (giúp scoring biết khi nào "quá tải")
    total_student_demand = sum(e.size for e in exams)
    target_per_slot = total_student_demand / max(1, window.total_slots)

    relaxations: List[str] = []
    if window.has_per_cohort_windows:
        n_win = len({(a, b) for a, b in window.khoa_lop_windows.values()})
        relaxations.append(
            f"Kế hoạch thi theo Khoa_lop* (4 ký tự cuối MalopHP): {len(window.khoa_lop_windows)} lớp, "
            f"{n_win} đợt ngày khác nhau; mỗi ca chỉ xếp trong giao các đợt của lớp tham gia."
        )
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

    def _max_exams_per_day_for_student(sid: str) -> int:
        """Xếp tay: ~99% SV chỉ 1 môn/ngày khi bật ôn — không nới 2 môn/ngày trong greedy."""
        cap = int(max_exams_per_day or 0)
        if cap <= 0:
            return 0
        if prep_day_per_credit <= 0 and min_prep_days <= 0:
            return cap
        return 1

    def _prep_weight_for_student(sid: str) -> float:
        wave = cohort_wave_index(student_cohort_codes.get(str(sid), ""), year1_anchor)
        if wave == 0:
            return 4.5
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
        fmt = _exam_format_code(idx)
        slot_room_demand_by_format[(slot, fmt)] += _estimated_rooms_for_exam(idx)
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
        pfx = str(exam.course_prefix_7 or "").strip()
        large_n = int(prefix_tot.get(pfx, exam.size) if pfx else exam.size)
        is_very_large = (
            weekend_large_course_min_students > 0
            and (large_n >= int(weekend_large_course_min_students) or int(exam.size) >= int(weekend_large_course_min_students))
        )
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
            plan_days = exam_allowed_day_indices(exam, window)
            # Môn rất đông vẫn giữ nguyên lọc cuối tuần, không mở rộng ngày thường ở bước fallback cuối.
            if sessions and plan_days and not is_very_large:
                slots = [
                    d * spd + s for d in sorted(plan_days) for s in sessions
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
        # max_exams_per_day (khóa sau: thường chỉ 1 môn/ngày để giữ ngày ôn)
        if not ignore_per_day and max_exams_per_day > 0:
            for sid in exam.student_ids:
                lim = _max_exams_per_day_for_student(sid)
                if lim > 0 and day_exams_per_student[(sid, day)] >= lim:
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
                        if prep_gap_violated(
                            abs(d - day),
                            exam,
                            exams[oidx],
                            prep_day_per_credit,
                            min_prep_days,
                            year1_allow_same_day=year1_allow_same_day,
                            for_year1_student=y1,
                            same_calendar_day=(d == day),
                        ):
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
        day, _ = _slot_to_day_session(int(slot), window.sessions_per_day)
        plan_days = exam_allowed_day_indices(exams[idx], window)
        if plan_days and day not in plan_days:
            return False
        if _try_place(
            idx,
            int(slot),
            allow_conflict=False,
            ignore_capacity=True,
            ignore_per_day=False,
            ignore_credit_prep=False,
            ignore_khoa_nhom=ignore_khoa,
            ignore_prefix_anchor=ignore_prefix,
            ignore_sunday_spread=True,
        ):
            _commit(idx, int(slot))
            return True
        if plan_days and day in plan_days:
            if not _exam_has_year1(idx):
                _force_commit(idx, int(slot))
                return exams[idx].exam_id in assignment
        return False

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
        fmt = _exam_format_code(idx)
        slot_room_demand_by_format[(slot, fmt)] += _estimated_rooms_for_exam(idx)
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
        fmt = _exam_format_code(idx)
        slot_room_demand_by_format[(slot, fmt)] -= _estimated_rooms_for_exam(idx)
        if slot_room_demand_by_format[(slot, fmt)] <= 1e-9:
            slot_room_demand_by_format.pop((slot, fmt), None)
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
                for ign_khoa in (False, True):
                    for slot in allowed:
                        if not _try_place(
                            idx,
                            slot,
                            allow_conflict=False,
                            ignore_capacity=True,
                            ignore_per_day=False,
                            ignore_credit_prep=False,
                            ignore_khoa_nhom=ign_khoa,
                        ):
                            continue
                        pen = _score_slot(idx, slot)
                        if pen < best_pen - 1e-6:
                            best_pen = pen
                            best_slot = slot
                    if best_slot != old_slot:
                        break
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

        # (2) Load-balance — phạt theo tỉ lệ vượt mục tiêu trong phạm vi đợt Khoa_lop*.
        projected_day = day_load_students[day] + exam.size
        projected_slot = slot_load_students[slot] + exam.size
        allowed_days = exam_allowed_day_indices(exam, window)
        eff_day_count = len(allowed_days) if allowed_days else window.total_days
        local_target_day = total_student_demand / max(1, eff_day_count)
        day_ratio = projected_day / max(1.0, local_target_day)
        slot_ratio = projected_slot / max(1.0, target_per_slot)
        # Chỉ phạt ngày/ca quá tải — không phạt ngày còn trống (tránh dồn môn gây vi phạm ôn).
        balance_pen = 0.0
        if day_ratio > 1.0:
            balance_pen += (day_ratio - 1.0) ** 2 * 100.0
        elif eff_day_count > 1 and day_ratio < 0.82:
            # Thưởng nhẹ khi dùng ngày còn trống để dàn lịch, giảm vi phạm ôn.
            balance_pen -= (0.82 - day_ratio) ** 2 * 50.0
        if slot_ratio > 1.0:
            balance_pen += (slot_ratio - 1.0) ** 2 * 50.0
        elif projected_slot <= max(1.0, target_per_slot * 0.75):
            # Ưu tiên mở thêm slot khi còn thưa để giảm dồn ca.
            balance_pen -= ((max(1.0, target_per_slot * 0.75) - projected_slot) / max(1.0, target_per_slot)) * 14.0
        if slot_load_students[slot] <= 0:
            # Bonus nhỏ cho slot hoàn toàn mới.
            balance_pen -= 3.0

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
                prep_w = 2.5
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
                        diff = abs(d - day)
                        need_idx = min_prep_index_gap_between(
                            exam,
                            other,
                            prep_day_per_credit,
                            min_prep_days,
                            year1_allow_same_day=year1_allow_same_day,
                            for_year1_student=_is_year1_student(sid),
                            same_calendar_day=(diff == 0),
                        )
                        if need_idx <= 0:
                            continue
                        if diff < need_idx:
                            deficit = need_idx - diff
                            deficit_sq_sum += deficit * deficit * sv_w
                            if _is_year1_student(sid) and diff > 0:
                                deficit_sq_sum += deficit * deficit * sv_w * 150.0
                            if max(exam.credits, other.credits) >= 4.0:
                                high_credit_pen += deficit * deficit * 2.0 * sv_w
                            if diff == 0:
                                same_day_count += sv_w
            scale = len(exam.student_ids) / max(1, len(sample))
            spread_w = max(0.01, float(spread_prep_factor))
            score += deficit_sq_sum * scale * 0.55 * spread_w * prep_w
            score += high_credit_pen * scale * 0.38 * spread_w * prep_w
            score += same_day_count * scale * 72.0 * spread_w * prep_w

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

        # (6) Pattern học từ lịch chia tay: ưu tiên đúng ca quen thuộc theo mã học phần + thứ/ca.
        if pattern_weight > 0:
            pfx = str(exam.course_prefix_7 or "").strip()
            pref_sess = preferred_session_by_prefix7.get(pfx)
            if pref_sess is not None:
                score += abs(int(sess) - int(pref_sess)) * 4.0 * float(pattern_weight)
            wd = weekday_at_day_index(window, day)
            bonus = float(weekday_session_bonus.get((int(wd), int(sess)), 0.0))
            if bonus > 0:
                score -= bonus * 6.0 * float(pattern_weight)

        # (7) Soft constraint phòng: không quá 50 phòng/ca theo từng loại phòng thi.
        if max_rooms_per_slot_per_format > 0:
            fmt = _exam_format_code(idx)
            projected_rooms = slot_room_demand_by_format.get((slot, fmt), 0.0) + _estimated_rooms_for_exam(idx)
            overflow = projected_rooms - float(max_rooms_per_slot_per_format)
            if overflow > 0:
                score += (overflow ** 2) * 180.0

        return score

    def _prep_deficit_for_slot(idx: int, slot: int) -> float:
        """Tổng thiếu hụt ngày ôn nếu đặt môn idx vào slot (không commit)."""
        if prep_day_per_credit <= 0 and min_prep_days <= 0:
            return 0.0
        exam = exams[idx]
        day = slot // window.sessions_per_day
        deficit = 0.0
        for sid in exam.student_ids:
            y1 = _is_year1_student(sid)
            for d, oidx_list in student_day_exams[sid].items():
                for oidx in oidx_list:
                    if oidx == idx:
                        continue
                    other = exams[oidx]
                    same = d == day
                    if prep_gap_violated(
                        abs(d - day),
                        exam,
                        other,
                        prep_day_per_credit,
                        min_prep_days,
                        year1_allow_same_day=year1_allow_same_day,
                        for_year1_student=y1,
                        same_calendar_day=same,
                    ):
                        need = min_prep_index_gap_between(
                            exam,
                            other,
                            prep_day_per_credit,
                            min_prep_days,
                            year1_allow_same_day=year1_allow_same_day,
                            for_year1_student=y1,
                            same_calendar_day=same,
                        )
                        gap = 0 if same else abs(d - day)
                        deficit += max(1.0, float(need - gap))
        return deficit

    def _pick_force_slot(
        idx: int,
        allowed: List[int],
        *,
        ignore_khoa: bool,
        ignore_prefix: bool,
    ) -> Optional[int]:
        """Chọn slot ép cuối: luôn ưu tiên ôn + 1 môn/ngày; không bỏ ôn trừ khi không còn chỗ."""
        tiers: List[Tuple[bool, bool, bool]] = [
            (False, False, False),
            (False, True, False),
            (True, False, False),
            (True, True, False),
        ]
        if not ignore_prefix:
            tiers.append((True, True, True))
        for ignore_sunday, ignore_pfx, ign_khoa in tiers:
            if ign_khoa and not ignore_khoa:
                continue
            best_slot: Optional[int] = None
            best_pen = float("inf")
            for slot in allowed:
                if not _try_place(
                    idx,
                    slot,
                    allow_conflict=False,
                    ignore_capacity=True,
                    ignore_per_day=False,
                    ignore_credit_prep=False,
                    ignore_khoa_nhom=ign_khoa,
                    ignore_prefix_anchor=ignore_pfx,
                    ignore_sunday_spread=ignore_sunday,
                ):
                    continue
                pen = _score_slot(idx, slot) + _prep_deficit_for_slot(idx, slot) * 5000.0
                if pen < best_pen:
                    best_pen = pen
                    best_slot = slot
            if best_slot is not None:
                return best_slot
        # Không còn slot khả thi cứng — chọn chỗ thiếu ôn ít nhất (vẫn không trùng ca / 2 môn-ngày).
        if allowed:
            return min(
                allowed,
                key=lambda sl: (
                    _prep_deficit_for_slot(idx, sl),
                    _score_slot(idx, sl),
                ),
            )
        return None

    def _place_with_policy(
        allow_conflict: bool,
        ignore_capacity: bool,
        ignore_per_day: bool,
        ignore_credit_prep: bool = False,
    ) -> List[int]:
        """Thử xếp lần lượt các môn còn lại; trả về list các môn vẫn không đặt được."""
        nonlocal weekday_relax_logged
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

    def _vio_score_from_state(asgn: Dict[str, int]) -> Tuple[int, int, Dict[str, float]]:
        per_student: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
        spd = window.sessions_per_day
        for eid, slot in asgn.items():
            if eid not in exam_index:
                continue
            day = slot // spd
            for sid in exams[exam_index[eid]].student_ids:
                per_student[sid].append((day, eid))
        per_exam_sev: Dict[str, float] = defaultdict(float)
        total = same = 0
        for sid, entries in per_student.items():
            entries.sort()
            y1 = _is_year1_student(sid)
            for i in range(1, len(entries)):
                d0, e0 = entries[i - 1]
                d1, e1 = entries[i]
                if d0 != d1:
                    continue
                need = min_prep_index_gap_between(
                    exams[exam_index[e0]],
                    exams[exam_index[e1]],
                    prep_day_per_credit,
                    min_prep_days,
                    year1_allow_same_day=year1_allow_same_day,
                    for_year1_student=y1,
                    same_calendar_day=True,
                )
                if need > 0:
                    same += 1
                    per_exam_sev[e0] += 2.0
                    per_exam_sev[e1] += 2.0
                total += 1
        return total, same, per_exam_sev

    def _count_same_day_violation_pairs() -> int:
        _, sd, _ = _vio_score_from_state(assignment)
        return sd

    def _break_same_day_pairs(max_passes: int = 10) -> int:
        """Dời môn khỏi ngày đã có ≥2 môn — mọi SV khi chưa nới 2 môn/ngày (giống xếp tay)."""
        moved = 0
        fixed_set = set(fixed_slots.keys())
        spd = window.sessions_per_day
        for _ in range(max_passes):
            round_m = 0
            sid_exams: Dict[str, List[str]] = defaultdict(list)
            for eid in assignment:
                for sid in exams[exam_index[eid]].student_ids:
                    sid_exams[sid].append(eid)
            for sid, eids in sid_exams.items():
                uniq = sorted(set(eids), key=lambda e: assignment[e] // spd)
                by_day: Dict[int, List[str]] = defaultdict(list)
                for e in uniq:
                    by_day[assignment[e] // spd].append(e)
                for day, day_eids in by_day.items():
                    if len(day_eids) < 2:
                        continue
                    for eid in day_eids:
                        if eid in fixed_set:
                            continue
                        midx = exam_index[eid]
                        old = assignment[eid]
                        if _uncommit(midx) is None:
                            continue
                        placed = False
                        for cand in _slots_for_exam(midx, relax_weekday=True):
                            if cand // spd == day:
                                continue
                            if _try_place(
                                midx,
                                cand,
                                allow_conflict=False,
                                ignore_capacity=True,
                                ignore_per_day=False,
                                ignore_credit_prep=False,
                            ):
                                _commit(midx, cand)
                                placed = True
                                round_m += 1
                                break
                        if not placed:
                            _commit(midx, old)
            moved += round_m
            if round_m == 0:
                break
        return moved

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

    # 2b) Tách cặp thi cùng ngày (khóa sau) trước khi nới môn/ngày
    if assignment and max_exams_per_day > 0 and (prep_day_per_credit > 0 or min_prep_days > 0):
        sd_before = _count_same_day_violation_pairs()
        if sd_before > 0:
            broken = _break_same_day_pairs(max_passes=15)
            if broken > 0:
                relaxations.append(
                    f"Tách thi cùng ngày (trước khi nới môn/ngày): {sd_before:,} → "
                    f"{_count_same_day_violation_pairs():,} cặp cùng-ngày, đã dời {broken} môn."
                )

    # 3) Thử xếp tiếp — vẫn giữ 1 môn/SV/ngày (giống xếp tay)
    if leftover:
        relaxations.append(
            "Tiếp tục xếp môn còn lại — giữ tối đa 1 môn/SV/ngày để bảo toàn ngày ôn."
        )
        leftover = _place_with_policy(
            allow_conflict=False,
            ignore_capacity=True,
            ignore_per_day=False,
            ignore_credit_prep=False,
        )
        remaining_state["pending"] = leftover

    if assignment and (prep_day_per_credit > 0 or min_prep_days > 0):
        sd2 = _count_same_day_violation_pairs()
        if sd2 > 0:
            b2 = _break_same_day_pairs(max_passes=20)
            if b2 > 0:
                relaxations.append(
                    f"Tách thi cùng ngày (sau bước 3): {_count_same_day_violation_pairs():,} cặp, "
                    f"đã dời thêm {b2} môn."
                )

    def _place_leftover_loop(
        indices: List[int],
        *,
        allow_conflict: bool,
        ignore_credit_prep: bool,
        ignore_khoa: bool,
        ignore_prefix: bool,
        ignore_sunday: bool = False,
        ignore_per_day: bool = False,
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
                    ignore_per_day=ignore_per_day,
                    ignore_credit_prep=ignore_credit_prep,
                    ignore_khoa_nhom=ignore_khoa,
                    ignore_prefix_anchor=ignore_prefix,
                    ignore_sunday_spread=ignore_sunday,
                ):
                    pen = _score_slot(idx, slot) + _prep_deficit_for_slot(idx, slot) * 8000.0
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
                slot_force = int(forced) if forced is not None else int(
                    min(
                        allowed,
                        key=lambda sl: (
                            _prep_deficit_for_slot(idx, sl),
                            _score_slot(idx, sl),
                        ),
                    )
                )
                if not _safe_force_place(
                    idx, slot_force, ignore_khoa=ignore_khoa, ignore_prefix=ignore_prefix
                ):
                    if not _try_place(
                        idx,
                        slot_force,
                        allow_conflict=False,
                        ignore_capacity=True,
                        ignore_per_day=False,
                        ignore_credit_prep=False,
                        ignore_khoa_nhom=ignore_khoa,
                        ignore_prefix_anchor=ignore_prefix,
                        ignore_sunday_spread=True,
                    ):
                        _force_commit(idx, slot_force)
                        if exams[idx].exam_id not in assignment:
                            still.append(idx)
                    else:
                        _commit(idx, slot_force)
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
            ignore_per_day=False,
            force_if_needed=False,
        )
        leftover = [i for i in leftover if exams[i].exam_id not in assignment]

    # 4) Bỏ «trùng SV cùng ca» — gây hàng nghìn vi phạm ôn (SV thi 2 môn cùng slot).
    # Thay bằng tiếp tục xếp với Khoa_nhom, vẫn 1 môn/SV/ngày + ôn cứng.
    if leftover:
        relaxations.append(
            f"Tiếp tục xếp {len(leftover)} môn — giữ 1 môn/SV/ngày, không trùng ca."
        )
        leftover = _place_leftover_loop(
            list(leftover),
            allow_conflict=False,
            ignore_credit_prep=False,
            ignore_khoa=False,
            ignore_prefix=False,
            ignore_sunday=False,
            ignore_per_day=False,
            force_if_needed=False,
        )

    # 4.5) Nới loại ca — thiếu ô theo nhãn LT/TN/VD (vẫn giữ Khoa_nhom & buổi HP)
    if leftover:
        relaxations.append(
            f"Nới loại ca cho {len(leftover)} môn — dùng mọi ca trong đợt nếu thiếu slot theo nhãn loại."
        )
        spd_sess = window.sessions_per_day
        for idx in leftover:
            allowed_sessions_by_exam_id[exams[idx].exam_id] = list(range(spd_sess))
        leftover = _place_leftover_loop(
            list(leftover),
            allow_conflict=False,
            ignore_credit_prep=False,
            ignore_khoa=False,
            ignore_prefix=False,
            ignore_sunday=False,
            ignore_per_day=False,
            force_if_needed=False,
        )

    # 5) Nới Khoa_nhom + buổi HP (ca tách) — vẫn giữ ôn + 1 môn/ngày
    if leftover:
        relaxations.append(
            f"Nới Khoa_nhom & buổi HP cho {len(leftover)} môn — khóa sau có thể vi phạm ôn khi cần."
        )
        leftover = _place_leftover_loop(
            list(leftover),
            allow_conflict=False,
            ignore_credit_prep=False,
            ignore_khoa=True,
            ignore_prefix=True,
            ignore_sunday=False,
            ignore_per_day=False,
            force_if_needed=False,
        )

    # 6) Bắt buộc 100% — chỉ nới CN / trùng ca khi ép; vẫn ưu tiên ôn + 1 môn/ngày
    if leftover:
        n_force = len(leftover)
        relaxations.append(
            f"Đặt bắt buộc {n_force} môn còn lại — ưu tiên slot còn đủ ngày ôn."
        )
        leftover = _place_leftover_loop(
            list(leftover),
            allow_conflict=False,
            ignore_credit_prep=False,
            ignore_khoa=True,
            ignore_prefix=True,
            ignore_sunday=True,
            ignore_per_day=False,
            force_if_needed=True,
        )

    # 7) Không ép ra ngoài Ke_hoach_thi — chỉ gán trong ô còn lại của đợt lớp
    if leftover:
        plan_blocked: List[int] = []
        for idx in leftover:
            eid = exams[idx].exam_id
            allowed_em = _slots_for_exam(idx, relax_weekday=True)
            if not allowed_em:
                plan_days = exam_allowed_day_indices(exams[idx], window)
                if plan_days:
                    spd = window.sessions_per_day
                    sessions = allowed_sessions_by_exam_id.get(
                        eid, list(range(spd))
                    )
                    sessions = sorted({int(s) for s in sessions if 0 <= int(s) < spd})
                    allowed_em = [
                        d * spd + s for d in sorted(plan_days) for s in sessions
                    ]
            if allowed_em:
                forced = _pick_force_slot(
                    idx, allowed_em, ignore_khoa=True, ignore_prefix=True
                )
                slot_guess = int(forced) if forced is not None else int(allowed_em[0])
                if not _safe_force_place(
                    idx, slot_guess, ignore_khoa=True, ignore_prefix=True
                ):
                    best_em = min(
                        allowed_em,
                        key=lambda sl: (
                            _prep_deficit_for_slot(idx, sl),
                            _score_slot(idx, sl),
                        ),
                    )
                    placed_em = False
                    for try_slot in (slot_guess, best_em):
                        if _try_place(
                            idx,
                            int(try_slot),
                            allow_conflict=False,
                            ignore_capacity=True,
                            ignore_per_day=False,
                            ignore_credit_prep=False,
                            ignore_khoa_nhom=True,
                            ignore_prefix_anchor=True,
                            ignore_sunday_spread=True,
                        ):
                            _commit(idx, int(try_slot))
                            placed_em = True
                            break
                    if not placed_em:
                        _force_commit(idx, int(best_em))
                        if exams[idx].exam_id not in assignment:
                            plan_blocked.append(idx)
            else:
                plan_blocked.append(idx)
        if plan_blocked:
            relaxations.append(
                f"Không xếp được {len(plan_blocked)} môn trong đợt Khoa_lop* "
                "(gom lớp khác đợt hoặc thiếu ngày — xem «Môn chưa xếp»)."
            )

    large_instance = n >= 1000 or len(conflicts) >= 80_000
    greedy_rounds_all = 2 if large_instance else 5
    greedy_rounds_year1 = 5 if large_instance else 10
    prep_rounds_year1 = 10 if large_instance else 28
    prep_rounds_all = 12 if large_instance else 32
    break_same_day_passes = 6 if large_instance else 12
    tail_repair_rounds = 6 if large_instance else 12

    repaired = _greedy_repair_prep(rounds=greedy_rounds_all) + _greedy_repair_prep(
        rounds=greedy_rounds_year1, year1_only=True
    )

    def _repair_prep_gaps(
        max_rounds: int = 24,
        *,
        year1_only: bool = False,
        max_wave: Optional[int] = None,
    ) -> int:
        """Giãn lịch SV: dời môn khi hai ngày thi liên tiếp thiếu ôn (ưu tiên khóa anchor)."""
        if year1_anchor <= 0 and year1_only:
            return 0
        fixed_set = set(fixed_slots.keys())
        spd = window.sessions_per_day
        moved = 0
        for _ in range(max_rounds):
            round_moved = 0
            sid_to_eids: Dict[str, List[str]] = defaultdict(list)
            for idx in range(n):
                eid = exams[idx].exam_id
                if eid not in assignment:
                    continue
                for sid in exams[idx].student_ids:
                    if year1_only and not _is_year1_student(sid):
                        continue
                    if max_wave is not None:
                        w = cohort_wave_index(
                            student_cohort_codes.get(str(sid), ""), year1_anchor
                        )
                        if w > max_wave:
                            continue
                    sid_to_eids[sid].append(eid)
            for sid, eids in sid_to_eids.items():
                y1 = _is_year1_student(sid)
                uniq = sorted(set(eids), key=lambda e: assignment[e] // spd)
                for i in range(1, len(uniq)):
                    prev_eid, curr_eid = uniq[i - 1], uniq[i]
                    prev_day = assignment[prev_eid] // spd
                    curr_day = assignment[curr_eid] // spd
                    same_cal = prev_day == curr_day
                    need = min_prep_index_gap_between(
                        exams[exam_index[prev_eid]],
                        exams[exam_index[curr_eid]],
                        prep_day_per_credit,
                        min_prep_days,
                        year1_allow_same_day=year1_allow_same_day,
                        for_year1_student=y1,
                        same_calendar_day=same_cal,
                    )
                    gap = 0 if same_cal else (curr_day - prev_day)
                    if need <= 0 or gap >= need:
                        continue
                    fixed = False
                    for move_eid, anchor_day, later in (
                        (curr_eid, prev_day, True),
                        (prev_eid, curr_day, False),
                    ):
                        if move_eid in fixed_set:
                            continue
                        midx = exam_index[move_eid]
                        old_slot = assignment[move_eid]
                        if _uncommit(midx) is None:
                            continue
                        best_cand = old_slot
                        best_pen = float("inf")
                        for ign_khoa in (False, True):
                            for cand in _slots_for_exam(midx, relax_weekday=True):
                                new_day = cand // spd
                                if same_cal:
                                    if new_day == anchor_day:
                                        continue
                                elif later:
                                    if new_day <= anchor_day or (new_day - anchor_day) < need:
                                        continue
                                elif new_day >= anchor_day or (anchor_day - new_day) < need:
                                    continue
                                if not _try_place(
                                    midx,
                                    cand,
                                    allow_conflict=False,
                                    ignore_capacity=True,
                                    ignore_per_day=False,
                                    ignore_credit_prep=False,
                                    ignore_khoa_nhom=ign_khoa,
                                ):
                                    continue
                                _commit(midx, cand)
                                pen = _score_slot(midx, cand)
                                if pen < best_pen - 1e-6:
                                    best_pen = pen
                                    best_cand = cand
                                if _uncommit(midx) is None:
                                    break
                            if best_cand != old_slot:
                                break
                        if best_cand != old_slot:
                            _commit(midx, best_cand)
                            round_moved += 1
                            fixed = True
                            break
                        _commit(midx, old_slot)
                    if fixed:
                        break
            moved += round_moved
            if round_moved == 0:
                break
        return moved

    # Cuối pipeline: sửa ôn mọi khóa (ưu tiên anchor trước).
    y1_fixed = _repair_prep_gaps(max_rounds=prep_rounds_year1, year1_only=True)
    all_fixed = _repair_prep_gaps(max_rounds=prep_rounds_all, year1_only=False, max_wave=None)
    repaired += y1_fixed + all_fixed
    repaired += _greedy_repair_prep(rounds=greedy_rounds_year1)
    sd_tail = _count_same_day_violation_pairs()
    if sd_tail > 0:
        broken_tail = _break_same_day_pairs(max_passes=break_same_day_passes)
        repaired += broken_tail
        if broken_tail > 0:
            repaired += _repair_prep_gaps(
                max_rounds=tail_repair_rounds, year1_only=False, max_wave=None
            )
            repaired += _greedy_repair_prep(rounds=max(2, greedy_rounds_all))
    if repaired > 0:
        relaxations.append(
            f"Sửa sau greedy: đã chuyển {repaired} ca để giảm vi phạm ôn/CN "
            f"(khóa {year1_anchor:02d}: {y1_fixed}, mọi khóa: {all_fixed})."
        )
    if large_instance:
        relaxations.append(
            "Instance lớn: rút gọn số vòng repair hậu-greedy để giảm thời gian chạy."
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

    # Phạt cặp thi cùng ngày (gap=0) — cao hơn cặp thiếu 1 ngày để LNS ưu tiên tách cùng-ngày.
    _LNS_SAME_DAY_SEV_BONUS = 4.0

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

    def _lns_max_exams_per_day(sid: str) -> int:
        cap = int(max_exams_per_day or 0)
        if cap <= 0:
            return 0
        if prep_day_per_credit <= 0 and min_prep_days <= 0:
            return cap
        # LNS không nới 2 môn/ngày — giữ như xếp tay, tránh làm xấu vi phạm ôn.
        return 1

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
        same_day_count = 0
        for sid, entries in per_student.items():
            entries.sort()
            for i in range(1, len(entries)):
                prev_day, prev_eid = entries[i - 1]
                curr_day, curr_eid = entries[i]
                prev_exam = exams[exam_index[prev_eid]]
                curr_exam = exams[exam_index[curr_eid]]
                y1 = _lns_is_year1(sid)
                is_same_calendar = curr_day == prev_day
                need = min_prep_index_gap_between(
                    prev_exam,
                    curr_exam,
                    prep_day_per_credit,
                    min_prep_days,
                    year1_allow_same_day=year1_allow_same_day,
                    for_year1_student=y1,
                    same_calendar_day=is_same_calendar,
                )
                gap = curr_day - prev_day
                if need <= 0:
                    continue
                if gap < need:
                    total += 1
                    deficit = need - gap
                    sev = 1.0 + 0.3 * deficit
                    if gap == 0:
                        same_day_count += 1
                        sev += _LNS_SAME_DAY_SEV_BONUS
                    per_exam_sev[curr_eid] += sev
                    per_exam_sev[prev_eid] += sev
        return total, same_day_count, per_exam_sev

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

        Điểm = 1.0 (base count) + 0.3·deficit + bonus·1[gap==0]. Dùng cho LNS so sánh delta
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
                        sev += _LNS_SAME_DAY_SEV_BONUS
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
            lim = _lns_max_exams_per_day(sid)
            if lim > 0 and student_day_count[(sid, new_day)] >= lim:
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
                if prep_gap_violated(
                    abs(new_day - other_day),
                    exam,
                    other_exam,
                    prep_day_per_credit,
                    min_prep_days,
                    year1_allow_same_day=year1_allow_same_day,
                    for_year1_student=y1,
                    same_calendar_day=(new_day == other_day),
                ):
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

    # Pre-compute allowed slots per exam (thứ trong tuần: T2–T7 / T7–CN cho môn đông).
    # LNS cũng thử mọi loại ca trong đợt để tìm chỗ giảm vi phạm ôn (không chỉ ca theo nhãn).
    allowed_by_exam: Dict[str, List[int]] = {}
    spd_lns = window.sessions_per_day
    all_session_slots = list(range(spd_lns))
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
        wide = enumerate_feasible_slots_for_exam(
            ex,
            window,
            {eid: all_session_slots},
            fixed_slots,
            weekend_large_min_students=weekend_large_course_min_students,
            prefix_totals=prefix_tot,
            relax_weekday_rule=True,
        )
        if wide:
            slots = sorted(set(slots) | set(wide))
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
                        sev += _LNS_SAME_DAY_SEV_BONUS
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

    if base_vio > 8000:
        lns_no_improve_limit = 4
    elif base_vio > 4000:
        lns_no_improve_limit = 3
    else:
        lns_no_improve_limit = 2

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
            cur_sd, _ = _same_day_and_sev_for_exam(eid, old_slot)
            if cur_vio <= 0 and cur_peak <= 0 and cur_sd <= 0:
                continue
            best_slot = old_slot
            best_pair = (cur_sd, cur_vio + cur_peak)
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
                new_sd, _ = _same_day_and_sev_for_exam(eid, slot)
                pair = (new_sd, v + p)
                if pair < best_pair:
                    best_pair = pair
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
            if no_improve_count >= lns_no_improve_limit:
                logs.append(
                    f"Dừng cải tiến LNS chính sớm: {lns_no_improve_limit} vòng liên tiếp không cải thiện."
                )
                break

    # ---- Pass cuối: dedicated same-day breaker ----
    # Bounded cost (chỉ duyệt exam còn dính same-day). Chấp nhận move khi giảm cùng-ngày
    # hoặc giữ nguyên cùng-ngày nhưng giảm severity tổng. Đây không phải tinh chỉnh dữ liệu
    # cụ thể — đó là cấu trúc thuật toán: tách giai đoạn tổng quát (LNS chính) và giai đoạn
    # targeted (same-day) để tránh stuck ở local optimum nhẹ.
    _, same_day_left, _ = _compute_violation_score(current)
    for sd_pass in range(3):
        if same_day_left <= 0:
            break
        if progress_cb:
            progress_cb(
                72,
                f"Bước 2/3: pass cùng-ngày {sd_pass + 1}/3 — đang phá {same_day_left:,} cặp…",
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
                if pair < best_pair:
                    best_pair = pair
                    best_slot = slot
            if best_slot != old_slot:
                _move(eid, best_slot)
                moved_sd += 1
        after_total, after_sd, _ = _compute_violation_score(current)
        logs.append(
            f"Pass cùng-ngày {sd_pass + 1}: {same_day_left:,} → {after_sd:,} "
            f"(tổng {after_total:,}, đã chuyển {moved_sd} môn)"
        )
        if after_sd >= same_day_left:
            break
        same_day_left = after_sd

    # ---- Pass sửa prep (nhanh): dùng điểm cục bộ / môn — không quét lại toàn bộ SV mỗi ô thử ----
    if base_vio > 8000:
        prep_rounds = max(14, min(22, iterations * 3))
        prep_pool = min(max(pool_size, 300), 400)
        prep_slot_cap = 120
    elif base_vio > 4000:
        prep_rounds = max(12, min(20, iterations * 2 + 6))
        prep_pool = min(max(pool_size, 260), 360)
        prep_slot_cap = 110
    elif base_vio > 2500:
        prep_rounds = max(14, min(22, iterations * 2 + 8))
        prep_pool = min(max(pool_size, 300), 400)
        prep_slot_cap = 120
    elif base_vio > 1200:
        prep_rounds = max(10, min(18, iterations * 2 + 6))
        prep_pool = min(max(pool_size, 240), 320)
        prep_slot_cap = 100
    else:
        prep_rounds = max(6, min(12, iterations * 2 + 2))
        prep_pool = min(pool_size, 120)
        prep_slot_cap = 60
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

    # Pass sửa prep mạnh: nới Khoa_nhom/buổi HP khi còn nhiều vi phạm (sau bước nới ôn khóa sau).
    total_after_prep, sd_after_prep, per_exam_after = _compute_violation_score(current)
    if total_after_prep > 3500:
        relax_pool = sorted(
            ((eid, v) for eid, v in per_exam_after.items() if eid not in fixed_slots),
            key=lambda x: -x[1],
        )[: min(350, prep_pool + 80)]
        moved_relax = 0
        for eid, _ in relax_pool:
            old_slot = current[eid]
            cur_sd, cur_sev = _same_day_and_sev_for_exam(eid, old_slot)
            cur_score = _vio_for_exam(eid, old_slot) + cur_sev + _peak_cost(eid, old_slot)
            if cur_score <= 0 and cur_sd <= 0:
                continue
            best_slot = old_slot
            best_pair = (cur_sd, cur_score)
            for slot in allowed_by_exam.get(eid, []):
                if slot == old_slot:
                    continue
                if _conflict_at_slot(eid, slot):
                    continue
                if _violates_max_per_day(eid, slot):
                    continue
                if not _prep_feasible(eid, slot):
                    continue
                new_sd, new_sev = _same_day_and_sev_for_exam(eid, slot)
                new_s = _vio_for_exam(eid, slot) + new_sev + _peak_cost(eid, slot)
                pair = (new_sd, new_s)
                if pair < best_pair:
                    best_pair = pair
                    best_slot = slot
            if best_slot != old_slot:
                _move(eid, best_slot)
                moved_relax += 1
        after_relax, after_relax_sd, _ = _compute_violation_score(current)
        if moved_relax > 0:
            logs.append(
                f"Pass sửa prep (nới Khoa_nhom/buổi): {total_after_prep:,} → {after_relax:,} "
                f"(cùng-ngày {sd_after_prep:,} → {after_relax_sd:,}, đã chuyển {moved_relax} môn)"
            )

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

        moved_y1_gap = 0
        for _gap_round in range(10):
            gap_round_moves = 0
            for sid in list(student_to_exams.keys()):
                if not _lns_is_year1(sid):
                    continue
                eids = [e for e in student_to_exams[sid] if e in current and e not in fixed_slots]
                entries = sorted(
                    [(current[e], e) for e in eids],
                    key=lambda x: x[0] // sessions_per_day,
                )
                for i in range(1, len(entries)):
                    prev_slot, prev_eid = entries[i - 1]
                    curr_slot, curr_eid = entries[i]
                    prev_day = prev_slot // sessions_per_day
                    curr_day = curr_slot // sessions_per_day
                    if prev_day == curr_day:
                        continue
                    need = min_prep_index_gap_between(
                        exams[exam_index[prev_eid]],
                        exams[exam_index[curr_eid]],
                        prep_day_per_credit,
                        min_prep_days,
                        year1_allow_same_day=year1_allow_same_day,
                        for_year1_student=True,
                        same_calendar_day=False,
                    )
                    if need <= 0 or (curr_day - prev_day) >= need:
                        continue
                    for move_eid, other_eid, move_later in (
                        (curr_eid, prev_eid, True),
                        (prev_eid, curr_eid, False),
                    ):
                        if move_eid in fixed_slots:
                            continue
                        old_slot = current[move_eid]
                        anchor_day = curr_day if move_later else prev_day
                        best_slot = old_slot
                        best_key = (1, float("inf"))
                        for slot in allowed_by_exam.get(move_eid, []):
                            new_day = slot // sessions_per_day
                            if move_later:
                                if new_day <= anchor_day:
                                    continue
                                if (new_day - anchor_day) < need:
                                    continue
                            else:
                                if new_day >= anchor_day:
                                    continue
                                if (anchor_day - new_day) < need:
                                    continue
                            if _conflict_at_slot(move_eid, slot):
                                continue
                            if _violates_max_per_day(move_eid, slot):
                                continue
                            if not _khoa_feasible(move_eid, slot):
                                continue
                            if not _prefix_feasible(move_eid, slot):
                                continue
                            if not _prep_feasible(move_eid, slot):
                                continue
                            sd, sev = _same_day_and_sev_for_exam(move_eid, slot)
                            key = (sd, _vio_for_exam(move_eid, slot) + sev + _peak_cost(move_eid, slot))
                            if key < best_key:
                                best_key = key
                                best_slot = slot
                        if best_slot != old_slot:
                            _move(move_eid, best_slot)
                            gap_round_moves += 1
                            break
            moved_y1_gap += gap_round_moves
            if gap_round_moves == 0:
                break
        if moved_y1_gap > 0:
            logs.append(
                f"Pass khóa {lns_year1_anchor:02d} (ôn giữa hai ngày): đã dời {moved_y1_gap} môn."
            )

    def _year1_cross_day_violations() -> int:
        if lns_year1_anchor <= 0:
            return 0
        count = 0
        for sid, eids in student_to_exams.items():
            if not _lns_is_year1(sid):
                continue
            entries = sorted(
                (current[e] // sessions_per_day, e)
                for e in eids
                if e in current
            )
            for i in range(1, len(entries)):
                d0, e0 = entries[i - 1]
                d1, e1 = entries[i]
                if d0 == d1:
                    continue
                need = min_prep_index_gap_between(
                    exams[exam_index[e0]],
                    exams[exam_index[e1]],
                    prep_day_per_credit,
                    min_prep_days,
                    year1_allow_same_day=year1_allow_same_day,
                    for_year1_student=True,
                    same_calendar_day=False,
                )
                if need > 0 and (d1 - d0) < need:
                    count += 1
        return count

    y1_before = _year1_cross_day_violations()
    if y1_before > 0 and lns_year1_anchor > 0:
        polish_moves = 0
        for _polish in range(20):
            if _year1_cross_day_violations() == 0:
                break
            step = 0
            for sid in list(student_to_exams.keys()):
                if not _lns_is_year1(sid):
                    continue
                eids = [e for e in student_to_exams[sid] if e in current and e not in fixed_slots]
                entries = sorted(
                    [(current[e], e) for e in eids],
                    key=lambda x: x[0] // sessions_per_day,
                )
                for i in range(1, len(entries)):
                    prev_slot, prev_eid = entries[i - 1]
                    curr_slot, curr_eid = entries[i]
                    prev_day = prev_slot // sessions_per_day
                    curr_day = curr_slot // sessions_per_day
                    if prev_day == curr_day:
                        continue
                    need = min_prep_index_gap_between(
                        exams[exam_index[prev_eid]],
                        exams[exam_index[curr_eid]],
                        prep_day_per_credit,
                        min_prep_days,
                        year1_allow_same_day=year1_allow_same_day,
                        for_year1_student=True,
                        same_calendar_day=False,
                    )
                    if need <= 0 or (curr_day - prev_day) >= need:
                        continue
                    for move_eid, move_later in ((curr_eid, True), (prev_eid, False)):
                        if move_eid in fixed_slots:
                            continue
                        old_slot = current[move_eid]
                        anchor_day = curr_day if move_later else prev_day
                        best_slot = old_slot
                        best_key = (1, float("inf"))
                        for slot in allowed_by_exam.get(move_eid, []):
                            new_day = slot // sessions_per_day
                            if move_later:
                                if new_day <= anchor_day or (new_day - anchor_day) < need:
                                    continue
                            else:
                                if new_day >= anchor_day or (anchor_day - new_day) < need:
                                    continue
                            if _conflict_at_slot(move_eid, slot):
                                continue
                            if _violates_max_per_day(move_eid, slot):
                                continue
                            if not _khoa_feasible(move_eid, slot):
                                continue
                            if not _prefix_feasible(move_eid, slot):
                                continue
                            if not _prep_feasible(move_eid, slot):
                                continue
                            sd, sev = _same_day_and_sev_for_exam(move_eid, slot)
                            key = (
                                sd,
                                _vio_for_exam(move_eid, slot) + sev + _peak_cost(move_eid, slot),
                            )
                            if key < best_key:
                                best_key = key
                                best_slot = slot
                        if best_slot != old_slot:
                            _move(move_eid, best_slot)
                            step += 1
                            break
                    if step:
                        break
                if step:
                    break
            polish_moves += step
            if step == 0:
                break
        y1_after = _year1_cross_day_violations()
        if polish_moves > 0 or y1_after < y1_before:
            logs.append(
                f"Pass tinh chỉnh khóa {lns_year1_anchor:02d}: vi phạm ôn giữa hai ngày "
                f"{y1_before:,} → {y1_after:,} (đã dời {polish_moves} môn)."
            )

    def _lns_spread_peak_days(max_moves: int = 120) -> int:
        """Dời môn từ ngày quá tải sang ngày còn trống nếu không tăng vi phạm ôn."""
        day_load: Dict[int, int] = defaultdict(int)
        for eid, slot in current.items():
            if eid in exam_index:
                day_load[slot // sessions_per_day] += exams[exam_index[eid]].size
        if len(day_load) < 3:
            return 0
        avg_load = sum(day_load.values()) / len(day_load)
        peak_thresh = avg_load * 1.12
        low_thresh = avg_load * 0.88
        peak_days = {d for d, load in day_load.items() if load > peak_thresh}
        low_days = {d for d, load in day_load.items() if load < low_thresh}
        if not peak_days or not low_days:
            return 0
        moved = 0
        candidates = [
            eid
            for eid, slot in current.items()
            if eid not in fixed_slots
            and eid in exam_index
            and (slot // sessions_per_day) in peak_days
        ]
        candidates.sort(
            key=lambda e: -exams[exam_index[e]].size,
        )
        for eid in candidates:
            if moved >= max_moves:
                break
            old_slot = current[eid]
            old_day = old_slot // sessions_per_day
            cur_vio = _vio_for_exam(eid, old_slot)
            cur_sd, cur_sev = _same_day_and_sev_for_exam(eid, old_slot)
            cur_peak = _peak_cost(eid, old_slot)
            cur_key = (cur_sd, cur_vio + cur_sev + cur_peak)
            best_slot = old_slot
            best_key = cur_key
            for slot in allowed_by_exam.get(eid, []):
                new_day = slot // sessions_per_day
                if new_day == old_day or new_day not in low_days:
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
                new_vio = _vio_for_exam(eid, slot)
                new_sd, new_sev = _same_day_and_sev_for_exam(eid, slot)
                new_peak = _peak_cost(eid, slot)
                new_key = (new_sd, new_vio + new_sev + new_peak)
                if new_key > cur_key:
                    continue
                if day_load[new_day] + exams[exam_index[eid]].size >= day_load[old_day]:
                    continue
                if new_key < best_key or (
                    new_key[0] == best_key[0]
                    and day_load[new_day] < day_load[best_slot // sessions_per_day]
                ):
                    best_key = new_key
                    best_slot = slot
            if best_slot != old_slot:
                sz = exams[exam_index[eid]].size
                day_load[old_day] -= sz
                day_load[best_slot // sessions_per_day] += sz
                _move(eid, best_slot)
                moved += 1
        return moved

    def _try_move_exam_gap(
        move_eid: str,
        *,
        anchor_day: int,
        need: int,
        move_later: bool,
        same_cal: bool,
        relax_khoa: bool,
        relax_prefix: bool,
    ) -> Optional[int]:
        """Tìm slot tốt nhất để giãn cặp vi phạm ôn; None nếu không dời được."""
        old_slot = current[move_eid]
        best_slot = old_slot
        best_key = (999, float("inf"))
        for slot in allowed_by_exam.get(move_eid, []):
            new_day = slot // sessions_per_day
            if same_cal:
                if new_day == anchor_day:
                    continue
            elif move_later:
                if new_day <= anchor_day or (new_day - anchor_day) < need:
                    continue
            elif new_day >= anchor_day or (anchor_day - new_day) < need:
                continue
            if _conflict_at_slot(move_eid, slot):
                continue
            if _violates_max_per_day(move_eid, slot):
                continue
            if not relax_khoa and not _khoa_feasible(move_eid, slot):
                continue
            if not relax_prefix and not _prefix_feasible(move_eid, slot):
                continue
            if not _prep_feasible(move_eid, slot):
                continue
            sd, sev = _same_day_and_sev_for_exam(move_eid, slot)
            key = (sd, _vio_for_exam(move_eid, slot) + sev + _peak_cost(move_eid, slot))
            if key < best_key:
                best_key = key
                best_slot = slot
        return best_slot if best_slot != old_slot else None

    def _try_move_exam_gap_pair_only(
        move_eid: str,
        other_eid: str,
        *,
        anchor_day: int,
        need: int,
        move_later: bool,
        same_cal: bool,
        relax_khoa: bool,
    ) -> Optional[int]:
        """Chỉ sửa cặp (move_eid, other_eid) — dùng khi _prep_feasible quá chặt toàn cục."""
        old_slot = current[move_eid]
        best_slot = old_slot
        for slot in allowed_by_exam.get(move_eid, []):
            new_day = slot // sessions_per_day
            if same_cal:
                if new_day == anchor_day:
                    continue
            elif move_later:
                if new_day <= anchor_day or (new_day - anchor_day) < need:
                    continue
            else:
                if new_day >= anchor_day or (anchor_day - new_day) < need:
                    continue
            if _conflict_at_slot(move_eid, slot):
                continue
            if _violates_max_per_day(move_eid, slot):
                continue
            if not relax_khoa and not _khoa_feasible(move_eid, slot):
                continue
            other_day = current[other_eid] // sessions_per_day
            gap = 0 if new_day == other_day else abs(new_day - other_day)
            if gap < need:
                continue
            if slot != old_slot:
                best_slot = slot
                break
        return best_slot if best_slot != old_slot else None

    def _polish_all_cohort_prep_gaps(max_rounds: int = 20) -> int:
        """Vòng 2: sửa ôn mọi khóa — ưu tiên cùng-ngày (0 ngày ôn) rồi gap=1."""
        moved_total = 0
        before_all, before_sd, _ = _compute_violation_score(current)
        for _rnd in range(max_rounds):
            round_moves = 0
            total_vio, sd_vio, _ = _compute_violation_score(current)
            if total_vio == 0:
                break
            # SV có vi phạm: ưu tiên nhiều môn & cùng-ngày trước
            sid_scores: List[Tuple[float, str]] = []
            for sid, eids in student_to_exams.items():
                elist = [e for e in eids if e in current and e not in fixed_slots]
                if len(elist) < 2:
                    continue
                entries = sorted(
                    (current[e] // sessions_per_day, e) for e in elist
                )
                bad = 0.0
                for i in range(1, len(entries)):
                    d0, e0 = entries[i - 1]
                    d1, e1 = entries[i]
                    y1s = _lns_is_year1(sid)
                    same = d0 == d1
                    need = min_prep_index_gap_between(
                        exams[exam_index[e0]],
                        exams[exam_index[e1]],
                        prep_day_per_credit,
                        min_prep_days,
                        year1_allow_same_day=year1_allow_same_day,
                        for_year1_student=y1s,
                        same_calendar_day=same,
                    )
                    gap = 0 if same else (d1 - d0)
                    if need > 0 and gap < need:
                        bad += (need - gap) * (3.0 if gap == 0 else 1.0)
                if bad > 0:
                    sid_scores.append((-bad, sid))
            sid_scores.sort()
            for _, sid in sid_scores[: min(800, len(sid_scores))]:
                eids = [e for e in student_to_exams[sid] if e in current and e not in fixed_slots]
                entries = sorted(
                    [(current[e] // sessions_per_day, e) for e in eids],
                    key=lambda x: (x[0], x[1]),
                )
                for i in range(1, len(entries)):
                    prev_day, prev_eid = entries[i - 1]
                    curr_day, curr_eid = entries[i]
                    y1s = _lns_is_year1(sid)
                    same_cal = prev_day == curr_day
                    need = min_prep_index_gap_between(
                        exams[exam_index[prev_eid]],
                        exams[exam_index[curr_eid]],
                        prep_day_per_credit,
                        min_prep_days,
                        year1_allow_same_day=year1_allow_same_day,
                        for_year1_student=y1s,
                        same_calendar_day=same_cal,
                    )
                    gap = 0 if same_cal else (curr_day - prev_day)
                    if need <= 0 or gap >= need:
                        continue
                    fixed_pair = False
                    for move_eid, anchor_day, later in (
                        (curr_eid, prev_day, True),
                        (prev_eid, curr_day, False),
                    ):
                        if move_eid in fixed_slots:
                            continue
                        for relax_khoa, relax_pfx in (
                            (False, False),
                            (True, False),
                            (True, True),
                        ):
                            new_slot = _try_move_exam_gap(
                                move_eid,
                                anchor_day=anchor_day,
                                need=need,
                                move_later=later,
                                same_cal=same_cal,
                                relax_khoa=relax_khoa,
                                relax_prefix=relax_pfx,
                            )
                            if new_slot is not None:
                                _move(move_eid, new_slot)
                                round_moves += 1
                                fixed_pair = True
                                break
                        if fixed_pair:
                            break
                        other_eid = prev_eid if move_eid == curr_eid else curr_eid
                        for relax_khoa in (False, True):
                            new_slot = _try_move_exam_gap_pair_only(
                                move_eid,
                                other_eid,
                                anchor_day=anchor_day,
                                need=need,
                                move_later=later,
                                same_cal=same_cal,
                                relax_khoa=relax_khoa,
                            )
                            if new_slot is not None:
                                _move(move_eid, new_slot)
                                round_moves += 1
                                fixed_pair = True
                                break
                        if fixed_pair:
                            break
                    if fixed_pair:
                        break
            moved_total += round_moves
            if round_moves == 0:
                break
        after_all, after_sd, _ = _compute_violation_score(current)
        if moved_total > 0 or after_all < before_all:
            logs.append(
                f"Pass polish mọi khóa: vi phạm {before_all:,} → {after_all:,} "
                f"(cùng-ngày {before_sd:,} → {after_sd:,}, đã dời {moved_total} môn)."
            )
        return moved_total

    vio_before_polish, sd_before_polish, _ = _compute_violation_score(current)
    if vio_before_polish > 600:
        _polish_all_cohort_prep_gaps(
            max_rounds=24 if vio_before_polish > 2500 else (18 if vio_before_polish > 1200 else 14)
        )

    spread_m = _lns_spread_peak_days()
    if spread_m > 0:
        logs.append(f"Pass san tải theo ngày: đã dời {spread_m} môn khỏi ngày quá đông.")

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
