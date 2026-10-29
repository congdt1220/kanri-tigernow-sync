#!/usr/bin/env python3
"""Daily report trên kanri.jemshcm.vn.

Chạy:  py daily_report.py 2026-10-29
Kết quả in ra màn hình và ghi daily_report_YYYY-MM-DD.json.
Dùng trong code khác:
  from daily_report import get_daily_report
  report = get_daily_report("2026-10-29")
"""
import json
import sys
from datetime import date, datetime

from kanri_api import KanriApi, KanriError

DAILY_REPORT_PATH = "/api/api/DailyReports"


def _as_date(value) -> str:
    """Chuẩn hóa ngày truyền vào thành YYYY-MM-DD."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise KanriError(f"Ngày không hợp lệ: {value}. Dùng YYYY-MM-DD, ví dụ 2026-10-29.")


def get_daily_report(report_date, api=None) -> dict:
    """GET /api/api/DailyReports?date=YYYY-MM-DD. report_date là ngày cần lấy."""
    if api is None:
        api = KanriApi()
    day = _as_date(report_date)
    return api.get(DAILY_REPORT_PATH, params={"date": day})


def print_daily_report(report: dict) -> None:
    lines = report.get("lines") or []
    day = str(report.get("date") or "")[:10]
    print(
        f"{day}\t{report.get('shainName')}\t{report.get('status')}\t"
        f"{report.get('totalHours')}h\t{len(lines)} dòng"
    )
    print("pjNo\tpjName\ttaskId\ttaskName\thours\tstatus\tnote")
    for line in lines:
        print(
            f"{line.get('pjNo')}\t{line.get('pjName')}\t{line.get('taskId')}\t"
            f"{line.get('taskName')}\t{line.get('jisekiTime')}\t{line.get('status')}\t"
            f"{line.get('note') or ''}"
        )


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Dùng: py daily_report.py 2026-10-29")
    try:
        report = get_daily_report(sys.argv[1])
    except KanriError as e:
        sys.exit(f"Lỗi: {e}")
    day = _as_date(sys.argv[1])
    path = f"daily_report_{day}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print_daily_report(report)
    print(f"Đã lưu {path}")


if __name__ == "__main__":
    main()
