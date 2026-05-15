from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional


@dataclass
class Registration:
    student_id: str
    student_name: str
    section_id: str
    course_name: str
    credits: float
    # Theo bảng mã: 1=Tự luận, 2=Trắc nghiệm, 3=Vấn đáp — None = suy từ tên môn
    exam_format: int | None = None


@dataclass
class Exam:
    exam_id: str
    course_id: str
    course_name: str
    exam_type: str  # "theory" | "oral" | "computer" — đồng bộ với exam_format
    section_ids: List[str]
    credits: float
    student_ids: List[str]
    exam_format: int = 1  # 1=Tự luận, 2=Trắc nghiệm, 3=Vấn đáp (đồng bộ phân phòng)
    course_prefix_7: str = ""  # 7 ký tự trái MalopHP — cùng mã nhiều ca → cùng buổi
    priority: int = 0  # >0: ưu tiên xếp cuối đợt (PBL/đồ án)
    prep_days: float = 0.0  # số ngày ôn khuyến nghị

    @property
    def size(self) -> int:
        return len(self.student_ids)


@dataclass
class Room:
    room_id: str
    location: str
    capacity: int
    room_type: str = "any"   # any | theory | computer (mở rộng tương lai)


@dataclass
class Invigilator:
    invigilator_id: str
    full_name: str
    max_sessions_per_day: int = 2
    max_sessions_total: int = 9999


@dataclass
class ScheduleWindow:
    start_date: date
    end_date: date
    sessions_per_day: int = 2

    @property
    def total_days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def total_slots(self) -> int:
        return self.total_days * self.sessions_per_day


@dataclass
class ScheduledExam:
    exam_id: str
    course_id: str
    course_name: str
    exam_date: date
    session: int
    session_label: str = ""
    room_ids: List[str] = field(default_factory=list)
    invigilator_ids: List[str] = field(default_factory=list)
    # Cùng thứ tự room_ids: nhóm sinh viên từng phòng & mã ghép theo quy ước nghiệp vụ.
    room_student_groups: List[List[str]] = field(default_factory=list)
    room_split_codes: List[str] = field(default_factory=list)


@dataclass
class PrepViolation:
    student_id: str
    student_name: str
    earlier_exam: str
    later_exam: str
    required_days: float
    actual_days: float
    # Môn thi sau (cần khoảng ôn) — dùng để gom theo 7 ký tự đầu MalopHP
    later_exam_id: str = ""


@dataclass
class SolveStats:
    """Thống kê tóm tắt sau khi giải để hiển thị KPI cho người dùng."""
    method: str = ""               # "greedy" | "greedy+cpsat" | "cpsat-only"
    feasible: bool = False
    elapsed_seconds: float = 0.0
    num_exams: int = 0
    num_students: int = 0
    num_slots: int = 0
    num_conflicts: int = 0
    slots_used: int = 0
    days_used: int = 0
    relaxations: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class SolveResult:
    scheduled: List[ScheduledExam]
    stats: SolveStats
    violations: List[PrepViolation] = field(default_factory=list)
