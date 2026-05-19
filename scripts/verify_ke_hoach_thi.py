#!/usr/bin/env python3
"""CLI: kiểm tra lịch thi / DSSV khớp Ke_hoach_thi.xlsx theo Khoa_lop* (= 4 ký tự cuối MalopHP)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.plan_verify import (  # noqa: E402
    build_verify_report,
    compare_global_vs_per_cohort,
    load_khoa_lop_plan,
    summarize_plan_windows,
    verify_exam_schedule_rows,
)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Kiểm tra Khoa_lop* (kế hoạch thi) = 4 ký tự cuối MalopHP và ngày thi nằm trong khoảng."
    )
    p.add_argument(
        "--plan",
        default="/Users/tringuyen/Downloads/Ke_hoach_thi.xlsx",
        help="File Ke_hoach_thi.xlsx",
    )
    p.add_argument("--schedule", help="File ket_qua_xep_lich_thi.xlsx (sheet Lich_thi)")
    p.add_argument("--dssv", help="File DSSV đăng ký (tùy chọn)")
    p.add_argument("-o", "--output", help="Ghi báo cáo Excel")
    args = p.parse_args()

    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"Không tìm thấy kế hoạch thi: {plan_path}", file=sys.stderr)
        return 1

    cmp = compare_global_vs_per_cohort(plan_path)
    print("=== Kế hoạch thi ===")
    print(f"  Số Khoa_lop*: {cmp['cohort_count']}")
    print(f"  Số cửa sổ ngày khác nhau: {cmp['distinct_cohort_windows']}")
    print(
        f"  App hiện dùng (load_schedule_window): "
        f"{cmp['global_start']} → {cmp['global_end']}"
    )
    if cmp.get("app_uses_per_cohort_windows"):
        print("  Engine: xếp lịch theo từng Khoa_lop* (giao đợt nếu ca gom nhiều lớp).")
    elif cmp["distinct_cohort_windows"] > 1:
        print(
            "  ⚠ File có nhiều đợt thi nhưng chưa nạp Khoa_lop* — kiểm tra cột trong Ke_hoach_thi."
        )

    plan = load_khoa_lop_plan(plan_path)
    wins = summarize_plan_windows(plan)
    print("\n=== Các đợt thi trong kế hoạch (gom theo ngày) ===")
    if not wins.empty:
        g = (
            wins.groupby(["Ngay_BD", "Ngay_ket_thuc"])
            .size()
            .reset_index(name="so_khoa_lop")
            .sort_values("so_khoa_lop", ascending=False)
        )
        for _, r in g.iterrows():
            print(f"  {r['Ngay_BD']} → {r['Ngay_ket_thuc']}: {int(r['so_khoa_lop'])} lớp")

    exit_code = 0
    if args.schedule:
        import pandas as pd

        sched_path = Path(args.schedule)
        df = pd.read_excel(sched_path, sheet_name="Lich_thi")
        _, detail = verify_exam_schedule_rows(df, plan)
        counts = detail["Trang_thai"].value_counts()
        print(f"\n=== Kiểm tra lịch: {sched_path.name} ===")
        for st, n in counts.items():
            print(f"  {st}: {n}")
        bad = detail[detail["Trang_thai"] != "ok"]
        if len(bad):
            exit_code = 2
            print(f"\n  Vi phạm / thiếu kế hoạch ({len(bad)} ca) — mẫu:")
            cols = ["Ma_ca_thi", "Khoa_lop_4", "Ngay_thi", "Ke_hoach_BD", "Ke_hoach_KT", "Trang_thai", "Ten_mon"]
            print(bad[cols].head(15).to_string(index=False))
        else:
            print("  Tất cả ca thi nằm đúng cửa sổ Khoa_lop* tương ứng.")

    if args.dssv:
        from engine.plan_verify import verify_registrations_against_plan

        miss = verify_registrations_against_plan(args.dssv, plan)
        miss = miss[miss["Co_trong_ke_hoach"] == "Không"]
        print(f"\n=== DSSV: MalopHP không có trong kế hoạch ===")
        print(f"  Số mã lớp: {len(miss)}")
        if len(miss):
            exit_code = max(exit_code, 2)
            print(miss.head(10).to_string(index=False))

    if args.output:
        import pandas as pd

        rep = build_verify_report(
            plan_path,
            schedule_path=args.schedule,
            reg_path=args.dssv,
        )
        out = Path(args.output)
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            rep["plan_windows"].to_excel(w, sheet_name="Ke_hoach_tom_tat", index=False)
            if "schedule_checks" in rep:
                rep["schedule_checks"].to_excel(w, sheet_name="Kiem_tra_lich", index=False)
                bad = rep["schedule_checks"][rep["schedule_checks"]["Trang_thai"] != "ok"]
                bad.to_excel(w, sheet_name="Vi_pham", index=False)
            if "registration_missing_plan" in rep:
                rep["registration_missing_plan"].to_excel(
                    w, sheet_name="MalopHP_thieu_ke_hoach", index=False
                )
        print(f"\nĐã ghi báo cáo: {out}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
