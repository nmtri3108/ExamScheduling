#!/usr/bin/env python3
"""Chạy solver trên data/ và in KPI (dùng kiểm tra sau tối ưu)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.diagnostics import build_student_cohort_code_map, build_student_cohort_map, compute_kpi
from engine.io import build_exams, load_registrations, load_rooms, load_schedule_window
from engine.scheduler import detect_prep_violations, solve

DATA = ROOT / "data"
DL = Path("/Users/tringuyen/Downloads")
REG = (
    DATA / "DSSV_2510_xep_lich_thi_M.xlsx"
    if (DATA / "DSSV_2510_xep_lich_thi_M.xlsx").exists()
    else DATA / "DSSV_2510_xep_lich_thi.xlsx"
    if (DATA / "DSSV_2510_xep_lich_thi.xlsx").exists()
    else DL / "DSSV_2510_xep_lich_thi_M.xlsx"
)
PLAN = (
    DATA / "Ke_hoach_thi.xlsx"
    if (DATA / "Ke_hoach_thi.xlsx").exists()
    else DL / "Ke_hoach_thi.xlsx"
)
ROOMS = (
    DATA / "Phong_thi (1).xlsx"
    if (DATA / "Phong_thi (1).xlsx").exists()
    else DATA / "Phong_thi.xlsx"
    if (DATA / "Phong_thi.xlsx").exists()
    else DL / "Phong_thi (1).xlsx"
)

SESSION_LABELS = ["2C1", "2C2", "2C3", "2C4", "1A1", "1P1", "3C1", "3C2", "3C3", "3C4", "3C5", "3C6"]
ALLOWED_BY_TYPE = {
    "theory": [0, 1, 2, 3],
    "oral": [4, 5],
    "computer": [6, 7, 8, 9, 10, 11],
}
SESSION_HALF = [0, 0, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1]


def main():
    if not REG.exists():
        print(f"Missing {REG}")
        return 1
    window = load_schedule_window(PLAN)
    regs = load_registrations(REG)
    rooms = load_rooms(ROOMS) if ROOMS and ROOMS.exists() else []
    exams, student_ref, cohort, codes = build_exams(
        regs,
        prep_day_per_credit=0.6,
        max_exam_size=1500,
        khoa_lop_windows=window.khoa_lop_windows or None,
    )
    codes = build_student_cohort_code_map(exams, registrations=regs, year1_anchor=25)
    cohort = build_student_cohort_map(exams, student_cohort_codes=codes, year1_anchor=25)
    allowed = {e.exam_id: ALLOWED_BY_TYPE.get(e.exam_type, list(range(12))) for e in exams}

    t0 = time.time()
    result = solve(
        exams=exams,
        window=window,
        rooms=rooms,
        allowed_sessions_by_exam_id=allowed,
        session_labels=SESSION_LABELS,
        session_half=SESSION_HALF,
        prep_day_per_credit=0.6,
        min_prep_days=1.0,
        max_exams_per_day=2,
        solver_time_limit_seconds=120.0,
        optimize_objective=True,
        balance_weight=0.12,
        soft_slot_cap=1100,
        lns_iterations=10,
        spread_prep_factor=2.6,
        student_cohort=cohort,
        student_cohort_codes=codes,
        year1_cohort_anchor=25,
        year1_allow_same_day=True,
    )
    elapsed = time.time() - t0
    scheduled = result.scheduled
    vios = detect_prep_violations(
        scheduled,
        exams,
        student_ref,
        prep_day_per_credit=0.6,
        min_prep_days=1.0,
        student_cohort=cohort,
        student_cohort_codes=codes,
        year1_cohort_anchor=25,
        year1_allow_same_day=True,
    )
    kpi = compute_kpi(
        scheduled,
        exams,
        window,
        vios,
        student_cohort=cohort,
        student_cohort_codes=codes,
        year1_cohort_anchor=25,
        year1_allow_same_day=True,
    )
    loads = [v for _, v in kpi.by_day_load]
    import statistics

    cv = statistics.stdev(loads) / statistics.mean(loads) if len(loads) > 1 else 0
    print(f"Method: {result.stats.method} | {elapsed:.1f}s | placed {len(scheduled)}/{len(exams)}")
    print(f"Prep violations: {kpi.prep_violation_count:,} | same-day: {kpi.same_day_violation_count:,}")
    print(f"Year1 prep: {kpi.prep_violation_count_year1:,} pairs | {kpi.prep_violation_students_year1:,} students")
    print(f"Days: {kpi.days_used} | slots: {kpi.slots_used} | max SV/slot: {kpi.max_students_per_slot}")
    print(f"Day load CV: {cv:.3f} | max day: {max(loads):,} | avg: {statistics.mean(loads):.0f}")
    if result.stats.relaxations:
        print("Relaxations:")
        for r in result.stats.relaxations[-8:]:
            print(f"  - {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
