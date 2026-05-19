"""Kiểm tra lịch thi / DSSV có khớp kế hoạch thi theo Khoa_lop* (4 ký tự cuối MalopHP)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

from .diagnostics import exam_plan_date_bounds, khoa_nhom_from_malop
from .io import load_schedule_window
from .models import Exam
from .plan_windows import KhoaLopWindow, load_khoa_lop_plan


@dataclass
class PlanVerifyRow:
    ma_ca_thi: str
    malophp: str
    khoa_lop: str
    ngay_thi: date
    plan_start: date | None
    plan_end: date | None
    status: str  # ok | outside_window | no_plan | mixed_khoa_lop | no_malop
    ten_mon: str = ""


def malop_suffix(malophp: str) -> str:
    """4 ký tự cuối MalopHP = Khoa_lop* trong kế hoạch thi (cùng Khoa_nhom)."""
    return khoa_nhom_from_malop(malophp)


def split_malophp_cell(cell: object) -> List[str]:
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return []
    return [p.strip() for p in str(cell).split(",") if p.strip()]


def summarize_plan_windows(plan: Dict[str, KhoaLopWindow]) -> pd.DataFrame:
    rows = [
        {
            "Khoa_lop": w.khoa_lop,
            "Khoa": w.khoa,
            "Ngay_BD": w.start_date.isoformat(),
            "Ngay_ket_thuc": w.end_date.isoformat(),
            "So_tuan_thi": w.weeks,
            "Xong": w.done,
        }
        for w in plan.values()
    ]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(["Ngay_BD", "Khoa_lop"]).reset_index(drop=True)


def compare_global_vs_per_cohort(plan_path: str | Path) -> dict:
    plan = load_khoa_lop_plan(plan_path)
    global_win = load_schedule_window(plan_path)
    windows = {(w.start_date, w.end_date) for w in plan.values()}
    return {
        "global_start": global_win.start_date,
        "global_end": global_win.end_date,
        "distinct_cohort_windows": len(windows),
        "cohort_count": len(plan),
        "app_uses_per_cohort_windows": global_win.has_per_cohort_windows,
    }


def _exam_from_malop_row(malops: List[str]) -> Exam:
    return Exam(
        exam_id="_verify",
        course_id="",
        course_name="",
        exam_type="theory",
        section_ids=malops,
        credits=0.0,
        student_ids=[],
    )


def _verify_status_for_exam(
    malops: List[str],
    ngay_d: date,
    plan: Dict[str, KhoaLopWindow],
    global_start: date,
    global_end: date,
) -> Tuple[str, str, date | None, date | None]:
    suffixes = sorted({malop_suffix(m) for m in malops if malop_suffix(m)})
    if not suffixes:
        return "no_malop", "", None, None
    if len(suffixes) > 1:
        pseudo = _exam_from_malop_row(malops)
        from .models import ScheduleWindow
        from .plan_windows import plan_to_date_map

        win = ScheduleWindow(
            start_date=global_start,
            end_date=global_end,
            khoa_lop_windows=plan_to_date_map(plan),
        )
        bounds = exam_plan_date_bounds(pseudo, win)
        if bounds is None:
            return "mixed_no_overlap", ",".join(suffixes), None, None
        p0, p1 = bounds
        if p0 <= ngay_d <= p1:
            return "ok", ",".join(suffixes), p0, p1
        return "outside_window", ",".join(suffixes), p0, p1
    kl = suffixes[0]
    w = plan.get(kl)
    if w is None:
        return "no_plan", kl, None, None
    ok = w.start_date <= ngay_d <= w.end_date
    return (
        "ok" if ok else "outside_window",
        kl,
        w.start_date,
        w.end_date,
    )


def verify_exam_schedule_rows(
    schedule_df: pd.DataFrame,
    plan: Dict[str, KhoaLopWindow],
    *,
    malop_col: str = "MalopHP",
    date_col: str = "Ngay_thi",
    exam_col: str = "Ma_ca_thi",
    name_col: str = "Ten_mon",
    global_start: date | None = None,
    global_end: date | None = None,
) -> Tuple[List[PlanVerifyRow], pd.DataFrame]:
    if global_start is None or global_end is None:
        starts = [w.start_date for w in plan.values()]
        ends = [w.end_date for w in plan.values()]
        global_start = min(starts) if starts else date.today()
        global_end = max(ends) if ends else global_start

    results: List[PlanVerifyRow] = []
    for _, row in schedule_df.iterrows():
        malops = split_malophp_cell(row.get(malop_col))
        ngay_raw = row.get(date_col)
        ngay = pd.to_datetime(ngay_raw, errors="coerce")
        if pd.isna(ngay):
            continue
        ngay_d = ngay.date() if isinstance(ngay, datetime) else ngay
        status, kl, p0, p1 = _verify_status_for_exam(
            malops, ngay_d, plan, global_start, global_end
        )
        results.append(
            PlanVerifyRow(
                ma_ca_thi=str(row.get(exam_col, "")),
                malophp=", ".join(malops),
                khoa_lop=kl,
                ngay_thi=ngay_d,
                plan_start=p0,
                plan_end=p1,
                status=status,
                ten_mon=str(row.get(name_col, "") or ""),
            )
        )
    detail = pd.DataFrame(
        [
            {
                "Ma_ca_thi": r.ma_ca_thi,
                "MalopHP": r.malophp,
                "Khoa_lop_4": r.khoa_lop,
                "Ngay_thi": r.ngay_thi.isoformat(),
                "Ke_hoach_BD": r.plan_start.isoformat() if r.plan_start else "",
                "Ke_hoach_KT": r.plan_end.isoformat() if r.plan_end else "",
                "Trang_thai": r.status,
                "Ten_mon": r.ten_mon,
            }
            for r in results
        ]
    )
    return results, detail


def verify_registrations_against_plan(
    reg_path: str | Path,
    plan: Dict[str, KhoaLopWindow],
) -> pd.DataFrame:
    df = pd.read_excel(reg_path, sheet_name=0)
    if "MalopHP" not in df.columns:
        raise ValueError(f"File đăng ký thiếu MalopHP: {list(df.columns)}")
    rows = []
    for malop in df["MalopHP"].astype(str).str.strip().unique():
        kl = malop_suffix(malop)
        win = plan.get(kl)
        rows.append(
            {
                "MalopHP": malop,
                "Khoa_lop_4": kl,
                "Co_trong_ke_hoach": "Có" if win else "Không",
                "Ke_hoach_BD": win.start_date.isoformat() if win else "",
                "Ke_hoach_KT": win.end_date.isoformat() if win else "",
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["Co_trong_ke_hoach", "Khoa_lop_4", "MalopHP"]
    )


def build_verify_report(
    plan_path: str | Path,
    schedule_path: str | Path | None = None,
    schedule_df: pd.DataFrame | None = None,
    reg_path: str | Path | None = None,
) -> dict:
    plan = load_khoa_lop_plan(plan_path)
    cmp = compare_global_vs_per_cohort(plan_path)
    report: dict = {
        "plan_summary": cmp,
        "plan_windows": summarize_plan_windows(plan),
    }
    if schedule_df is None and schedule_path:
        schedule_df = pd.read_excel(schedule_path, sheet_name="Lich_thi")
    if schedule_df is not None:
        _, detail = verify_exam_schedule_rows(
            schedule_df,
            plan,
            global_start=cmp["global_start"],
            global_end=cmp["global_end"],
        )
        report["schedule_checks"] = detail
        report["schedule_status_counts"] = detail["Trang_thai"].value_counts().to_dict()
        bad = detail[~detail["Trang_thai"].isin(("ok",))]
        report["violations_count"] = len(bad)
        report["violations_sample"] = bad.head(30)
    if reg_path:
        reg_df = verify_registrations_against_plan(reg_path, plan)
        report["registration_missing_plan"] = reg_df[
            reg_df["Co_trong_ke_hoach"] == "Không"
        ]
        report["registration_missing_count"] = len(report["registration_missing_plan"])
    return report
