from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd


@dataclass
class ManualPatternProfile:
    """Pattern học từ lịch chia tay để bias greedy score."""

    preferred_session_by_prefix7: Dict[str, int] = field(default_factory=dict)
    weekday_session_bonus: Dict[Tuple[int, int], float] = field(default_factory=dict)
    target_students_per_room_by_exam_format: Dict[int, float] = field(default_factory=dict)
    preferred_zone_by_session_label: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def _safe_read_excel(path: Path, header: int) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_excel(path, sheet_name=0, header=header)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _course_prefix_7(value: object) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    return s[:7] if len(s) >= 7 else s


def build_manual_pattern_profile(
    common_schedule_path: str | Path | None,
    invigilator_schedule_path: str | Path | None,
    session_labels: List[str],
) -> ManualPatternProfile:
    """Đọc 2 file lịch chia tay và trích xuất profile bias cho solver."""
    profile = ManualPatternProfile()
    if not session_labels:
        return profile

    label_to_idx = {str(lbl).strip(): i for i, lbl in enumerate(session_labels)}
    common_path = Path(common_schedule_path) if common_schedule_path else None
    inv_path = Path(invigilator_schedule_path) if invigilator_schedule_path else None

    # File 1: danh sách thi chung (header thực ở dòng 7 trong data mẫu).
    if common_path and common_path.exists():
        common = _safe_read_excel(common_path, header=6)
        needed = {"Mã lớp học phần", "Ca thi"}
        if needed.issubset(set(common.columns)):
            common = common.dropna(subset=["Mã lớp học phần", "Ca thi"]).copy()
            common["Mã lớp học phần"] = common["Mã lớp học phần"].astype(str).str.strip()
            common["Ca thi"] = common["Ca thi"].astype(str).str.strip()
            common["prefix7"] = common["Mã lớp học phần"].map(_course_prefix_7)
            common = common[
                common["prefix7"].astype(bool) & common["Ca thi"].map(label_to_idx.__contains__)
            ]
            if not common.empty:
                grp = (
                    common.groupby(["prefix7", "Ca thi"])
                    .size()
                    .reset_index(name="n")
                    .sort_values(["prefix7", "n"], ascending=[True, False])
                )
                top = grp.drop_duplicates(subset=["prefix7"], keep="first")
                profile.preferred_session_by_prefix7 = {
                    str(r["prefix7"]): int(label_to_idx[str(r["Ca thi"])])
                    for _, r in top.iterrows()
                    if str(r["Ca thi"]) in label_to_idx
                }
                profile.notes.append(
                    f"Học ca ưu tiên theo học phần từ file tay: {len(profile.preferred_session_by_prefix7)} mã 7 ký tự."
                )

    # File 2: lịch thi giảng viên/phòng (header thực ở dòng 9 trong data mẫu).
    if inv_path and inv_path.exists():
        inv = _safe_read_excel(inv_path, header=8)
        needed2 = {"Ngày thi", "Xuất thi", "Phòng"}
        if needed2.issubset(set(inv.columns)):
            inv = inv.dropna(subset=["Ngày thi", "Xuất thi"]).copy()
            inv["Ngày thi"] = pd.to_datetime(inv["Ngày thi"], errors="coerce", dayfirst=True)
            inv["Xuất thi"] = inv["Xuất thi"].astype(str).str.strip()
            inv["Phòng"] = inv["Phòng"].astype(str).str.strip()
            inv = inv[inv["Ngày thi"].notna() & inv["Xuất thi"].map(label_to_idx.__contains__)]
            if not inv.empty:
                inv["weekday"] = inv["Ngày thi"].dt.weekday
                inv["sess_idx"] = inv["Xuất thi"].map(label_to_idx).astype(int)
                counts = inv.groupby(["weekday", "sess_idx"]).size().to_dict()
                mx = max(counts.values()) if counts else 1
                if mx <= 0:
                    mx = 1
                profile.weekday_session_bonus = {
                    (int(wd), int(si)): float(v) / float(mx)
                    for (wd, si), v in counts.items()
                }
                profile.notes.append(
                    f"Học phân bố thứ/ca từ file tay: {len(profile.weekday_session_bonus)} ô (weekday,session)."
                )

                # Zone theo ca: lấy ký tự đầu mã phòng có tần suất cao nhất.
                inv["zone"] = inv["Phòng"].map(lambda x: str(x).strip()[:1].upper() if str(x).strip() else "")
                zone_df = inv[inv["zone"].astype(bool)]
                if not zone_df.empty:
                    top_zone = (
                        zone_df.groupby(["Xuất thi", "zone"])
                        .size()
                        .reset_index(name="n")
                        .sort_values(["Xuất thi", "n"], ascending=[True, False])
                        .drop_duplicates(subset=["Xuất thi"], keep="first")
                    )
                    profile.preferred_zone_by_session_label = {
                        str(r["Xuất thi"]): str(r["zone"]) for _, r in top_zone.iterrows()
                    }
                    profile.notes.append(
                        f"Học khu phòng theo ca: {len(profile.preferred_zone_by_session_label)} ký hiệu ca có khu ưu tiên."
                    )

                # Mức SV/phòng theo loại thi (suy từ ký hiệu ca trong lịch tay).
                if "SLSV" in inv.columns:
                    inv["SLSV"] = pd.to_numeric(inv["SLSV"], errors="coerce")
                    sl = inv[inv["SLSV"].notna() & (inv["SLSV"] > 0)].copy()
                    if not sl.empty:
                        def _fmt_from_session(lbl: str) -> int:
                            s = str(lbl or "").strip().upper()
                            if s.startswith("3"):
                                return 2  # trắc nghiệm máy
                            if s.startswith("1"):
                                return 3  # vấn đáp / oral
                            return 1      # lý thuyết

                        sl["fmt"] = sl["Xuất thi"].map(_fmt_from_session)
                        med = sl.groupby("fmt")["SLSV"].median().to_dict()
                        profile.target_students_per_room_by_exam_format = {
                            int(k): float(v) for k, v in med.items() if float(v) > 0
                        }
                        if profile.target_students_per_room_by_exam_format:
                            profile.notes.append(
                                "Học mức SV/phòng theo loại thi: "
                                + ", ".join(
                                    f"mã {k}≈{v:.1f}"
                                    for k, v in sorted(
                                        profile.target_students_per_room_by_exam_format.items()
                                    )
                                )
                                + "."
                            )
    return profile
