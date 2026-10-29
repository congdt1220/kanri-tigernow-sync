#!/usr/bin/env python3
"""KanriApi: đăng nhập kanri.jemshcm.vn, tự lấy token, lấy danh sách task.

Cài:   pip install requests
Chạy:  py kanri_api.py
Kết quả in ra màn hình. Danh sách project chỉ giữ tạm trong bộ nhớ để lấy id.

Đổi điều kiện lọc: thêm --set key=value (giá trị là JSON). Key của màn project
(pjName, notFinishProjectOnly, kyotenId, ...) lọc project. Key của Tasks/GetList
(isMiNouhinTask, taskName, ...) lọc task. Ví dụ
  py kanri_api.py --set pjName=\"アールズファクトリー\" --set notFinishProjectOnly=false
Dùng trong code khác:
  from kanri_api import KanriApi
  api = KanriApi()
  tasks = api.get_all_tasks(api.get_projects(pjName="SD5"))

Tài khoản và mật khẩu điền sẵn; ghi đè bằng biến môi trường KANRI_USER / KANRI_PASS.
"""
import json
import os
import sys
import time

import requests

BASE = "https://kanri.jemshcm.vn"
LOGIN_PATH = "/api/api/Shains/Login"
TASKS_PATH = "/api/api/Projects/GetProjectsFromTasksScreen"
PROJECT_TASKS_PATH = "/api/api/Projects/{project_id}/Tasks/GetList"
# Không chia sẻ file này cho người khác vì đang chứa mật khẩu.
DEFAULT_USER = "congdo"
DEFAULT_PASS = ""

# Payload lấy từ request thật của trình duyệt (trang Task list)
DEFAULT_PAYLOAD = {
    "kyotenId": 10, "teamId": None, "pjNo": None, "pjName": "",
    "notFinishProjectOnly": True, "taskId": None, "taskName": "",
    "isMiNouhinTask": True, "typeId": None, "kaihatsuTantoushaCode": None,
    "testTantoushaCode": None, "taskStartDateFrom": None, "taskStartDateTo": None,
    "taskEndDateFrom": None, "taskEndDateTo": None, "isCodeShinchokuIhan": False,
    "isTestShinchokuIhan": False, "bugType": 0,
    "pagingDto": {"currentPage": 1, "pageSize": 200, "totalPage": 0, "totalCount": 0},
    "bumonId": None, "pjTeamId": None, "pjTestTeamId": None, "pjId": None,
}
# Payload lấy task của một project (Tasks/GetList). isMiNouhinTask=true: chỉ task chưa nộp.
DEFAULT_TASK_PAYLOAD = {
    "taskId": None, "taskName": "", "isMiNouhinTask": True, "typeId": None,
    "kaihatsuTantoushaCode": None, "testTantoushaCode": None,
    "taskStartDateFrom": None, "taskStartDateTo": None,
    "taskEndDateFrom": None, "taskEndDateTo": None,
    "isCodeShinchokuIhan": False, "isTestShinchokuIhan": False, "bugType": 0,
    "pagingDto": {"currentPage": 1, "pageSize": 200, "totalCount": 0, "totalPage": 0},
}
TOKEN_KEYS = ("token", "accessToken", "access_token", "jwt", "jwtToken", "idToken", "id_token")


class KanriError(Exception):
    pass


def _find_token(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in TOKEN_KEYS and isinstance(v, str) and len(v) > 20:
                return v
        for v in obj.values():
            t = _find_token(v)
            if t:
                return t
    elif isinstance(obj, list):
        for v in obj:
            t = _find_token(v)
            if t:
                return t
    return None


def _find_rows(obj):
    """Danh sách dòng (list các dict) đầu tiên tìm thấy trong phản hồi."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return obj
        for x in obj:
            r = _find_rows(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for key in ("items", "data", "result", "results", "projects", "value", "list"):
            if key in obj:
                r = _find_rows(obj[key])
                if r:
                    return r
        for v in obj.values():
            r = _find_rows(v)
            if r:
                return r
    return []


def _find_paging(obj):
    """Tìm dict phân trang (có totalPage hoặc totalCount) trong phản hồi."""
    if isinstance(obj, dict):
        if "totalPage" in obj or "totalCount" in obj:
            return obj
        for v in obj.values():
            p = _find_paging(v)
            if p:
                return p
    elif isinstance(obj, list):
        for v in obj:
            p = _find_paging(v)
            if p:
                return p
    return None


class KanriApi:
    def __init__(self, user=None, password=None, base=BASE):
        self.base = base
        self.user = user or os.environ.get("KANRI_USER", DEFAULT_USER)
        self.password = password or os.environ.get("KANRI_PASS", DEFAULT_PASS)
        self.token = None
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Origin": base,
            "Referer": base + "/",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"),
        })

    def login(self) -> str:
        r = self.session.post(self.base + LOGIN_PATH, timeout=30,
                              json={"username": self.user, "password": self.password})
        if not r.ok:
            raise KanriError(f"Đăng nhập lỗi HTTP {r.status_code}: {r.text[:300]}")
        try:
            token = _find_token(r.json())
        except ValueError:
            token = r.text.strip().strip('"') if len(r.text.strip()) > 20 else None
        if not token:
            raise KanriError("Đăng nhập OK nhưng không thấy token trong phản hồi "
                             "(cần xem Response của request Login để biết tên khóa).")
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {token}"
        return token

    def post(self, path: str, payload: dict):
        if not self.token:
            self.login()
        r = self.session.post(self.base + path, json=payload, timeout=60)
        if r.status_code == 401:  # token hết hạn: đăng nhập lại một lần
            self.login()
            r = self.session.post(self.base + path, json=payload, timeout=60)
        if not r.ok:
            raise KanriError(f"{path} lỗi HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def get(self, path: str, params=None):
        if not self.token:
            self.login()
        r = self.session.get(self.base + path, params=params, timeout=60)
        if r.status_code == 401:
            self.login()
            r = self.session.get(self.base + path, params=params, timeout=60)
        if not r.ok:
            raise KanriError(f"{path} lỗi HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def get_project_pages(self, max_pages=100, **overrides):
        """Các trang project (tạm, để lấy id). Không ghi file."""
        payload = json.loads(json.dumps(DEFAULT_PAYLOAD))  # bản sao sâu
        payload.update(overrides)
        paging = payload["pagingDto"]
        page_size = int(paging.get("pageSize") or 200)
        pages, seen_first = [], None
        for page_no in range(1, max_pages + 1):
            paging["currentPage"] = page_no
            data = self.post(TASKS_PATH, payload)
            rows = data.get("items")
            if rows is None:
                rows = _find_rows(data)
            if not rows:
                break
            first = json.dumps(rows[0], sort_keys=True, ensure_ascii=False)
            if first == seen_first:  # server trả lại cùng một trang: dừng, tránh lặp vô hạn
                break
            seen_first = first
            pages.append(data)
            info = data.get("paging") or _find_paging(data) or {}
            total_page = info.get("totalPages") or info.get("totalPage") or 0
            if info.get("hasNext") is False or (total_page and page_no >= total_page):
                break
            if not total_page and len(rows) < page_size:
                break
            time.sleep(0.3)
        return pages

    def get_projects(self, **overrides) -> list:
        """Gộp mọi trang project. Chỉ dùng tạm để lấy id, rồi gọi get_all_tasks."""
        rows = []
        for data in self.get_project_pages(**overrides):
            items = data.get("items")
            rows.extend(items if items is not None else _find_rows(data))
        return rows

    def get_project_task_pages(self, project_id, max_pages=100, **overrides):
        """Mọi trang Tasks/GetList của một project. project_id là id project đã trả về."""
        payload = json.loads(json.dumps(DEFAULT_TASK_PAYLOAD))
        payload.update(overrides)
        paging = payload["pagingDto"]
        page_size = int(paging.get("pageSize") or 200)
        path = PROJECT_TASKS_PATH.format(project_id=project_id)
        pages, seen_first = [], None
        for page_no in range(1, max_pages + 1):
            paging["currentPage"] = page_no
            data = self.post(path, payload)
            rows = data.get("items")
            if rows is None:
                rows = _find_rows(data)
            if not rows:
                break
            first = json.dumps(rows[0], sort_keys=True, ensure_ascii=False)
            if first == seen_first:
                break
            seen_first = first
            pages.append(data)
            info = data.get("paging") or {}
            total_page = info.get("totalPages") or info.get("totalPage") or 0
            paging["totalCount"] = info.get("totalRecords", paging.get("totalCount"))
            paging["totalPage"] = total_page
            if info.get("hasNext") is False:
                break
            if total_page and page_no >= total_page:
                break
            if not total_page and len(rows) < page_size:
                break
            time.sleep(0.3)
        return pages

    def get_project_tasks(self, project_id, **overrides) -> list:
        """Gộp mọi trang task của một project."""
        rows = []
        for data in self.get_project_task_pages(project_id, **overrides):
            items = data.get("items")
            rows.extend(items if items is not None else _find_rows(data))
        return rows

    def get_all_tasks(self, projects, **overrides) -> list:
        """Lấy hết task của từng project. projects là list dict có id, hoặc list id."""
        rows = []
        for project in projects:
            project_id = project.get("id") if isinstance(project, dict) else project
            if project_id is None:
                continue
            for task in self.get_project_tasks(project_id, **overrides):
                row = dict(task)
                row["projectId"] = project_id
                rows.append(row)
            time.sleep(0.3)
        return rows


def print_tasks(tasks: list, projects: list) -> None:
    names = {p.get("id"): p.get("pjName") or "" for p in projects if isinstance(p, dict)}
    print(f"{len(tasks)} task")
    if not tasks:
        return
    print("projectId\tpjName\ttaskId\ttaskName\tstatus\tdev\ttest\tdelivery")
    for t in tasks:
        pid = t.get("projectId")
        delivery = str(t.get("deliveryDate") or "")[:10]
        print(
            f"{pid}\t{names.get(pid, '')}\t{t.get('taskId')}\t{t.get('taskName')}\t"
            f"{t.get('taskStatusName')}\t{t.get('kaihatsuTantoushaName')}\t"
            f"{t.get('testTantoushaName')}\t{delivery}"
        )


def parse_overrides(argv) -> dict:
    out = {}
    args = iter(argv)
    for a in args:
        if a == "--set":
            a = next(args, "")
            if "=" not in a:
                sys.exit("Dùng: --set key=value")
            k, v = a.split("=", 1)
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = v  # không phải JSON thì coi là chuỗi
        elif a.startswith("--set="):
            k, v = a[6:].split("=", 1)
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = v
    return out


def main() -> None:
    overrides = parse_overrides(sys.argv[1:])
    task_overrides = {k: v for k, v in overrides.items() if k in DEFAULT_TASK_PAYLOAD}
    api = KanriApi()
    try:
        projects = api.get_projects(**overrides)
        print(f"Tạm lấy {len(projects)} project, đang lấy task...")
        tasks = api.get_all_tasks(projects, **task_overrides)
    except KanriError as e:
        sys.exit(f"Lỗi: {e}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print_tasks(tasks, projects)


if __name__ == "__main__":
    main()