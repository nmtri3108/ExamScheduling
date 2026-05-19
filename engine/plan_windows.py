"""Đọc kế hoạch thi theo Khoa_lop* (= 4 ký tự cuối MalopHP)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, Tuple

import pandas as pd


@dataclass(frozen=True)
class KhoaLopWindow:
    khoa_lop: str
    khoa: str
    start_date: date
    end_date: date
    weeks: float | None = None
    done: str = ""


def _normalize_khoa_lop(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if s.isdigit():
        return s.zfill(4)
    return s


def _find_khoa_lop_column(columns: Iterable[str]) -> str | None:
    for col in columns:
        folded = "".join(c.lower() if c.isalnum() else "" for c in str(col))
        if folded in ("khoalop", "khoalopstar") or folded.startswith("khoalop"):
            return col
    return None


def load_khoa_lop_plan(plan_path: str | Path) -> Dict[str, KhoaLopWindow]:
    """Đọc Ke_hoach_thi.xlsx: map Khoa_lop* → (Ngày BD, Ngày kết thúc)."""
    path = Path(plan_path)
    df = pd.read_excel(path, sheet_name=0)
    kl_col = _find_khoa_lop_column(df.columns)
    if not kl_col:
        raise ValueError(
            f"File kế hoạch thi không có cột Khoa_lop*: {list(df.columns)}"
        )
    start_col = next(
        (c for c in df.columns if "ngày bd" in str(c).lower() or str(c).strip() == "Ngày BD"),
        None,
    )
    end_col = next(
        (c for c in df.columns if "kết thúc" in str(c).lower() or "ket thuc" in str(c).lower()),
        None,
    )
    if not start_col or not end_col:
        raise ValueError(f"Thiếu cột ngày BD / kết thúc: {list(df.columns)}")

    khoa_col = next((c for c in df.columns if str(c).strip() in ("Khóa", "Khoa")), None)
    weeks_col = next((c for c in df.columns if "tuần" in str(c).lower()), None)
    done_col = next((c for c in df.columns if str(c).strip().lower() == "xong"), None)

    out: Dict[str, KhoaLopWindow] = {}
    for rec in df.to_dict("records"):
        kl = _normalize_khoa_lop(rec.get(kl_col))
        if not kl:
            continue
        start = pd.to_datetime(rec.get(start_col), errors="coerce")
        end = pd.to_datetime(rec.get(end_col), errors="coerce")
        if pd.isna(start) or pd.isna(end):
            continue
        khoa_raw = rec.get(khoa_col) if khoa_col else ""
        if khoa_raw is not None and not pd.isna(khoa_raw):
            khoa_s = _normalize_khoa_lop(khoa_raw)[:2]
        else:
            khoa_s = kl[:2]
        weeks = rec.get(weeks_col) if weeks_col else None
        try:
            weeks_f = float(weeks) if weeks is not None and not pd.isna(weeks) else None
        except (TypeError, ValueError):
            weeks_f = None
        done = ""
        if done_col:
            v = rec.get(done_col)
            done = "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()
        win = KhoaLopWindow(
            khoa_lop=kl,
            khoa=khoa_s,
            start_date=start.date(),
            end_date=end.date(),
            weeks=weeks_f,
            done=done,
        )
        if kl in out and (out[kl].start_date, out[kl].end_date) != (win.start_date, win.end_date):
            raise ValueError(
                f"Khoa_lop {kl} có nhiều khoảng ngày khác nhau trong file kế hoạch."
            )
        out[kl] = win
    if not out:
        raise ValueError("Không đọc được dòng Khoa_lop* hợp lệ từ file kế hoạch thi.")
    return out


def plan_to_date_map(plan: Dict[str, KhoaLopWindow]) -> Dict[str, Tuple[date, date]]:
    return {k: (w.start_date, w.end_date) for k, w in plan.items()}
