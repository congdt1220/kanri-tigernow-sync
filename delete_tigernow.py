#!/usr/bin/env python3
"""Xóa mọi timelog trên màn hình Timesheet của một tuần.

Chạy:  py delete_tigernow.py 2026-10-29 --dry-run
       py delete_tigernow.py 2026-10-29 --yes

Ngày bất kỳ trong tuần. Hàm đọc danh sách showAll của tuần đó rồi gọi
GET /Timesheet/DeleteTimelog/{id} cho từng dòng.
"""
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from daily_report import _as_date
from kanri_api import KanriError
from tigernow_api import BASE, login

DELETE_PATH = "/Timesheet/DeleteTimelog/{timelog_id}"


def _week_url(day: datetime) -> str:
    return BASE + "/Timesheet?date=" + quote(day.strftime("%d/%m/%Y"), safe="") + "&showAll=true"


def list_timelogs(session: requests.Session, day: datetime) -> list:
    """Mọi dòng đang hiện trên màn hình Timesheet của tuần chứa day."""
    page = session.get(_week_url(day), timeout=30)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")
    table = soup.find(id="deparment_table")
    if table is None:
        return []
    rows = []
    seen = set()
    for tr in table.find_all("tr")[1:]:
        link = tr.find("a", href=re.compile(r"/Timesheet/DeleteTimelog/"))
        if link is None:
            continue
        timelog_id = link["href"].rstrip("/").split("/")[-1]
        if timelog_id in seen:
            continue
        seen.add(timelog_id)
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        rows.append({
            "id": timelog_id,
            "date": cells[0] if len(cells) > 0 else "",
            "project": cells[2] if len(cells) > 2 else "",
            "task": cells[5] if len(cells) > 5 else "",
            "hours": cells[6] if len(cells) > 6 else "",
            "note": cells[9] if len(cells) > 9 else "",
        })
    return rows


def delete_timelog(session: requests.Session, timelog_id: str) -> requests.Response:
    """GET /Timesheet/DeleteTimelog/{id}. Trang này trả 302 khi xóa xong."""
    session.headers["Referer"] = BASE + "/Timesheet"
    return session.get(
        BASE + DELETE_PATH.format(timelog_id=timelog_id),
        timeout=30,
        headers={"Accept": "text/html,application/xhtml+xml"},
    )


def delete_all_timelogs(report_date, dry_run=False) -> list:
    """Xóa hết timelog trên màn hình tuần chứa report_date (YYYY-MM-DD).

    dry_run=True chỉ liệt kê, không xóa.
    """
    day = datetime.strptime(_as_date(report_date), "%Y-%m-%d")
    session = requests.Session()
    login(session)
    rows = list_timelogs(session, day)
    results = []
    for index, row in enumerate(rows):
        item = dict(row)
        if dry_run:
            item["status"] = "dry-run"
            item["detail"] = ""
            results.append(item)
            continue
        response = delete_timelog(session, row["id"])
        item["status"] = "ok" if response.ok else "error"
        item["detail"] = f"HTTP {response.status_code}"
        results.append(item)
        if index < len(rows) - 1:
            time.sleep(0.3)
    return results


def print_results(results: list) -> None:
    ok = sum(1 for item in results if item["status"] in ("ok", "dry-run"))
    print(f"{ok}/{len(results)} timelog")
    for item in results:
        print(
            f"{item['status']}\t{item['date']}\t{item['hours']}h\t"
            f"{item['project']}\t{item['task']}\t{item['note']}"
        )
        if item["status"] == "error":
            print(f"\t{item['detail']}")


def main() -> None:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if not args:
        sys.exit("Dùng: py delete_tigernow.py 2026-10-29 [--dry-run | --yes]")
    dry_run = "--yes" not in sys.argv
    try:
        results = delete_all_timelogs(args[0], dry_run=dry_run)
    except KanriError as exc:
        sys.exit(f"Lỗi: {exc}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print_results(results)
    if dry_run and results:
        print("Chưa xóa. Thêm --yes để xóa các dòng trên.")


if __name__ == "__main__":
    main()
