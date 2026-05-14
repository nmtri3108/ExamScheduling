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


def markdown_canh_bao_hoc_phan_thieu_ngay_on(hard_errors: list[str]) -> str:
    """Một khối markdown cho `st.error` — gom chữ dùng ở nhiều tab."""
    if not hard_errors:
        return ""
    intro = (
        "**Cảnh báo nặng — ngày ôn theo học phần (7 ký tự đầu mã lớp học phần):** "
        "Với từng nhóm học phần, nếu **hơn 10%** sinh viên vẫn bị xếp thi hai môn liên tiếp "
        "mà **không có ngày nghỉ ôn** giữa hai môn, lịch coi là rủi ro cao. "
        "Có thể tăng «Số ngày ôn / 1 tín chỉ», giảm «Tối đa môn / SV / ngày», hoặc kéo dài đợt thi.\n\n"
    )
    return intro + "\n".join(f"- {msg}" for msg in hard_errors)
