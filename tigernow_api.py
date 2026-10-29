#!/usr/bin/env python3
"""Đăng nhập Tiger Now rồi gọi POST /Timesheet/SaveTimelog (dạng form). Chạy trên máy bạn.

Cài:   pip install requests beautifulsoup4
Chạy:  python tigernow_api.py payload.json --dry-run   # chỉ in ra, không gửi
       python tigernow_api.py payload.json             # gửi thật

Tài khoản điền sẵn bên dưới, có thể ghi đè bằng biến môi trường TIGER_USER / TIGER_PASS.
Token __RequestVerificationToken được lấy tự động từ trang Timesheet nên không
cần có trong payload.json.
"""
import csv
import difflib
import json
import os
import random
import re
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE = "https://tigernow.aegona.net"
ENDPOINT = "/Timesheet/SaveTimelog"
# Không chia sẻ file này cho người khác vì đang chứa mật khẩu.
DEFAULT_USER = "cong.do@aegona.com"
DEFAULT_PASS = ""
# Kiểm tra lại bằng DevTools > request Login > Payload; sửa nếu khác.
USER_FIELD = os.environ.get("TIGER_USER_FIELD", "Email")
PASS_FIELD = os.environ.get("TIGER_PASS_FIELD", "Password")
TOKEN_FIELD = "__RequestVerificationToken"


def login(session: requests.Session) -> None:
    login_url = f"{BASE}/Account/Login"
    page = session.get(login_url, timeout=30)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")

    form = soup.find("form")
    data = {}
    if form:
        for inp in form.find_all("input"):
            if inp.get("name") and inp.get("type") != "submit":
                data[inp["name"]] = inp.get("value", "")
    data[USER_FIELD] = os.environ.get("TIGER_USER", DEFAULT_USER)
    data[PASS_FIELD] = os.environ.get("TIGER_PASS", DEFAULT_PASS)

    resp = session.post(login_url, data=data, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    if "/Account/Login" in resp.url:
        sys.exit("Đăng nhập thất bại: kiểm tra tài khoản hoặc tên field của form.")


def get_token(session: requests.Session, return_url: str) -> str:
    """Mở trang Timesheet (cùng ngày/dự án) và lấy token chống giả mạo mới."""
    path = return_url.lstrip("~") or "/Timesheet"
    page = session.get(BASE + path, timeout=30)
    page.raise_for_status()
    html = page.text
    soup = BeautifulSoup(html, "html.parser")

    # 1) Input hoặc thẻ bất kỳ có name=__RequestVerificationToken
    for tag in soup.find_all(attrs={"name": TOKEN_FIELD}):
        val = tag.get("value") or tag.get("content")
        if val:
            return val
    # 2) Chuỗi nằm trong script / HTML thô
    patterns = [
        r'name=["\']__RequestVerificationToken["\'][^>]*?value=["\']([^"\']+)',
        r'value=["\']([^"\']+)["\'][^>]*?name=["\']__RequestVerificationToken',
        r'RequestVerificationToken["\']?\s*[:=]\s*["\']([^"\']{20,})',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)

    # Không thấy: lưu trang để chẩn đoán
    with open("timesheet_debug.html", "w", encoding="utf-8") as f:
        f.write(html)
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    print("--- CHẨN ĐOÁN ---")
    print("URL cuối      :", page.url)
    print("Tiêu đề trang :", title)
    print("Là trang login:", bool(soup.find("input", {"type": "password"})))
    print("Số form       :", len(soup.find_all("form")))
    print("Có chữ 'Token' trong HTML:", "Token" in html)
    print("Độ dài HTML   :", len(html))
    print("Đã lưu trang vào timesheet_debug.html")
    sys.exit("Không tìm thấy __RequestVerificationToken trên trang Timesheet.")


def scrape_task_ids(session: requests.Session, return_url: str) -> list:
    """Thử đọc danh sách TaskId từ ô chọn công việc trên trang Timesheet."""
    path = return_url.lstrip("~") or "/Timesheet"
    html = session.get(BASE + path, timeout=30).text
    soup = BeautifulSoup(html, "html.parser")
    ids = []
    for sel in soup.find_all("select", {"name": "TaskId"}):
        for opt in sel.find_all("option"):
            v = (opt.get("value") or "").strip()
            if v and set(v) != {"0", "-"}:
                ids.append(v)
    return ids


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower().replace("đ", "d")
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if not unicodedata.combining(c))  # bỏ dấu tiếng Việt
    return " ".join(s.split())


def _bigrams(s: str) -> set:
    s = s.replace(" ", "")
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def load_tasks() -> list:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks_list.json")
    for a in sys.argv[1:]:
        if a.startswith("--tasks-file="):
            path = a[len("--tasks-file="):]
    if not os.path.exists(path):
        sys.exit(f"Không thấy file danh sách task: {path} "
                 "(đặt tasks_list.json cùng thư mục với script hoặc dùng --tasks-file=...)")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def find_tasks(query: str, tasks: list, top: int = 5) -> list:
    """Trả về [(điểm, taskId, taskName)] giảm dần theo độ giống với query."""
    q = _norm(query)
    qb = _bigrams(q)
    scored = []
    for t in tasks:
        name = _norm(t["taskName"])
        if q in name:
            score = 1.0 - min(len(name), 400) / 100000  # ưu tiên tên ngắn hơn khi hòa
        else:
            coverage = len(qb & _bigrams(name)) / len(qb)
            m = difflib.SequenceMatcher(None, q, name).find_longest_match(
                0, len(q), 0, len(name))
            score = 0.6 * coverage + 0.4 * (m.size / max(len(q), 1))
            score = min(score, 0.99)
        scored.append((score, t["taskId"], t["taskName"]))
    scored.sort(key=lambda x: -x[0])
    return scored[:top]


def print_matches(matches: list) -> None:
    for score, tid, name in matches:
        print(f"  {score:.2f}  {tid}  {name}")


def set_date(payload: dict, d: datetime) -> None:
    """Đặt DateFromIns và returnUrl (date = thứ Hai của tuần, selectdate = ngày chọn)."""
    monday = d - timedelta(days=d.weekday())
    payload["DateFromIns"] = d.strftime("%d/%m/%Y")
    payload["returnUrl"] = (
        "~/Timesheet?date=" + quote(monday.strftime("%d/%m/%Y"), safe="")
        + "&selectdate=" + quote(d.strftime("%d/%m/%Y"), safe="")
        + "&projectid=" + payload["ProjectId"]
    )


def date_range():
    """Đọc --date-from= và --date-to=. Trả về list ngày, hoặc None nếu không dùng."""
    d_from = d_to = None
    for a in sys.argv[1:]:
        if a.startswith("--date-from="):
            d_from = datetime.strptime(a[len("--date-from="):], "%d/%m/%Y")
        elif a.startswith("--date-to="):
            d_to = datetime.strptime(a[len("--date-to="):], "%d/%m/%Y")
    if d_from is None and d_to is None:
        return None
    if d_from is None:
        sys.exit("Thiếu --date-from=dd/mm/yyyy")
    d_to = d_to or d_from
    if d_to < d_from:
        sys.exit("--date-to phải không nhỏ hơn --date-from")
    include_weekend = "--include-weekends" in sys.argv
    days = []
    d = d_from
    while d <= d_to:
        if include_weekend or d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    if not days:
        sys.exit("Khoảng ngày không có ngày làm việc nào (thêm --include-weekends nếu cần).")
    return days


def apply_overrides(payload: dict) -> None:
    """Hỗ trợ: --date=dd/mm/yyyy  --hours=2  "--note=nội dung"."""
    for a in sys.argv[1:]:
        if a.startswith("--date="):
            set_date(payload, datetime.strptime(a[7:], "%d/%m/%Y"))
        elif a.startswith("--task-name="):
            matches = find_tasks(a[len("--task-name="):], load_tasks())
            print("Các task giống nhất:")
            print_matches(matches)
            if not matches or matches[0][0] < 0.5:
                sys.exit("Không có task nào đủ giống (điểm < 0.50). Gõ rõ hơn hoặc dùng "
                         "--find-task= để xem danh sách gợi ý.")
            payload["TaskId"] = matches[0][1]
            print("=> Dùng TaskId:", matches[0][1])
        elif a == "--random-guid":
            payload["TaskId"] = str(uuid.uuid4())
        elif a.startswith("--hours="):
            payload["Hours"] = a[8:]
        elif a.startswith("--note="):
            payload["Note"] = a[7:]


def list_projects(session: requests.Session) -> None:
    """In ra danh sách dự án (ProjectId + tên) đọc từ trang Timesheet."""
    html = session.get(BASE + "/Timesheet", timeout=30).text
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for sel in soup.find_all("select"):
        name = sel.get("name") or sel.get("id") or ""
        if "project" in name.lower():
            for opt in sel.find_all("option"):
                v = (opt.get("value") or "").strip()
                if v and set(v) != {"0", "-"}:
                    found.append((v, opt.get_text(strip=True)))
    if not found:
        with open("timesheet_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
        names = [s.get("name") or s.get("id") for s in soup.find_all("select")]
        print("Không thấy ô chọn dự án trong HTML. Các ô select có trên trang:", names)
        print("Đã lưu trang vào timesheet_debug.html (danh sách có thể tải sau bằng JS).")
        return
    print(f"Tìm thấy {len(found)} dự án:")
    out = []
    for pid, pname in found:
        print(f"  {pid}  {pname}")
        out.append({"ProjectId": pid, "Name": pname})
    with open("projects.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("Đã lưu vào projects.json")


def parse_tables(html: str) -> list:
    """Trả về list các bảng; mỗi bảng là list các dòng; mỗi dòng là list ô (text)."""
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for t in soup.find_all("table"):
        rows = []
        for tr in t.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            # giữ lại các input ẩn/giá trị (ví dụ TimeLogId, TaskId) nằm trong dòng
            hidden = [f"{i.get('name')}={i.get('value')}"
                      for i in tr.find_all("input") if i.get("name") and i.get("value")]
            if cells or hidden:
                rows.append(cells + (["|" + ";".join(hidden)] if hidden else []))
        if rows:
            tables.append(rows)
    return tables


def dump_timesheet(session: requests.Session, payload: dict, days: list) -> None:
    """Tải trang Timesheet của từng tuần trong khoảng ngày, lưu HTML và xuất CSV."""
    weeks = {}
    for d in days:
        monday = d - timedelta(days=d.weekday())
        weeks.setdefault(monday, d)
    out_rows = []
    for monday, d in sorted(weeks.items()):
        url = (BASE + "/Timesheet?date=" + quote(monday.strftime("%d/%m/%Y"), safe="")
               + "&selectdate=" + quote(d.strftime("%d/%m/%Y"), safe="")
               + "&projectid=" + payload["ProjectId"])
        r = session.get(url, timeout=30)
        r.raise_for_status()
        fn = f"timesheet_{monday.strftime('%Y%m%d')}.html"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(r.text)
        tables = parse_tables(r.text)
        print(f"Tuần {monday:%d/%m/%Y}: {len(tables)} bảng, đã lưu {fn}")
        for ti, rows in enumerate(tables):
            for row in rows:
                out_rows.append([monday.strftime("%d/%m/%Y"), ti] + row)
        time.sleep(1)
    with open("timesheet_dump.csv", "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(out_rows)
    print(f"Đã ghi {len(out_rows)} dòng vào timesheet_dump.csv")
    if not out_rows:
        print("Không thấy bảng nào trong HTML: dữ liệu có thể được tải sau bằng JavaScript. "
              "Khi đó cần tìm request JSON trong DevTools > Network (Fetch/XHR).")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    if "--dump-timesheet" in sys.argv:
        if not args:
            sys.exit("Dùng: tigernow_api.py payload.json --dump-timesheet "
                     "--date-from=dd/mm/yyyy --date-to=dd/mm/yyyy")
        with open(args[0], encoding="utf-8") as f:
            payload = json.load(f)
        days = date_range()
        if not days:
            sys.exit("Thiếu --date-from= và --date-to=")
        s = requests.Session()
        s.headers["X-Requested-With"] = "XMLHttpRequest"
        login(s)
        dump_timesheet(s, payload, days)
        return
    for a in sys.argv[1:]:
        if a.startswith("--find-task="):
            print_matches(find_tasks(a[len("--find-task="):], load_tasks(), top=10))
            return
    if "--list-projects" in sys.argv:
        s = requests.Session()
        s.headers["X-Requested-With"] = "XMLHttpRequest"
        login(s)
        list_projects(s)
        return
    if not args:
        sys.exit(__doc__)

    with open(args[0], encoding="utf-8") as f:
        payload = json.load(f)
    payload.pop(TOKEN_FIELD, None)
    apply_overrides(payload)
    pool = payload.pop("TaskIdPool", [])  # chỉ dùng nội bộ, không gửi lên server
    random_task = "--random-task" in sys.argv
    days = date_range()  # None = chỉ một bản ghi theo payload/--date

    if dry_run:
        sample = dict(payload)
        if days:
            set_date(sample, days[0])
            print(f"Sẽ gửi {len(days)} bản ghi cho các ngày:")
            print("  " + ", ".join(d.strftime("%d/%m(%a)") for d in days))
        if random_task:
            sample["TaskId"] = (random.choice(pool) if pool
                                else "<chọn ngẫu nhiên từ trang khi chạy thật>")
        print("POST", BASE + ENDPOINT, "(form-urlencoded)")
        print("Mẫu payload" + (" (ngày đầu tiên)" if days else "") + ":")
        print(json.dumps({**sample, TOKEN_FIELD: "<tự lấy khi chạy thật>"},
                         ensure_ascii=False, indent=2))
        return

    targets = days or [None]
    if len(targets) > 1 and "--yes" not in sys.argv:
        print(f"Sắp gửi {len(targets)} bản ghi: TaskId={payload.get('TaskId')}, "
              f"Hours={payload.get('Hours')}, Note={payload.get('Note')!r}")
        print("  Ngày: " + ", ".join(d.strftime("%d/%m") for d in targets))
        if input("Tiếp tục? (y/N): ").strip().lower() != "y":
            sys.exit("Đã hủy, chưa gửi gì.")

    s = requests.Session()
    s.headers["X-Requested-With"] = "XMLHttpRequest"
    login(s)

    ok = 0
    for i, d in enumerate(targets):
        p = dict(payload)
        if d:
            set_date(p, d)
        label = p["DateFromIns"]
        return_url = p.get("returnUrl", "/Timesheet")
        p[TOKEN_FIELD] = get_token(s, return_url)

        if random_task:
            pool = pool or scrape_task_ids(s, return_url)
            if not pool:
                sys.exit("Không có danh sách TaskId. Thêm \"TaskIdPool\": [\"id1\", \"id2\"] "
                         "vào payload.json.")
            p["TaskId"] = random.choice(pool)
            print(f"[{label}] TaskId được chọn:", p["TaskId"])

        r = s.post(BASE + ENDPOINT, data=p, timeout=30)
        try:
            body = json.dumps(r.json(), ensure_ascii=False)
        except ValueError:
            body = r.text[:300]
        print(f"[{label}] HTTP {r.status_code} {body}")
        if not r.ok:
            sys.exit(f"Dừng tại ngày {label} do lỗi HTTP {r.status_code}. "
                     f"Đã gửi xong {ok}/{len(targets)} bản ghi.")
        ok += 1
        if i < len(targets) - 1:
            time.sleep(1)
    print(f"Hoàn tất: {ok}/{len(targets)} bản ghi.")


if __name__ == "__main__":
    main()