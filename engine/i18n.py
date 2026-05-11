"""Chuỗi tiếng Việt và ánh xạ hiển thị cho giao diện / xuất file."""
from __future__ import annotations

from typing import Dict

# Mã nội bộ → nhãn hiển thị
PHUONG_THUC_GIAI: Dict[str, str] = {
    "": "—",
    "empty": "Trống (không có môn)",
    "greedy": "Tham lam (DSATUR)",
    "greedy+lns": "Tham lam + cải tiến LNS",
    "greedy+cpsat": "Tham lam + tối ưu ràng buộc (SAT)",
    "greedy+lns+cpsat": "Tham lam + LNS + tối ưu SAT",
}

LOAI_HINH_THI: Dict[str, str] = {
    "theory": "Lý thuyết",
    "oral": "PBL / Vấn đáp / Đồ án",
    "computer": "Trắc nghiệm máy tính",
}


def hien_thi_phuong_thuc(method: str) -> str:
    return PHUONG_THUC_GIAI.get(method.strip(), method)


def hien_thi_loai_hinh(exam_type: str) -> str:
    return LOAI_HINH_THI.get(exam_type, exam_type)
