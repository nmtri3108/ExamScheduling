"""Bộ giải lịch thi hybrid: Greedy DSATUR ⇒ CP-SAT polish ⇒ cascade-relax.

Sự khác biệt với phiên bản cũ:
- KHÔNG còn reification slot×exam cho ràng buộc sức chứa (đẩy về room assignment).
- max_exams_per_day mã hoá bằng per-(student, day) reification CHỈ cho SV có nguy cơ vượt.
- Soft constraints dùng abs_diff theo cặp xung đột — nhưng có cấp ngưỡng `top_k_pairs`
  để CP-SAT không bị nghẹt với dataset lớn.
- Luôn có warm-start (solution hint) từ Greedy ⇒ CP-SAT chỉ cần cải thiện.
- Nếu CP-SAT timeout / UNKNOWN ⇒ trả về kết quả Greedy thay vì raise.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import timedelta
from math import ceil
from typing import Callable, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .diagnostics import (
    build_conflict_index,
    build_prefix_student_totals,
    enumerate_feasible_slots_for_exam,
    exam_khoa_nhom_keys,
    prep_gap_violated,
    same_course_khoa_nhom_waiver,
)
from .diagnostics import (
    build_student_cohort_map,
    is_year1_anchor_student,
    prep_days_required_for_pair,
    prep_hard_gap_days_for_pair,
    resolve_year1_cohort_anchor,
)
from .heuristic import HeuristicResult, heuristic_to_scheduled, lns_improve, schedule_greedy
from .models import (
    Exam,
    Invigilator,
    PrepViolation,
    Room,
    ScheduleWindow,
    ScheduledExam,
    SolveResult,
    SolveStats,
)


# ---------------------------------------------------------------------------
# CP-SAT polish
# ---------------------------------------------------------------------------

def _build_cpsat_model(
    exams: List[Exam],
    window: ScheduleWindow,
    allowed_sessions_by_exam_id: Dict[str, List[int]],
    conflicts: Dict[Tuple[int, int], int],
    min_prep_days: float,
    max_exams_per_day: int,
    prep_day_per_credit: float,
    fixed_slots: Dict[str, int] | None,
    base_slots: Dict[str, int] | None,
    warm_start: Dict[str, int] | None,
    optimize: bool,
    top_k_prep_pairs: int = 50000,
    allowed_slot_domains: Optional[List[List[int]]] = None,
) -> Tuple[cp_model.CpModel, List[cp_model.IntVar], List[cp_model.IntVar]]:
    model = cp_model.CpModel()
    n = len(exams)
    total_days = window.total_days
    total_slots = window.total_slots
    sessions_per_day = window.sessions_per_day

    slot_vars: List[cp_model.IntVar] = []
    day_vars: List[cp_model.IntVar] = []
    exam_index = {e.exam_id: i for i, e in enumerate(exams)}

    for i, exam in enumerate(exams):
        sessions = allowed_sessions_by_exam_id.get(exam.exam_id, list(range(sessions_per_day)))
        sessions = sorted({int(s) for s in sessions if 0 <= int(s) < sessions_per_day})
        if not sessions:
            raise ValueError(f"Môn '{exam.course_name}' không có ca thi hợp lệ.")
        if allowed_slot_domains is not None:
            allowed = allowed_slot_domains[i]
        else:
            allowed = [d * sessions_per_day + s for d in range(total_days) for s in sessions]
        if not allowed:
            raise ValueError(f"Môn '{exam.course_name}' không có ô thời gian hợp lệ sau lọc ngày.")
        slot_vars.append(
            model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"slot_{i}")
        )
        day_vars.append(model.NewIntVar(0, total_days - 1, f"day_{i}"))
        model.AddDivisionEquality(day_vars[i], slot_vars[i], sessions_per_day)

    # Fixed slots
    if fixed_slots:
        for exam_id, slot in fixed_slots.items():
            if exam_id not in exam_index:
                continue
            slot = int(slot)
            if 0 <= slot < total_slots:
                model.Add(slot_vars[exam_index[exam_id]] == slot)

    # Hard: same slot ⇒ no conflict
    for (i, j), _ in conflicts.items():
        model.Add(slot_vars[i] != slot_vars[j])

    # Khoa_nhom (4 ký tự cuối MalopHP): hai môn khác nhau cùng hậu tố ⇒ khác ngày (ca tách cùng học phần miễn).
    for i in range(n):
        for j in range(i + 1, n):
            if same_course_khoa_nhom_waiver(exams[i], exams[j]):
                continue
            if exam_khoa_nhom_keys(exams[i]) & exam_khoa_nhom_keys(exams[j]):
                model.Add(day_vars[i] != day_vars[j])

    # max_exams_per_day: dùng AddCumulative theo SV — gọn hơn reification per-day.
    if max_exams_per_day > 0:
        student_to_exams: Dict[str, List[int]] = defaultdict(list)
        for i, exam in enumerate(exams):
            for sid in exam.student_ids:
                student_to_exams[sid].append(i)
        # cache interval var theo exam — tái sử dụng giữa các SV
        intervals_by_exam: Dict[int, cp_model.IntervalVar] = {}
        for idx in range(n):
            intervals_by_exam[idx] = model.NewFixedSizeIntervalVar(
                day_vars[idx], 1, f"iv_{idx}"
            )
        for sid, ex_list in student_to_exams.items():
            unique_list = sorted(set(ex_list))
            if len(unique_list) <= max_exams_per_day:
                continue
            ivs = [intervals_by_exam[i] for i in unique_list]
            demands = [1] * len(ivs)
            model.AddCumulative(ivs, demands, max_exams_per_day)

    # Hard min_prep_days (chỉ kích hoạt khi >0)
    hard_req = int(ceil(min_prep_days))
    if hard_req > 0:
        for (i, j), _ in conflicts.items():
            diff = model.NewIntVar(-total_days, total_days, f"d_{i}_{j}")
            abs_diff = model.NewIntVar(0, total_days, f"ad_{i}_{j}")
            model.Add(diff == day_vars[i] - day_vars[j])
            model.AddAbsEquality(abs_diff, diff)
            model.Add(abs_diff >= hard_req)

    penalty_terms = []

    # Soft prep-day penalty — giới hạn top_k cặp xung đột nặng nhất để giữ mô hình gọn.
    # Severity = (req - gap) tuyến tính + bonus riêng cho gap=0 (same-day): mỗi cặp xung đột
    # có một boolean "same_day" được kích hoạt khi abs_diff == 0; trọng số same-day cao hơn
    # ~5x trọng số 1 đơn vị deficit để CP-SAT cũng ưu tiên phá same-day giống greedy/LNS.
    if optimize and prep_day_per_credit > 0 and conflicts:
        sorted_pairs = sorted(conflicts.items(), key=lambda kv: -kv[1])[:top_k_prep_pairs]
        for (i, j), overlap in sorted_pairs:
            # Dùng max tín chỉ của cặp để không "thiệt" môn 4+ tín chỉ khi cặp với môn ít tín chỉ.
            # (Thực tế báo cáo vi phạm cũng tính theo môn "sau", thường là môn cần ôn nhiều hơn.)
            req = int(
                ceil(
                    max(
                        min_prep_days,
                        max(float(exams[i].credits), float(exams[j].credits)) * prep_day_per_credit,
                    )
                )
            )
            if req <= 0:
                continue
            diff = model.NewIntVar(-total_days, total_days, f"sp_d_{i}_{j}")
            abs_diff = model.NewIntVar(0, total_days, f"sp_ad_{i}_{j}")
            model.Add(diff == day_vars[i] - day_vars[j])
            model.AddAbsEquality(abs_diff, diff)
            lack = model.NewIntVar(0, req, f"lack_{i}_{j}")
            model.Add(lack >= req - abs_diff)
            pair_w = max(1, overlap // 5)
            penalty_terms.append(lack * pair_w)
            # Same-day boolean: kích hoạt khi abs_diff == 0. Hệ số ~5x deficit weight để
            # giảm same-day là ưu tiên hơn là chỉ thu hẹp deficit.
            same_day_bool = model.NewBoolVar(f"sd_{i}_{j}")
            model.Add(abs_diff == 0).OnlyEnforceIf(same_day_bool)
            model.Add(abs_diff >= 1).OnlyEnforceIf(same_day_bool.Not())
            penalty_terms.append(same_day_bool * (5 * pair_w))

    # PBL push-late
    if optimize:
        max_day = total_days - 1
        for i, exam in enumerate(exams):
            if exam.priority <= 0:
                continue
            penalty = model.NewIntVar(0, max_day, f"pbl_{i}")
            model.Add(penalty == max_day - day_vars[i])
            penalty_terms.append(penalty * exam.priority)

    # Repair distance to base_slots
    if optimize and base_slots:
        for exam in exams:
            if fixed_slots and exam.exam_id in fixed_slots:
                continue
            prev = base_slots.get(exam.exam_id)
            if prev is None:
                continue
            i = exam_index[exam.exam_id]
            delta = model.NewIntVar(-total_slots, total_slots, f"rd_{i}")
            abs_delta = model.NewIntVar(0, total_slots, f"rad_{i}")
            model.Add(delta == slot_vars[i] - int(prev))
            model.AddAbsEquality(abs_delta, delta)
            penalty_terms.append(abs_delta)

    if optimize and penalty_terms:
        model.Minimize(sum(penalty_terms))

    # Warm-start hints
    if warm_start:
        for exam_id, slot in warm_start.items():
            i = exam_index.get(exam_id)
            if i is None:
                continue
            slot = int(slot)
            if 0 <= slot < total_slots:
                model.AddHint(slot_vars[i], slot)
                model.AddHint(day_vars[i], slot // sessions_per_day)

    return model, slot_vars, day_vars


def _trang_thai_cp_sat_vn(status: int) -> str:
    return {
        cp_model.OPTIMAL: "tối ưu toàn cục",
        cp_model.FEASIBLE: "chấp nhận được",
        cp_model.INFEASIBLE: "vô nghiệm",
        cp_model.UNKNOWN: "hết thời gian / chưa rõ",
        cp_model.MODEL_INVALID: "mô hình không hợp lệ",
    }.get(status, f"mã trạng thái {status}")


def _solve_cpsat(
    model: cp_model.CpModel,
    slot_vars: List[cp_model.IntVar],
    time_limit: float,
) -> Tuple[int, Optional[List[int]]]:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(max(1.0, time_limit))
    solver.parameters.num_search_workers = 8
    solver.parameters.log_search_progress = False
    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return status, [int(solver.Value(v)) for v in slot_vars]
    return status, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def solve(
    exams: List[Exam],
    window: ScheduleWindow,
    rooms: List[Room],
    allowed_sessions_by_exam_id: Dict[str, List[int]] | None = None,
    session_labels: List[str] | None = None,
    session_half: List[int] | None = None,
    prep_day_per_credit: float = 0.6,
    min_prep_days: float = 0.0,
    max_exams_per_day: int = 2,
    solver_time_limit_seconds: float = 120.0,
    optimize_objective: bool = True,
    fixed_slots: Dict[str, int] | None = None,
    base_slots: Dict[str, int] | None = None,
    auto_relax: bool = True,
    balance_weight: float = 0.12,
    soft_slot_cap: int | None = 1100,
    lns_iterations: int = 10,
    lns_pool_size: int = 250,
    progress_cb: Optional[Callable[[int, str], None]] = None,
    weekend_large_course_min_students: int = 0,
    spread_prep_factor: float = 2.6,
    student_cohort: Dict[str, int] | None = None,
    student_cohort_codes: Dict[str, str] | None = None,
    year1_cohort_anchor: int = 0,
    year1_allow_same_day: bool = True,
) -> SolveResult:
    """Entry point chính.

    Quy trình:
    1. Greedy DSATUR → lịch khả thi đầu tiên.
    2. Nếu còn thời gian và bật optimize: CP-SAT polish với warm-start.
    3. Nếu CP-SAT không cho nghiệm tốt hơn → trả về greedy.
    4. auto_relax: tự nới min_prep_days/max_exams_per_day theo bậc nếu greedy báo unplaced.
    """
    start_time = time.time()
    allowed_sessions_by_exam_id = allowed_sessions_by_exam_id or {}
    progress = progress_cb or (lambda pct, msg: None)

    if not exams:
        return SolveResult(
            scheduled=[],
            stats=SolveStats(method="empty", feasible=True, elapsed_seconds=0.0),
        )

    progress(5, "Đang phân tích xung đột và chuẩn bị bước tham lam…")
    conflicts = build_conflict_index(exams)

    prefix_totals: Dict[str, int] = {}
    if weekend_large_course_min_students > 0:
        prefix_totals = build_prefix_student_totals(exams)

    total_capacity = sum(r.capacity for r in rooms) if rooms else None

    relaxations: List[str] = []
    if window.has_per_cohort_windows:
        n_distinct = len({(a, b) for a, b in window.khoa_lop_windows.values()})
        relaxations.append(
            f"Áp dụng Ke_hoach_thi theo Khoa_lop*: {len(window.khoa_lop_windows)} mã lớp, "
            f"{n_distinct} khoảng ngày đợt thi."
        )

    # ---- Greedy phase ----
    progress(15, "Bước 1/3: đang xếp lịch nhanh bằng thuật toán tham lam (DSATUR)…")
    greedy = schedule_greedy(
        exams=exams,
        window=window,
        allowed_sessions_by_exam_id=allowed_sessions_by_exam_id,
        max_exams_per_day=max_exams_per_day,
        min_prep_days=min_prep_days,
        prep_day_per_credit=prep_day_per_credit,
        total_capacity=total_capacity,
        fixed_slots=fixed_slots,
        base_slots=base_slots,
        balance_weight=balance_weight,
        soft_slot_cap=soft_slot_cap,
        session_half=session_half,
        weekend_large_course_min_students=weekend_large_course_min_students,
        prefix_student_totals=prefix_totals,
        spread_prep_factor=spread_prep_factor,
        student_cohort=student_cohort,
        student_cohort_codes=student_cohort_codes,
        year1_cohort_anchor=year1_cohort_anchor,
        year1_allow_same_day=year1_allow_same_day,
    )
    relaxations.extend(greedy.relaxations)

    # Không chạy lại greedy với min_prep_days=0 — dễ tạo hàng nghìn vi phạm prep (req=3, thực tế=0–1).
    if greedy.unplaced and auto_relax:
        relaxations.append(
            f"Còn {len(greedy.unplaced)} môn chưa xếp sau nới trong bước tham lam — "
            "cân nhắc mở rộng đợt thi hoặc giảm max môn/SV/ngày (không tự bỏ ngày ôn toàn cục)."
        )

    progress(45, f"Bước tham lam xong: đã đặt {len(greedy.assignment)}/{len(exams)} môn.")

    method = "greedy"
    final_assignment = dict(greedy.assignment)
    lns_logs: List[str] = []

    # ---- LNS post-improvement (rẻ và hiệu quả, luôn chạy khi optimize=True) ----
    if optimize_objective and lns_iterations > 0 and final_assignment:
        progress(
            50,
            f"Bước 2/3: đang cải tiến LNS trên {lns_pool_size} môn vi phạm ngày ôn nhiều nhất…",
        )
        try:
            improved, lns_logs = lns_improve(
                assignment=final_assignment,
                exams=exams,
                window=window,
                allowed_sessions_by_exam_id=allowed_sessions_by_exam_id,
                max_exams_per_day=max_exams_per_day,
                min_prep_days=min_prep_days,
                prep_day_per_credit=prep_day_per_credit,
                iterations=lns_iterations,
                pool_size=lns_pool_size,
                soft_slot_cap=soft_slot_cap,
                fixed_slots=fixed_slots,
                session_half=session_half,
                progress_cb=progress,
                weekend_large_course_min_students=weekend_large_course_min_students,
                prefix_student_totals=prefix_totals,
                student_cohort=student_cohort,
                student_cohort_codes=student_cohort_codes,
                year1_cohort_anchor=year1_cohort_anchor,
                year1_allow_same_day=year1_allow_same_day,
            )
            if improved and improved != final_assignment:
                final_assignment = improved
                method = "greedy+lns"
            # Vòng LNS thứ hai khi vẫn còn hàng nghìn vi phạm (thường sau nới ôn khóa sau).
            if final_assignment and len(final_assignment) >= len(exams) * 0.98:
                tmp_sched = heuristic_to_scheduled(
                    HeuristicResult(assignment=final_assignment, unplaced=[], relaxations=[]),
                    exams,
                    window,
                    session_labels=session_labels,
                )
                sid_names = {
                    sid: ""
                    for ex in exams
                    for sid in ex.student_ids
                }
                vio_n = len(
                    detect_prep_violations(
                        tmp_sched,
                        exams,
                        sid_names,
                        prep_day_per_credit=prep_day_per_credit,
                        min_prep_days=min_prep_days,
                        student_cohort=student_cohort,
                        student_cohort_codes=student_cohort_codes,
                        year1_cohort_anchor=year1_cohort_anchor,
                        year1_allow_same_day=year1_allow_same_day,
                    )
                )
                if vio_n > 1800:
                    progress(
                        65,
                        f"Bước 2b/3: LNS bổ sung — còn {vio_n:,} vi phạm ngày ôn…",
                    )
                    improved2, lns_logs2 = lns_improve(
                        assignment=final_assignment,
                        exams=exams,
                        window=window,
                        allowed_sessions_by_exam_id=allowed_sessions_by_exam_id,
                        max_exams_per_day=max_exams_per_day,
                        min_prep_days=min_prep_days,
                        prep_day_per_credit=prep_day_per_credit,
                        iterations=max(8, lns_iterations),
                        pool_size=min(400, lns_pool_size + 100),
                        soft_slot_cap=soft_slot_cap,
                        fixed_slots=fixed_slots,
                        session_half=session_half,
                        progress_cb=progress,
                        weekend_large_course_min_students=weekend_large_course_min_students,
                        prefix_student_totals=prefix_totals,
                        student_cohort=student_cohort,
                        student_cohort_codes=student_cohort_codes,
                        year1_cohort_anchor=year1_cohort_anchor,
                        year1_allow_same_day=year1_allow_same_day,
                    )
                    lns_logs.extend(lns_logs2)
                    if improved2:
                        final_assignment = improved2
                    # Vòng 3: polish ôn khi vẫn còn nhiều SV thiếu ngày nghỉ (đặc biệt gap=1).
                    if final_assignment and len(final_assignment) >= len(exams) * 0.98:
                        tmp_sched2 = heuristic_to_scheduled(
                            HeuristicResult(
                                assignment=final_assignment, unplaced=[], relaxations=[]
                            ),
                            exams,
                            window,
                            session_labels=session_labels,
                        )
                        vio_n2 = len(
                            detect_prep_violations(
                                tmp_sched2,
                                exams,
                                sid_names,
                                prep_day_per_credit=prep_day_per_credit,
                                min_prep_days=min_prep_days,
                                student_cohort=student_cohort,
                                student_cohort_codes=student_cohort_codes,
                                year1_cohort_anchor=year1_cohort_anchor,
                                year1_allow_same_day=year1_allow_same_day,
                            )
                        )
                        if vio_n2 > 1000:
                            progress(
                                68,
                                f"Bước 2c/3: LNS polish ôn — còn {vio_n2:,} vi phạm…",
                            )
                            improved3, lns_logs3 = lns_improve(
                                assignment=final_assignment,
                                exams=exams,
                                window=window,
                                allowed_sessions_by_exam_id=allowed_sessions_by_exam_id,
                                max_exams_per_day=max_exams_per_day,
                                min_prep_days=min_prep_days,
                                prep_day_per_credit=prep_day_per_credit,
                                iterations=max(12, lns_iterations),
                                pool_size=min(450, lns_pool_size + 150),
                                soft_slot_cap=soft_slot_cap,
                                fixed_slots=fixed_slots,
                                session_half=session_half,
                                progress_cb=progress,
                                weekend_large_course_min_students=weekend_large_course_min_students,
                                prefix_student_totals=prefix_totals,
                                student_cohort=student_cohort,
                                student_cohort_codes=student_cohort_codes,
                                year1_cohort_anchor=year1_cohort_anchor,
                                year1_allow_same_day=year1_allow_same_day,
                            )
                            lns_logs.extend(lns_logs3)
                            if improved3:
                                final_assignment = improved3
        except Exception as exc:  # noqa: BLE001
            relaxations.append(f"Bỏ qua bước LNS: {exc}")

    elapsed = time.time() - start_time
    remaining = max(0.0, solver_time_limit_seconds - elapsed)
    final_slots: Optional[List[int]] = None

    # CP-SAT polish chỉ chạy khi instance đủ nhỏ (tránh dựng model 10M+ vars).
    # Ngưỡng dựa trên: số môn × số slot ≤ 500_000, số xung đột ≤ 60_000.
    # Ngoài ra mỗi môn phải có ≥1 ô thời gian hợp lệ sau lọc thứ trong tuần.
    cpsat_domains: Optional[List[List[int]]] = None
    _cpsat_size_ok = (
        optimize_objective
        and remaining > 10.0
        and len(exams) * window.total_slots <= 500_000
        and len(conflicts) <= 60_000
    )
    if _cpsat_size_ok:
        doms: List[List[int]] = []
        dom_ok = True
        for ex in exams:
            slots = enumerate_feasible_slots_for_exam(
                ex,
                window,
                allowed_sessions_by_exam_id,
                fixed_slots,
                weekend_large_min_students=weekend_large_course_min_students,
                prefix_totals=prefix_totals,
                relax_weekday_rule=False,
            )
            if not slots:
                slots = enumerate_feasible_slots_for_exam(
                    ex,
                    window,
                    allowed_sessions_by_exam_id,
                    fixed_slots,
                    weekend_large_min_students=weekend_large_course_min_students,
                    prefix_totals=prefix_totals,
                    relax_weekday_rule=True,
                )
            if not slots:
                dom_ok = False
                break
            doms.append(sorted(set(slots)))
        if dom_ok:
            cpsat_domains = doms

    cpsat_eligible = _cpsat_size_ok and cpsat_domains is not None

    # ---- CP-SAT polish phase ----
    cpsat_used = False
    if cpsat_eligible:
        try:
            progress(75, f"Bước 3/3: tối ưu ràng buộc (SAT), còn tối đa {int(remaining)} giây…")
            model, slot_vars, _ = _build_cpsat_model(
                exams=exams,
                window=window,
                allowed_sessions_by_exam_id=allowed_sessions_by_exam_id,
                conflicts=conflicts,
                min_prep_days=min_prep_days,
                max_exams_per_day=max_exams_per_day,
                prep_day_per_credit=prep_day_per_credit,
                fixed_slots=fixed_slots,
                base_slots=base_slots,
                warm_start=final_assignment,
                optimize=True,
                allowed_slot_domains=cpsat_domains,
            )
            progress(76, "Đang chạy tối ưu ràng buộc (SAT)…")
            status, values = _solve_cpsat(model, slot_vars, remaining)
            if values is not None:
                cpsat_used = True
                method = f"{method}+cpsat" if method != "greedy" else "greedy+cpsat"
                for i, exam in enumerate(exams):
                    final_assignment[exam.exam_id] = values[i]
                progress(88, f"Tối ưu ràng buộc (SAT) xong: {_trang_thai_cp_sat_vn(status)}.")
            else:
                progress(85, "Bước tối ưu ràng buộc (SAT) không cho nghiệm mới — giữ lịch sau tham lam/LNS.")
        except Exception as exc:  # noqa: BLE001
            relaxations.append(f"Lỗi hoặc bỏ qua bước SAT: {exc}")
    elif optimize_objective and not cpsat_eligible:
        relaxations.append(
            "Đã bỏ bước tối ưu SAT vì bài toán quá lớn (trên 500 nghìn cặp môn×ô thời gian "
            "hoặc trên 60 nghìn cặp xung đột) — chỉ dùng thuật toán tham lam cho phù hợp thực tế."
        )

    # ---- Build ScheduledExam ----
    scheduled: List[ScheduledExam] = []
    for exam in exams:
        slot = final_assignment.get(exam.exam_id)
        if slot is None:
            continue
        day_idx = int(slot) // window.sessions_per_day
        sess_idx = int(slot) % window.sessions_per_day
        scheduled.append(
            ScheduledExam(
                exam_id=exam.exam_id,
                course_id=exam.course_id,
                course_name=exam.course_name,
                exam_date=window.start_date + timedelta(days=day_idx),
                session=sess_idx + 1,
                session_label=(
                    session_labels[sess_idx]
                    if session_labels and sess_idx < len(session_labels)
                    else ""
                ),
            )
        )
    scheduled.sort(key=lambda x: (x.exam_date, x.session, x.course_name))

    elapsed_total = time.time() - start_time
    stats = SolveStats(
        method=method,
        feasible=len(scheduled) == len(exams),
        elapsed_seconds=elapsed_total,
        num_exams=len(exams),
        num_students=len({sid for e in exams for sid in e.student_ids}),
        num_slots=window.total_slots,
        num_conflicts=len(conflicts),
        slots_used=len({(s.exam_date.isoformat(), s.session) for s in scheduled}),
        days_used=len({s.exam_date for s in scheduled}),
        relaxations=list(dict.fromkeys(relaxations)),
        notes=[
            "Bước tham lam: sóng khóa (MalopHP) — khóa mới nhất trong đợt xếp trước, khóa cũ sau "
            "(tương đương ưu tiên sinh viên năm thấp / ít môn chồng lịch trước).",
            f"Bước tham lam đã đặt {len(greedy.assignment)}/{len(exams)} môn.",
            *lns_logs,
            ("Đã chạy thêm bước tối ưu SAT." if cpsat_used else "Không chạy bước SAT (đủ tốt hoặc hết thời gian)."),
        ],
    )
    stats.unplaced_exam_ids = list(greedy.unplaced)
    if greedy.unplaced:
        stats.notes.append(
            f"CẢNH BÁO NGHIÊM TRỌNG: {len(greedy.unplaced)} môn KHÔNG có lịch thi — "
            "sinh viên đăng ký các môn này sẽ không biết ngày giờ thi. "
            "Xem tab Tổng quan → «Môn chưa xếp» và sheet Excel «Mon_chua_xep»."
        )
    else:
        stats.notes.append("Đã xếp đủ 100% môn (có thể đã nới một số ràng buộc — xem log relaxations).")

    result = SolveResult(scheduled=scheduled, stats=stats, violations=[])
    result.unplaced_diagnostics = list(getattr(greedy, "unplaced_diagnostics", None) or [])
    return result


def detect_prep_violations(
    scheduled: List[ScheduledExam],
    exams: List[Exam],
    student_name_map: Dict[str, str],
    prep_day_per_credit: float = 0.6,
    min_prep_days: float = 0.0,
    student_cohort: Dict[str, int] | None = None,
    student_cohort_codes: Dict[str, str] | None = None,
    year1_cohort_anchor: int = 0,
    year1_allow_same_day: bool = True,
) -> List[PrepViolation]:
    exam_by_id = {e.exam_id: e for e in exams}
    code_map = student_cohort_codes or {}
    cohort_map = student_cohort if student_cohort else build_student_cohort_map(
        exams, student_cohort_codes=code_map or None
    )
    anchor = resolve_year1_cohort_anchor(year1_cohort_anchor, exams, code_map)
    student_exams = defaultdict(list)
    for item in scheduled:
        exam = exam_by_id[item.exam_id]
        for sid in exam.student_ids:
            student_exams[sid].append((item.exam_date, item.exam_id))

    violations: List[PrepViolation] = []
    for sid, entries in student_exams.items():
        ordered = sorted(entries, key=lambda x: x[0])
        y1 = anchor > 0 and is_year1_anchor_student(sid, code_map, anchor)
        for i in range(1, len(ordered)):
            prev_date, prev_exam_id = ordered[i - 1]
            curr_date, curr_exam_id = ordered[i]
            prev_exam = exam_by_id[prev_exam_id]
            curr_exam = exam_by_id[curr_exam_id]
            same_day = prev_date == curr_date
            required = round(
                prep_days_required_for_pair(
                    prev_exam, curr_exam, prep_day_per_credit, min_prep_days
                ),
                2,
            )
            min_gap = prep_hard_gap_days_for_pair(
                prev_exam,
                curr_exam,
                prep_day_per_credit,
                min_prep_days,
                year1_allow_same_day=year1_allow_same_day,
                for_year1_student=y1,
                same_calendar_day=same_day,
            )
            actual = (curr_date - prev_date).days
            if min_gap <= 0 and same_day and y1 and year1_allow_same_day:
                continue
            if prep_gap_violated(
                actual,
                prev_exam,
                curr_exam,
                prep_day_per_credit,
                min_prep_days,
                year1_allow_same_day=year1_allow_same_day,
                for_year1_student=y1,
                same_calendar_day=same_day,
            ):
                violations.append(
                    PrepViolation(
                        student_id=sid,
                        student_name=student_name_map.get(sid, ""),
                        earlier_exam=exam_by_id[prev_exam_id].course_name,
                        later_exam=curr_exam.course_name,
                        required_days=required,
                        actual_days=float(actual),
                        later_exam_id=curr_exam_id,
                        student_cohort=int(cohort_map.get(str(sid), 0) or 0),
                        student_cohort_code=code_map.get(str(sid), ""),
                    )
                )
    return violations
