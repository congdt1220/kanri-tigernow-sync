#!/usr/bin/env python3
"""Đồng bộ Daily Report Kanri sang TigerNow.

Chạy:  py sync_kanri_tigernow.py 2026-10-29
       py sync_kanri_tigernow.py 2026-10-27 2026-10-29
       py sync_kanri_tigernow.py 2026-10-27 2026-10-29 --dry-run

Mỗi dòng Kanri có jisekiTime > 0 được ghi một timelog:
  Hours = jisekiTime
  Note  = #{taskId} {taskName} ({jisekiTime}h) {workPecent}%
          thêm " Done" khi workPecent = 100.
          pjName 納品作業 hoặc 会議 thì Note chỉ là note của dòng đó.
  Project = chỉ PRJ2607393 | SD5 - JEMS5-Shogun Customize.
           納品作業 -> task "2 | Giao hàng".
           会議 -> task "005 | Meeting".
           Dự án khác tìm theo pjName; không thấy thì #242584 | G722 出荷入力
           và ghi các task đó, gom theo pjName, vào report.txt.
"""
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from daily_report import _as_date, get_daily_report
from kanri_api import KanriError
from tigernow_api import BASE, ENDPOINT, TOKEN_FIELD, _norm, get_token, login, set_date

EMPTY_TIMELOG = "00000000-0000-0000-0000-000000000000"
# Chỉ đăng ký trên dự án này.
TARGET_PROJECT = "PRJ2607393"
# Task TigerNow dùng khi không khớp được pjName.
FALLBACK_TASK_MARK = "#242584"
REPORT_PATH = "report.txt"
ISSUE_BASE = "http://153.126.182.137/issues/"
# pjName Kanri -> (đầu tên task, phần tên) trên TigerNow.
SPECIAL_TASKS = {
    "納品作業": ("2 |", "Giao hàng"),
    "会議": ("005 |", "Meeting"),
}


def build_content(line: dict) -> str:
    """Nội dung Note đăng lên TigerNow."""
    pj_name = str(line.get("pjName") or "").strip()
    if pj_name in SPECIAL_TASKS:
        return str(line.get("note") or "").strip()
    hours = _fmt(line.get("jisekiTime"))
    percent = _fmt(line.get("workPecent"))
    task_name = str(line.get("taskName") or "").strip()
    text = f"#{line.get('taskId')} {task_name} ({hours}h) {percent}%"
    try:
        if float(line.get("workPecent") or 0) == 100:
            text += " Done"
    except (TypeError, ValueError):
        pass
    return " ".join(text.split())


def _fmt(value) -> str:
    number = float(value or 0)
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _load_projects(session: requests.Session, day: datetime) -> list:
    """Project trên bảng chấm công của tuần chứa day, kèm task và activity mặc định."""
    url = BASE + "/Timesheet?date=" + quote(day.strftime("%d/%m/%Y"), safe="")
    page = session.get(url, timeout=30)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")
    found, seen = [], set()
    for link in soup.select("#timesheet_table a"):
        onclick = link.get("onclick") or ""
        if "OpenTimeLog" not in onclick:
            continue
        project_id = onclick.split("'")[1]
        if project_id in seen:
            continue
        seen.add(project_id)
        row = link.find_parent("tr")
        name = row.find("td").get_text(" ", strip=True) if row else ""
        if TARGET_PROJECT not in name:
            continue
        found.append(_load_project_form(session, project_id, name, day))
    return found


def _selected(select) -> str:
    if select is None:
        return ""
    option = select.find("option", selected=True)
    if option is not None and (option.get("value") or "").strip():
        return option["value"].strip()
    return ""


def _load_project_form(session, project_id: str, name: str, day: datetime) -> dict:
    response = session.get(
        BASE + "/Timesheet/EditTimeLog",
        params={"id": project_id, "timeLogId": EMPTY_TIMELOG, "date": day.strftime("%d/%m/%Y")},
        timeout=30,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    tasks = []
    task_select = soup.find("select", {"name": "TaskId"})
    if task_select:
        for option in task_select.find_all("option"):
            value = (option.get("value") or "").strip()
            if value and set(value) != {"0", "-"}:
                tasks.append({"taskId": value, "taskName": option.get_text(" ", strip=True)})
    user = soup.find("input", {"name": "UserId"})
    return {
        "id": project_id,
        "name": name,
        "tasks": tasks,
        "activityId": _selected(soup.find("select", {"name": "ActivityId"})),
        "positionId": _selected(soup.find("select", {"name": "User.Position"})),
        "userId": (user.get("value") if user else "") or "",
    }


def _name_score(pj_name: str, text: str) -> int:
    """Điểm khớp pjName. Khớp cả chuỗi cao hơn khớp một đoạn đầu đủ dài."""
    if not pj_name or not text:
        return 0
    if pj_name in text:
        return 100
    for size in range(len(pj_name) - 1, 3, -1):
        if pj_name[:size] in text:
            return 50 + size
    return 0


def find_tigernow_task(line: dict, projects: list):
    """Trả về (project, task) theo pjName. None nếu không thấy dự án."""
    pj_name = _norm(line.get("pjName") or "")
    pj_no = str(line.get("pjNo") or "")
    task_name = line.get("taskName") or ""
    head = task_name.strip().split(" ", 1)[0]
    best = None
    for project in projects:
        project_score = _name_score(pj_name, _norm(project["name"]))
        for task in project["tasks"]:
            raw = task["taskName"]
            score = _name_score(pj_name, _norm(raw))
            if pj_no and pj_no in raw:
                score += 80
            if score == 0 and project_score:
                score = project_score
            if score <= 0:
                continue
            if head and len(head) >= 2 and head in raw:
                score += 25
            rank = (score, _name_score(_norm(task_name), _norm(raw)))
            if best is None or rank > best[0]:
                best = (rank, project, task)
    if best is None:
        return None, None
    return best[1], best[2]


def find_special_task(pj_name: str, projects: list):
    """Task cố định cho 納品作業 và 会議. None nếu pjName không thuộc hai loại này."""
    spec = SPECIAL_TASKS.get(str(pj_name or "").strip())
    if spec is None:
        return None
    prefix, tail = _norm(spec[0]), _norm(spec[1])
    for project in projects:
        for task in project["tasks"]:
            name = _norm(task["taskName"])
            if name.startswith(prefix) and tail in name:
                return project, task
    return None, None


def fallback_tigernow_task(projects: list):
    """Task #242584 trên SD5 - JEMS5-Shogun Customize."""
    mark = _norm(FALLBACK_TASK_MARK)
    for project in projects:
        for task in project["tasks"]:
            if mark in _norm(task["taskName"]):
                return project, task
    return None, None


def _payload(line: dict, project: dict, task: dict, day: datetime) -> dict:
    payload = {
        "culture": "en",
        "TimeLogId": EMPTY_TIMELOG,
        "UserId": project["userId"],
        "ProjectId": project["id"],
        "ActivityId": project["activityId"],
        "User.Position": project["positionId"],
        "TaskId": task["taskId"],
        "Hours": _fmt(line.get("jisekiTime")),
        "Note": build_content(line),
        "IsRemoteWork": "false",
        "IsOvertime": "false",
        "IsChargeable": "false",
    }
    set_date(payload, day)
    return payload


def _days(date_from, date_to) -> list:
    """Các ngày từ date_from đến date_to, cả hai đầu. date_to bỏ trống thì một ngày."""
    start = datetime.strptime(_as_date(date_from), "%Y-%m-%d")
    end = datetime.strptime(_as_date(date_to), "%Y-%m-%d") if date_to else start
    if end < start:
        raise KanriError("Ngày kết thúc phải không nhỏ hơn ngày bắt đầu.")
    days = []
    day = start
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    return days


def _sync_day(session, projects, day: datetime, dry_run: bool, posted: bool) -> tuple:
    """Đăng Daily Report của một ngày. Trả về (kết quả, đã gửi ít nhất một dòng)."""
    report = get_daily_report(day.date())
    lines = [line for line in (report.get("lines") or []) if float(line.get("jisekiTime") or 0) > 0]
    label = day.strftime("%Y-%m-%d")
    if not lines:
        return [{
            "date": label, "status": "empty", "hours": "0", "pjName": "",
            "content": "", "projectName": None, "detail": "không có giờ",
        }], posted
    results = []
    for line in lines:
        content = build_content(line)
        pj_name = str(line.get("pjName") or "").strip()
        special = find_special_task(pj_name, projects)
        used_fallback = False
        if special is None and pj_name in SPECIAL_TASKS:
            project, task = None, None
        elif special is not None:
            project, task = special
        else:
            project, task = find_tigernow_task(line, projects)
            if project is None or task is None:
                project, task = fallback_tigernow_task(projects)
                used_fallback = True
        item = {
            "date": label,
            "pjName": line.get("pjName"),
            "taskId": line.get("taskId"),
            "taskName": " ".join(str(line.get("taskName") or "").split()),
            "hours": _fmt(line.get("jisekiTime")),
            "content": content,
            "projectName": project["name"] if project else None,
            "tigerTask": task["taskName"] if task else None,
            "usedFallback": used_fallback,
        }
        if project is None or task is None:
            item["status"] = "skip"
            label_task = " ".join(SPECIAL_TASKS.get(pj_name, ())) or FALLBACK_TASK_MARK
            item["detail"] = f"không thấy task {label_task}"
            results.append(item)
            continue
        payload = _payload(line, project, task, day)
        if dry_run:
            item["status"] = "dry-run"
            item["detail"] = payload["DateFromIns"]
            results.append(item)
            continue
        if posted:
            time.sleep(1)
        payload[TOKEN_FIELD] = get_token(session, payload["returnUrl"])
        response = session.post(BASE + ENDPOINT, data=payload, timeout=30)
        item["status"] = "ok" if response.ok else "error"
        item["detail"] = f"HTTP {response.status_code} {response.text[:200]}"
        results.append(item)
        if response.ok:
            posted = True
    return results, posted


def sync_kanri_to_tigernow(date_from, date_to=None, *, dry_run=False) -> list:
    """Đồng bộ Daily Report từ date_from đến date_to (YYYY-MM-DD).

    date_to bỏ trống thì chỉ một ngày. dry_run=True chỉ đối chiếu, không gửi.
    """
    days = _days(date_from, date_to)
    session = requests.Session()
    session.headers["X-Requested-With"] = "XMLHttpRequest"
    login(session)
    projects = _load_projects(session, days[0])
    if not projects:
        raise KanriError(f"Không thấy dự án {TARGET_PROJECT} trên TigerNow.")
    results, posted = [], False
    for day in days:
        day_results, posted = _sync_day(session, projects, day, dry_run, posted)
        results.extend(day_results)
    count = write_missing_report(results)
    print(f"Đã lưu {REPORT_PATH} ({count} task không tìm thấy)")
    return results


def write_missing_report(results: list, path: str = REPORT_PATH) -> int:
    """Gom task không khớp TigerNow theo pjName và ghi report.txt."""
    groups = {}
    order = []
    for item in results:
        if not item.get("usedFallback") or item.get("taskId") in (None, ""):
            continue
        name = str(item.get("pjName") or "").strip() or "(không tên)"
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(item)
    blocks = []
    for name in order:
        lines = [name, ""]
        for item in groups[name]:
            lines.append(
                f"{ISSUE_BASE}{item['taskId']} {item.get('taskName') or ''} ({item['hours']}h)"
            )
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    if text:
        text += "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return sum(len(rows) for rows in groups.values())


def print_results(results: list) -> None:
    rows = [item for item in results if item["status"] != "empty"]
    ok = sum(1 for item in rows if item["status"] in ("ok", "dry-run"))
    print(f"{ok}/{len(rows)} dòng đăng ký được")
    current = None
    for item in results:
        if item.get("date") != current:
            current = item.get("date")
            print(f"--- {current} ---")
        if item["status"] == "empty":
            print("không có giờ")
            continue
        print(
            f"{item['status']}\t{item['hours']}h\t{item['pjName']}\t"
            f"{item['projectName'] or '-'}\t{item['content']}"
        )
        if item.get("tigerTask"):
            note = "task mặc định" if item.get("usedFallback") else ""
            print(f"\t-> {item['tigerTask']}" + (f" ({note})" if note else ""))
        if item["status"] in ("skip", "error"):
            print(f"\t{item['detail']}")


def main() -> None:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if not args or len(args) > 2:
        sys.exit("Dùng: py sync_kanri_tigernow.py 2026-10-27 [2026-10-29] [--dry-run]")
    try:
        results = sync_kanri_to_tigernow(
            args[0], args[1] if len(args) > 1 else None, dry_run="--dry-run" in sys.argv)
    except KanriError as exc:
        sys.exit(f"Lỗi: {exc}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print_results(results)


if __name__ == "__main__":
    main()
