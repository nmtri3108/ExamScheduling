#!/usr/bin/env python3
"""Compare algorithm output vs manual reference schedules."""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.diagnostics import (  # noqa: E402
    build_student_cohort_code_map,
    build_student_cohort_map,
    prep_days_required_for_pair,
    prep_gap_violated,
)
from engine.scheduler import prep_hard_gap_days_for_pair  # noqa: E402

ALGO = Path("/Users/tringuyen/Downloads/ket_qua_xep_lich_thi (42).xlsx")
MANUAL_RIENG = Path("/Users/tringuyen/Downloads/2520_ALL_DSThiRieng.xlsx")
MANUAL_CHUNG_DS = Path("/Users/tringuyen/Downloads/2520DanhSachThiChung.xlsx")

PREP_DAY_PER_CREDIT = 0.6
MIN_PREP_DAYS = 1.0
YEAR1_ANCHOR = 25
YEAR1_ALLOW_SAME_DAY = True


def _parse_date(val) -> date | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    s = str(val).strip().replace("\t", "")
    if not s or s.lower() in ("nan", "undefined", ""):
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(val).date()
    except Exception:
        return None


def _norm_malop(s) -> str:
    return re.sub(r"\s+", "", str(s).strip())


def _slot_key(d: date | None, ca: str) -> str | None:
    if d is None:
        return None
    ca = str(ca or "").strip().replace("\t", "")
    if not ca or ca.lower() in ("nan", "undefined"):
        return None
    return f"{d.isoformat()}|{ca}"


def load_manual_rieng() -> pd.DataFrame:
    df = pd.read_excel(MANUAL_RIENG, sheet_name=0, header=6)
    df.columns = [str(c).strip().replace("\t", "") for c in df.columns]
    return df


def load_manual_chung_students() -> pd.DataFrame:
    df = pd.read_excel(MANUAL_CHUNG_DS, sheet_name=0, header=6)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def load_algo_lich() -> pd.DataFrame:
    return pd.read_excel(ALGO, sheet_name="Lich_thi")


def load_algo_students() -> pd.DataFrame:
    return pd.read_excel(ALGO, sheet_name="Theo_sinh_vien")


def manual_rieng_slots() -> dict[str, dict]:
    """MalopHP -> {date, ca, size, ten}"""
    df = load_manual_rieng()
    out = {}
    for _, row in df.iterrows():
        hp = _norm_malop(row.get("Mã học phần", ""))
        if not hp or hp == "nan":
            continue
        d = _parse_date(row.get("Ngày thi"))
        ca = row.get("Ca thi", "")
        sk = _slot_key(d, ca)
        if sk:
            out[hp] = {
                "slot": sk,
                "date": d,
                "ca": str(ca).strip(),
                "size": int(float(row.get("SLSV thi") or 0)),
                "name": str(row.get("Tên lớp học phần", "")),
            }
    return out


def manual_chung_by_malop() -> dict[str, dict]:
    """Aggregate chung: MalopHP -> slot (mode)"""
    df = load_manual_chung_students()
    groups: dict[str, list] = defaultdict(list)
    for _, row in df.iterrows():
        hp = _norm_malop(row.get("Mã lớp học phần", ""))
        d = _parse_date(row.get("Ngày thi"))
        ca = row.get("Ca thi", "")
        sk = _slot_key(d, ca)
        if hp and sk:
            groups[hp].append(sk)
    out = {}
    for hp, slots in groups.items():
        mode = max(set(slots), key=slots.count)
        out[hp] = {"slot": mode, "n_students": len(slots)}
    return out


def algo_slots_by_malop() -> dict[str, dict]:
    df = load_algo_lich()
    out = {}
    for _, row in df.iterrows():
        hp = _norm_malop(row.get("MalopHP", ""))
        d = _parse_date(row.get("Ngay_thi"))
        ca = row.get("Ky_hieu_ca") or row.get("So_ca")
        sk = _slot_key(d, str(ca))
        if hp and sk:
            out[hp] = {"slot": sk, "date": d, "ca": str(ca)}
    return out


def count_prep_violations_student_schedule(
    rows: list[tuple[date, float, str]],
    cohort_codes: dict[str, str] | None = None,
) -> dict:
    """rows: (exam_date, credits, exam_key) sorted per student."""
    cohort_codes = cohort_codes or {}
    total = same_day = y1_cross_day = 0

    def _is_y1(sid: str) -> bool:
        code = cohort_codes.get(sid, "")
        return code.endswith(str(YEAR1_ANCHOR)) if code else False

    for sid, entries in rows.items():
        ordered = sorted(entries, key=lambda x: x[0])
        y1 = _is_y1(sid)
        for i in range(1, len(ordered)):
            prev_d, prev_tc, _ = ordered[i - 1]
            curr_d, curr_tc, _ = ordered[i]
            same = prev_d == curr_d
            required = max(MIN_PREP_DAYS, max(prev_tc, curr_tc) * PREP_DAY_PER_CREDIT)
            actual = (curr_d - prev_d).days
            if same and y1 and YEAR1_ALLOW_SAME_DAY:
                continue
            if actual + 1e-9 < required:
                total += 1
                if actual <= 0:
                    same_day += 1
                if y1 and not same and required > 0:
                    y1_cross_day += 1
    return {
        "total_pairs": total,
        "same_day_pairs": same_day,
        "year1_cross_day_pairs": y1_cross_day,
    }


def student_schedules_from_algo() -> dict[str, list]:
    df = load_algo_students()
    code_map = {}
    per_sv: dict[str, list] = defaultdict(list)
    for _, row in df.iterrows():
        sid = str(row.get("Ma_sinh_vien", "")).strip()
        hp = _norm_malop(row.get("MalopHP", ""))
        d = _parse_date(row.get("Ngay_thi"))
        tc = float(row.get("So_tin_chi") or 0)
        nk = str(row.get("Nien_khoa", "")).strip()
        if nk:
            code_map[sid] = nk
        if sid and d:
            per_sv[sid].append((d, tc, hp))
    return per_sv, code_map


def student_schedules_from_manual_chung() -> dict[str, list]:
    df = load_manual_chung_students()
    per_sv: dict[str, list] = defaultdict(list)
    code_map = {}
    for _, row in df.iterrows():
        sid = str(row.get("Số thẻ SV", "")).strip()
        hp = _norm_malop(row.get("Mã lớp học phần", ""))
        d = _parse_date(row.get("Ngày thi"))
        if sid and d:
            per_sv[sid].append((d, 2.0, hp))  # credits unknown in DS; assume 2
    return per_sv, code_map


def day_load(rows: dict[str, list]) -> dict[str, int]:
    by_day: dict[str, int] = defaultdict(int)
    for entries in rows.values():
        for d, _, _ in entries:
            by_day[d.isoformat()] += 1
    return dict(by_day)


def summarize_load(by_day: dict[str, int], label: str) -> None:
    if not by_day:
        print(f"  {label}: no data")
        return
    loads = list(by_day.values())
    s = pd.Series(loads)
    print(
        f"  {label}: days={len(loads)}, total={sum(loads):,}, "
        f"max={max(loads):,}, avg={s.mean():.0f}, std={s.std():.0f}, cv={s.std()/s.mean():.3f}"
    )


def compare_slots():
    rieng = manual_rieng_slots()
    chung = manual_chung_by_malop()
    manual = {**rieng, **chung}
    algo = algo_slots_by_malop()

    common = set(manual) & set(algo)
    only_m = set(manual) - set(algo)
    only_a = set(algo) - set(manual)

    match = sum(1 for hp in common if manual[hp]["slot"] == algo[hp]["slot"])
    mismatch = [(hp, manual[hp]["slot"], algo[hp]["slot"]) for hp in common if manual[hp]["slot"] != algo[hp]["slot"]]

    print("=== SO SÁNH Ô THI (MalopHP) ===")
    print(f"  Manual có lịch: {len(manual)} (riêng {len(rieng)}, chung {len(chung)})")
    print(f"  Algo có lịch:   {len(algo)}")
    print(f"  Giao nhau:      {len(common)}")
    print(f"  Trùng ô (date+ca): {match} ({100*match/max(1,len(common)):.1f}%)")
    print(f"  Khác ô:         {len(mismatch)}")
    print(f"  Chỉ manual:     {len(only_m)}")
    print(f"  Chỉ algo:        {len(only_a)}")

    if mismatch[:15]:
        print("\n  Mẫu khác ô (tối đa 15):")
        for hp, m, a in mismatch[:15]:
            print(f"    {hp}: manual={m} | algo={a}")

    # Date-only match (ignore session)
    date_match = 0
    for hp in common:
        md = manual[hp]["slot"].split("|")[0]
        ad = algo[hp]["slot"].split("|")[0]
        if md == ad:
            date_match += 1
    print(f"  Trùng ngày (bỏ qua ca): {date_match} ({100*date_match/max(1,len(common)):.1f}%)")


def main():
    print("=== KPI TỪ FILE XUẤT THUẬT TOÁN ===")
    kpi = pd.read_excel(ALGO, sheet_name="KPI")
    for _, row in kpi.iterrows():
        print(f"  {row['Chi_so']}: {row['Gia_tri']}")

    compare_slots()

    print("\n=== VI PHẠM NGHỈ ÔN (ước lượng đơn giản) ===")
    algo_sv, algo_codes = student_schedules_from_algo()
    manual_sv, _ = student_schedules_from_manual_chung()
    print(f"  Algo — SV có ≥1 môn: {len(algo_sv)}")
    a_vio = count_prep_violations_student_schedule(algo_sv, algo_codes)
    print(f"    Cặp thiếu ôn: {a_vio['total_pairs']:,} (cùng ngày: {a_vio['same_day_pairs']:,})")

    print(f"  Manual chung only — SV: {len(manual_sv)}")
    m_vio = count_prep_violations_student_schedule(manual_sv)
    print(f"    Cặp thiếu ôn: {m_vio['total_pairs']:,} (cùng ngày: {m_vio['same_day_pairs']:,})")

    print("\n=== TẢI THEO NGÀY (số lượt thi SV) ===")
    summarize_load(day_load(algo_sv), "Algo (all students)")
    summarize_load(day_load(manual_sv), "Manual chung only")

    # Algo by MalopHP day load (exam size)
    df = load_algo_lich()
    by_day_exam: dict[str, int] = defaultdict(int)
    for _, row in df.iterrows():
        d = _parse_date(row.get("Ngay_thi"))
        if d:
            by_day_exam[d.isoformat()] += 1
    summarize_load(by_day_exam, "Algo (số môn/ngày)")


if __name__ == "__main__":
    main()
