#!/usr/bin/env python3
"""
Thiết lập môi trường ExamScheduling (macOS, Windows, Linux).

Chạy từ thư mục gốc repo:
  macOS / Linux:  python3 setup.py
  Windows (CMD):  python setup.py
  Windows (nếu có py launcher):  py -3 setup.py

Script sẽ:
  1. Kiểm tra phiên bản Python (>= 3.10)
  2. Tạo .venv nếu chưa có
  3. Cài dependencies từ requirements.txt
  4. In lệnh chạy Streamlit

Không cần cài pip thủ công — dùng python -m pip trong venv.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
MIN_PYTHON = (3, 10)


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def venv_pip() -> list[str]:
    py = venv_python()
    return [str(py), "-m", "pip"]


def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        ver = ".".join(map(str, sys.version_info[:3]))
        need = ".".join(map(str, MIN_PYTHON))
        print(f"Lỗi: Cần Python >= {need}, hiện tại là {ver}.", file=sys.stderr)
        print("  macOS: brew install python@3.12  hoặc tải từ https://www.python.org/downloads/", file=sys.stderr)
        print(
            "  Windows: cài từ https://www.python.org/downloads/ "
            "(chọn thêm Python vào biến môi trường PATH khi cài).",
            file=sys.stderr,
        )
        sys.exit(1)


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd or ROOT)
    if r.returncode != 0:
        sys.exit(r.returncode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Thiết lập môi trường và (tuỳ chọn) chạy app Streamlit."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Sau khi setup xong thì chạy luôn ứng dụng Streamlit.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    check_python_version()
    os.chdir(ROOT)

    if not REQUIREMENTS.is_file():
        print(f"Lỗi: Không tìm thấy {REQUIREMENTS}", file=sys.stderr)
        sys.exit(1)

    if not VENV.is_dir():
        print(f"Tạo virtualenv tại {VENV} …")
        run([sys.executable, "-m", "venv", str(VENV)])
    else:
        print(f"Đã có virtualenv: {VENV}")

    py = venv_python()
    if not py.is_file():
        print(f"Lỗi: Không thấy Python trong venv: {py}", file=sys.stderr)
        sys.exit(1)

    print("Nâng cấp pip …")
    run(venv_pip() + ["install", "--upgrade", "pip"])

    print("Cài dependencies từ requirements.txt …")
    run(venv_pip() + ["install", "-r", str(REQUIREMENTS)])

    print()
    print("— Hoàn tất setup —")
    if sys.platform == "win32":
        activate = ".venv\\Scripts\\activate"
        run_app = ".venv\\Scripts\\streamlit.exe run app.py"
    else:
        activate = "source .venv/bin/activate"
        run_app = ".venv/bin/streamlit run app.py"

    print("Kích hoạt venv (tuỳ chọn):")
    print(f"  {activate}")
    print("Chạy ứng dụng:")
    print(f"  {run_app}")
    print()
    print("Hoặc một lệnh (không cần activate):")
    print(f"  {py} -m streamlit run app.py")

    if args.run:
        print()
        print("Đang chạy ứng dụng Streamlit…")
        run([str(py), "-m", "streamlit", "run", "app.py"])


if __name__ == "__main__":
    main()
