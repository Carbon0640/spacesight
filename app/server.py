"""SpaceSight 本地 Web 服务（纯标准库实现）。

启动：python run.py [--port 8765] [--no-browser] [--host 127.0.0.1]
路由：/api/* 为 JSON 接口，其余路径映射到 web/ 目录下的静态文件。
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import detectors as detector_mod
from . import scanner
from .jobs import JobRegistry
from .store import Store, norm_path, pkey

APP_NAME = "SpaceSight"
APP_VERSION = "1.0.2"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.environ.get("SPACESIGHT_DATA") or os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "spacesight.db")

# 系统目录：注册表/目录侦测到的这些路径不自动纳入追踪（噪音太大）
NO_AUTO_TRACK = {
    "c:\\", "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "c:\\users",
}

# 目录侦测自动纳入追踪后，扫描完发现小于该大小的目标会被自动撤回
MIN_AUTO_TRACK_BYTES = 300 * 1024 * 1024

MIME_EXTRA = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


def _safe_print(*args, **kwargs) -> None:
    """无控制台启动时 sys.stdout 可能是 None，任何 print 都不允许把服务带崩。"""
    try:
        print(*args, **kwargs)
    except Exception:
        pass


def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit in ("B", "KB") else f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}TB"


class App:
    """进程内的应用状态容器。"""

    def __init__(self, store: Store, host: str, port: int):
        self.store = store
        self.host = host
        self.port = port
        self.started = time.time()
        self.jobs = JobRegistry()
        self.scan_queue: queue.Queue = queue.Queue()
        self.detector: detector_mod.Detector | None = None
        self.httpd: ThreadingHTTPServer | None = None
        self._rescan_stamp: dict[int, float] = {}
        self._lock = threading.Lock()
        self.worker = threading.Thread(target=self._scan_worker, daemon=True,
                                       name="spacesight-scan-worker")
        self.worker.start()

    # -------------------------------------------------------------- 扫描调度
    def scan_config(self) -> dict:
        s = self.store.get_settings()
        return {"min_file_keep": int(s.get("min_file_keep", 65536)),
                "scan_workers": int(s.get("scan_workers", 4)),
                "max_nodes": 600000}

    def running_scan_job(self, root_id: int) -> dict | None:
        for job in self.jobs.list(only_running=True):
            if job["kind"] == "scan" and int(job["extra"].get("root_id") or 0) == root_id:
                return job
        return None

    def start_scan(self, root_id: int, reason: str = "manual") -> dict:
        running = self.running_scan_job(root_id)
        if running:
            return running
        root = self.store.get_root(root_id)
        if not root:
            return {"error": "根节点不存在"}
        job = self.jobs.new("scan", root_id=root_id, path=root["path"],
                            label=root["label"], reason=reason)
        self.scan_queue.put((root_id, job))
        return job.snapshot()

    def _scan_worker(self) -> None:
        while True:
            item = self.scan_queue.get()
            if item is None:
                self.scan_queue.task_done()
                break
            root_id, job = item
            try:
                root = self.store.get_root(root_id)
                if not root:
                    job.fail("根节点已删除")
                    continue
                if not os.path.exists(root["path"]):
                    self.store.update_root(root_id, status="missing")
                    job.fail("路径不存在或已断开")
                    continue
                result = scanner.scan_root(self.store, root, job, self.scan_config())
                if result.get("error"):
                    self.store.add_event("error", label=root["label"], path=root["path"],
                                         message=f"扫描失败：{result['error']}")
                else:
                    total = int(result.get("total_bytes") or 0)
                    # 目录侦测出来的小目录不值得长期追踪（AppData 下的碎片会把列表塞满）
                    if (root.get("source") == "detector" and not root.get("pinned")
                            and total < MIN_AUTO_TRACK_BYTES):
                        self.store.delete_root(root_id)
                        self.store.add_event(
                            "untrack", label=root["label"], path=root["path"],
                            message=f"自动忽略：占用仅 {human_bytes(total)}（门槛 "
                                    f"{human_bytes(MIN_AUTO_TRACK_BYTES)}，可手动添加）")
                        continue
                    prev = int(root.get("total_bytes") or 0)
                    delta = abs(total - prev)
                    # 后台侦测触发的重扫：大小几乎没变就不刷日志
                    quiet = (job.extra.get("reason") == "watch" and prev > 0
                             and delta < max(prev * 0.05, 100 * 1024 * 1024))
                    self.store.update_root(root_id, is_new=0)
                    if not quiet:
                        self.store.add_event(
                            "scan", label=root["label"], path=root["path"], size=total,
                            message=f"扫描完成：{human_bytes(total)}"
                                    f" / {result.get('file_count', 0)} 文件 / {result.get('scan_ms', 0)}ms")
            except Exception as exc:
                job.fail(f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
            finally:
                self.scan_queue.task_done()

    # -------------------------------------------------------------- 察觉回调
    def _auto_track_allowed(self, path: str) -> bool:
        """自动追踪的准入判断。

        除了开关与系统目录，还要排除**与已有追踪根重叠**的路径：
        父目录已经在追踪里时再追踪子目录会造成重复计数，反之亦然。
        """
        if str(self.store.get_setting("auto_track", "1")) != "1":
            return False
        key = pkey(path).rstrip("\\")
        if key in {k.rstrip("\\") for k in NO_AUTO_TRACK}:
            return False
        for root in self.store.list_roots():
            rk = (root.get("key") or "").rstrip("\\")
            if not rk:
                continue
            if key == rk or key.startswith(rk + "\\") or rk.startswith(key + "\\"):
                return False
        return os.path.isdir(path)

    def on_install_detected(self, entry: dict) -> None:
        path = (entry.get("path") or "").strip()
        app_key = entry.get("key", "")
        if path and self._auto_track_allowed(path):
            root = self.store.add_root(path, label=entry.get("name", ""), kind="folder",
                                       source="registry", is_new=1,
                                       meta={"version": entry.get("version", ""),
                                             "publisher": entry.get("publisher", ""),
                                             "install_date": entry.get("install_date", ""),
                                             "detected": "registry"})
            if root:
                if app_key:
                    self.store.track_app(app_key, root["id"])
                self.store.add_event("track", label=root["label"], path=path,
                                     message="已自动纳入追踪，开始测量占用")
                self.start_scan(root["id"], reason="auto")

    def on_folder_detected(self, item: dict) -> None:
        path = item.get("path", "")
        if not self._auto_track_allowed(path):
            return
        root = self.store.add_root(path, label=item.get("name", ""), kind="folder",
                                   source="detector", is_new=1,
                                   meta={"detected": "folder-watch",
                                         "parent": item.get("parent", "")})
        if root:
            self.store.add_event("track", label=root["label"], path=path,
                                 message="侦测到新目录，已自动纳入追踪")
            self.start_scan(root["id"], reason="auto")

    def on_root_changed(self, root_id: int, item: dict) -> None:
        now = time.time()
        if now - self._rescan_stamp.get(root_id, 0) < 60:
            return
        self._rescan_stamp[root_id] = now
        self.start_scan(root_id, reason="watch")

    def shutdown(self) -> None:
        if self.detector:
            self.detector.stop()
        try:
            self.scan_queue.put(None)
        except Exception:
            pass
        if self.httpd:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{APP_VERSION}"
    protocol_version = "HTTP/1.1"
    app: App | None = None

    # ------------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):  # 静音访问日志，只保留错误
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400):
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > 8 * 1024 * 1024:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    # ------------------------------------------------------------------ 分发
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                self._route_api(method, path)
            elif method == "GET":
                self._serve_static(path)
            else:
                self._error("不支持的请求", 405)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._error("服务器内部错误", 500)
            except Exception:
                pass

    # ------------------------------------------------------------------ 静态文件
    def _serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = urllib.parse.unquote(path.lstrip("/"))
        target = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not target.startswith(os.path.realpath(WEB_DIR)) or not os.path.isfile(target):
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            self._send(404, b"404 Not Found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(target)[1].lower()
        ctype = MIME_EXTRA.get(ext) or mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype)

    # ------------------------------------------------------------------ API
    def _route_api(self, method: str, path: str):
        app = self.app
        store = app.store
        q = self._query()
        seg = [s for s in path.split("/") if s][1:]  # 去掉 'api'

        # ---- 概览
        if method == "GET" and seg == ["state"]:
            roots = store.list_roots()
            events = store.list_events(limit=30)
            return self._json({
                "ok": True, "app": APP_NAME, "version": APP_VERSION,
                "uptime": round(time.time() - app.started, 1),
                "settings": store.get_settings(),
                "totals": store.totals(),
                "roots": roots,
                "jobs": app.jobs.list(),
                "events": events,
                "max_event_id": events[0]["id"] if events else 0,
                "detector": app.detector.status_dict() if app.detector else {},
                "disk": detector_mod.disk_usage(),
                "host": f"{app.host}:{app.port}",
                "db_path": DB_PATH,
                "data_dir": DATA_DIR,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

        if method == "GET" and seg == ["health"]:
            return self._json({"ok": True, "app": APP_NAME, "version": APP_VERSION,
                               "pid": os.getpid()})

        # ---- 树
        if method == "GET" and seg == ["tree"]:
            return self._tree(q)
        if method == "GET" and seg == ["node"]:
            return self._node(q)
        if method == "GET" and seg == ["search"]:
            qtext = (q.get("q") or "").strip()
            if not qtext:
                return self._json({"ok": True, "hits": []})
            threshold = int(q.get("threshold") or 0)
            root_id = int(q.get("root_id") or 0) or None
            hits = store.search(root_id, qtext, threshold, limit=80)
            notes = store.notes_for([h["key"] for h in hits])
            for h in hits:
                h["note"] = notes.get(h["key"], {}).get("note", "")
                h["flagged"] = notes.get(h["key"], {}).get("flagged", False)
            return self._json({"ok": True, "hits": hits})
        if method == "GET" and seg == ["treemap"]:
            root_id = int(q.get("root_id") or 0)
            root = store.get_root(root_id)
            if not root:
                return self._error("根节点不存在", 404)
            key = pkey(q.get("key") or root["key"])
            threshold = int(q.get("threshold") or 0)
            limit = min(int(q.get("limit") or 160), 400)
            depth = max(1, min(int(q.get("depth") or 1), 2))
            rows, hidden, total = store.children(root_id, key, threshold, limit=limit)
            if depth >= 2:
                for r in rows[:70]:
                    if r["is_dir"]:
                        sub, _sh, _st = store.children(root_id, r["key"], threshold, limit=14)
                        r["sub"] = sub
                    else:
                        r["sub"] = []
            if key == root["key"]:
                node = {"key": root["key"], "path": root["path"], "name": root["label"],
                        "size": root["total_bytes"], "is_dir": 1,
                        "files": root["file_count"], "dirs": root["dir_count"],
                        "depth": 0, "parent_key": ""}
                ancestors = []
            else:
                node = store.node(root_id, key) or {
                    "key": key, "path": key, "name": os.path.basename(key), "size": 0,
                    "is_dir": 1, "files": 0, "dirs": 0, "depth": 1, "parent_key": ""}
                ancestors = store.ancestors(root_id, key)
            return self._json({
                "ok": True, "root": root, "node": node, "parent": node,
                "ancestors": ancestors, "children": rows, "hidden": hidden,
                "total": total, "depth": depth,
            })
        if method == "GET" and seg == ["top"]:
            root_id = int(q.get("root_id") or 0)
            threshold = int(q.get("threshold") or 0)
            limit = min(int(q.get("limit") or 20), 200)
            only_dirs = bool(int(q.get("dirs") or 0))
            return self._json({"ok": True, "nodes": store.top_nodes(root_id, threshold,
                                                                    limit, only_dirs)})

        # ---- 标注
        if method in ("PUT", "POST") and seg == ["notes"]:
            body = self._body()
            path_v = norm_path(body.get("path", ""))
            if not path_v:
                return self._error("缺少 path")
            note = str(body.get("note", "") or "")[:8000]
            tags = str(body.get("tags", "") or "")[:400]
            flagged = bool(body.get("flagged", False))
            res = store.set_note(path_v, note, tags, flagged)
            return self._json({"ok": True, "note": res})
        if method == "GET" and seg == ["notes"]:
            return self._json({"ok": True, "notes": store.all_notes()})

        # ---- 追踪根
        if method == "POST" and seg == ["roots"]:
            body = self._body()
            raw = (body.get("path") or "").strip()
            path_v = norm_path(raw)
            if not path_v:
                return self._error("请输入路径")
            if not os.path.exists(path_v):
                return self._error(f"路径不存在：{path_v}")
            kind = "file" if os.path.isfile(path_v) else (
                "drive" if os.path.dirname(path_v) == path_v else "folder")
            existing = store.find_root_by_key(pkey(path_v))
            if existing:
                return self._json({"ok": True, "root": existing, "already": True})
            root = store.add_root(path_v, label=(body.get("label") or "").strip(),
                                  kind=kind, source="manual",
                                  meta={"size_hint": body.get("size_hint")})
            if not root:
                return self._error("无法添加追踪目标")
            store.add_event("track", label=root["label"], path=path_v, message="已加入追踪，开始扫描")
            job = app.start_scan(root["id"], reason="manual")
            return self._json({"ok": True, "root": root, "job": job})

        if method == "DELETE" and len(seg) == 2 and seg[0] == "roots":
            root_id = int(seg[1])
            root = store.get_root(root_id)
            if not root:
                return self._error("根节点不存在", 404)
            store.delete_root(root_id)
            store.add_event("untrack", label=root["label"], path=root["path"],
                            message="已移出追踪（标注保留）")
            return self._json({"ok": True})

        if method == "POST" and len(seg) == 3 and seg[0] == "roots" and seg[2] == "scan":
            root_id = int(seg[1])
            job = app.start_scan(root_id, reason="manual")
            return self._json({"ok": True, "job": job})

        if method == "POST" and len(seg) == 3 and seg[0] == "roots" and seg[2] == "pin":
            root_id = int(seg[1])
            root = store.get_root(root_id)
            if not root:
                return self._error("根节点不存在", 404)
            updated = store.update_root(root_id, pinned=0 if root["pinned"] else 1)
            return self._json({"ok": True, "root": updated})

        if method == "POST" and len(seg) == 3 and seg[0] == "roots" and seg[2] == "label":
            body = self._body()
            root_id = int(seg[1])
            updated = store.update_root(root_id, label=str(body.get("label") or "")[:80])
            return self._json({"ok": True, "root": updated})

        # ---- 任务
        if method == "GET" and seg == ["jobs"]:
            return self._json({"ok": True, "jobs": app.jobs.list()})
        if method == "GET" and len(seg) == 2 and seg[0] == "jobs":
            job = app.jobs.get(seg[1])
            if not job:
                return self._error("任务不存在", 404)
            return self._json({"ok": True, "job": job.snapshot()})
        if method == "POST" and len(seg) == 3 and seg[0] == "jobs" and seg[2] == "cancel":
            job = app.jobs.get(seg[1])
            if job:
                job.cancel()
            return self._json({"ok": True})

        # ---- 大文件体检
        if method == "POST" and seg == ["audit"]:
            body = self._body()
            path_v = norm_path(body.get("path") or "")
            if not path_v or not os.path.exists(path_v):
                return self._error("路径不存在")
            min_size = int(body.get("min_size") or 100 * 1024 * 1024)
            top_n = min(int(body.get("top_n") or 80), 300)
            job = app.jobs.new("audit", path=path_v)
            threading.Thread(target=scanner.audit_path,
                             args=(path_v, job, min_size, top_n), daemon=True).start()
            return self._json({"ok": True, "job": job.snapshot()})

        # ---- 安装台账
        if method == "GET" and seg == ["apps"]:
            apps = store.list_apps(q.get("q", ""), only_tracked=bool(int(q.get("tracked") or 0)))
            # 关卡判定：只要台账里有数据就正常展示（离线/暂停察觉时也能看历史台账）
            registry_ok = bool(app.detector and app.detector.status.get("registry_ok")) or bool(apps)
            return self._json({"ok": True, "apps": apps,
                               "count": len(apps), "registry_ok": registry_ok})
        if method == "POST" and seg == ["apps", "track"]:
            body = self._body()
            app_key = body.get("key", "")
            apps = {a["key"]: a for a in store.list_apps()}
            entry = apps.get(app_key)
            if not entry:
                return self._error("台账中没有该程序", 404)
            path_v = norm_path(entry.get("path") or "")
            if not path_v or not os.path.isdir(path_v):
                return self._error("该程序未提供有效的安装路径，请手动添加")
            root = store.find_root_by_key(pkey(path_v))
            if not root:
                root = store.add_root(path_v, label=entry["name"], kind="folder",
                                      source="registry", is_new=1,
                                      meta={"version": entry.get("version", ""),
                                            "publisher": entry.get("publisher", ""),
                                            "detected": "registry-manual"})
            if not root:
                return self._error("无法添加追踪目标")
            store.track_app(app_key, root["id"])
            job = app.start_scan(root["id"], reason="manual")
            return self._json({"ok": True, "root": root, "job": job})
        if method == "POST" and seg == ["apps", "clear-new"]:
            store.clear_apps_new_flags()
            return self._json({"ok": True})

        # ---- 事件
        if method == "GET" and seg == ["events"]:
            since = int(q.get("since") or 0)
            limit = min(int(q.get("limit") or 60), 300)
            events = store.list_events(since_id=since, limit=limit)
            return self._json({"ok": True, "events": events,
                               "max_id": events[0]["id"] if events else since})
        if method == "POST" and seg == ["events", "clear"]:
            store.clear_events()
            return self._json({"ok": True})
        if method == "POST" and seg == ["detect"]:
            res = app.detector.detect_now() if app.detector else {}
            return self._json({"ok": True, "result": res,
                               "detector": app.detector.status_dict() if app.detector else {}})

        # ---- 文件系统浏览器
        if method == "GET" and seg == ["fs", "drives"]:
            return self._json({"ok": True, "drives": self._drives()})
        if method == "GET" and seg == ["fs", "list"]:
            return self._fs_list(q)
        if method == "POST" and seg == ["reveal"]:
            body = self._body()
            target = norm_path(body.get("path") or "")
            if not target or not os.path.exists(target):
                return self._error("路径不存在")
            import subprocess
            try:
                if os.path.isdir(target):
                    os.startfile(target)  # noqa: S606
                else:
                    subprocess.Popen(["explorer", "/select,", target])
                return self._json({"ok": True})
            except Exception as exc:
                return self._error(f"打开失败：{exc}")

        # ---- 设置 / 导出 / 关停
        if method in ("POST", "PUT") and seg == ["settings"]:
            body = self._body()
            allowed = {"threshold", "auto_track", "watch_enabled", "min_file_keep",
                       "scan_workers"}
            changed = {}
            for k, v in body.items():
                if k in allowed:
                    store.set_setting(k, v)
                    changed[k] = v
            return self._json({"ok": True, "settings": store.get_settings(), "changed": changed})
        if method == "GET" and seg == ["export"]:
            payload = {
                "app": APP_NAME, "version": APP_VERSION,
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "settings": store.get_settings(), "roots": store.list_roots(),
                "notes": store.all_notes(), "apps": store.list_apps(),
                "events": store.list_events(limit=1000),
            }
            return self._json(payload)
        if method == "POST" and seg == ["shutdown"]:
            self._json({"ok": True, "message": "服务正在关闭"})
            threading.Timer(0.2, app.shutdown).start()
            return

        return self._error(f"未知接口 {path}", 404)

    # ------------------------------------------------------------------ 子过程
    def _drives(self) -> list[dict]:
        import ctypes
        out = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            try:
                dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
            except Exception:
                dtype = 3
            if dtype not in (2, 3, 4, 5, 6):
                continue
            try:
                usage = __import__("shutil").disk_usage(root)
            except Exception:
                usage = None
            out.append({
                "path": root, "type": dtype,
                "label": {2: "可移动", 3: "本地磁盘", 4: "网络", 5: "光驱", 6: "RAM"}.get(dtype, "磁盘"),
                "total": usage.total if usage else 0,
                "used": usage.used if usage else 0,
                "free": usage.free if usage else 0,
            })
        return out

    def _fs_list(self, q: dict) -> None:
        import shutil
        target = norm_path(q.get("path") or "C:\\")
        if not target or not os.path.isdir(target):
            return self._error("不是有效目录")
        dirs: list[dict] = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs.append({"name": entry.name, "path": entry.path})
                    except OSError:
                        continue
            dirs.sort(key=lambda d: d["name"].lower())
        except OSError as exc:
            return self._error(f"无法读取目录：{exc}")
        trimmed = target.rstrip("\\/") or target
        parent = os.path.dirname(trimmed)
        return self._json({"ok": True, "path": target, "parent": parent,
                           "dirs": dirs[:400], "truncated": len(dirs) > 400})

    def _tree(self, q: dict) -> None:
        store = self.app.store
        root_id = int(q.get("root_id") or 0)
        root = store.get_root(root_id)
        if not root:
            return self._error("根节点不存在", 404)
        threshold = int(q.get("threshold") or 0)
        limit = min(int(q.get("limit") or 500), 2000)
        offset = max(int(q.get("offset") or 0), 0)
        parent = q.get("parent") or root["key"]
        parent_key = pkey(parent) if parent else root["key"]
        rows, hidden, total = store.children(root_id, parent_key, threshold,
                                             limit=limit, offset=offset)
        notes = store.notes_for([r["key"] for r in rows])
        for r in rows:
            info = notes.get(r["key"], {})
            r["note"] = info.get("note", "")
            r["flagged"] = info.get("flagged", False)
        if parent_key == root["key"]:
            parent_node = {"key": root["key"], "path": root["path"], "name": root["label"],
                           "size": root["total_bytes"], "is_dir": 1,
                           "files": root["file_count"], "dirs": root["dir_count"],
                           "depth": 0, "mtime": 0, "parent_key": ""}
        else:
            parent_node = store.node(root_id, parent_key) or {
                "key": parent_key, "path": parent, "name": os.path.basename(parent),
                "size": 0, "is_dir": 1, "files": 0, "dirs": 0, "depth": 1, "mtime": 0}
        notes_parent = store.notes_for([parent_node["key"]]).get(parent_node["key"], {})
        parent_node["note"] = notes_parent.get("note", "")
        parent_node["flagged"] = notes_parent.get("flagged", False)
        self._json({"ok": True, "root": root, "parent": parent_node,
                    "children": rows, "hidden": hidden, "total": total,
                    "offset": offset, "limit": limit,
                    "truncated": offset + len(rows) < total})

    def _node(self, q: dict) -> None:
        store = self.app.store
        root_id = int(q.get("root_id") or 0)
        root = store.get_root(root_id)
        if not root:
            return self._error("根节点不存在", 404)
        key = pkey(q.get("key") or root["key"])
        if key == root["key"]:
            node = {"key": root["key"], "path": root["path"], "name": root["label"],
                    "size": root["total_bytes"], "is_dir": 1, "files": root["file_count"],
                    "dirs": root["dir_count"], "depth": 0, "mtime": 0,
                    "parent_key": "", "is_root": True}
        else:
            node = store.node(root_id, key)
            if not node:
                return self._error("节点不存在", 404)
        notes = store.notes_for([key]).get(key, {})
        node["note"] = notes.get("note", "")
        node["tags"] = notes.get("tags", "")
        node["flagged"] = notes.get("flagged", False)
        preview, hidden, total = store.children(root_id, key, 0, limit=12)
        node["preview"] = preview
        node["children_total"] = total
        node["root"] = root
        self._json({"ok": True, "node": node})


class SpaceHTTPServer(ThreadingHTTPServer):
    """忽略浏览器断连产生的噪声回溯（keep-alive 连接被关闭是正常现象）。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def find_free_port(host: str, preferred: int, span: int = 12) -> int:
    for port in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("端口全部被占用")


def existing_instance(port: int) -> bool:
    """探测目标端口上是否已经跑着 SpaceSight。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("app") == APP_NAME
    except Exception:
        return False


def print_banner(url: str) -> None:
    # 只有真正的终端才输出 ANSI 颜色；写入日志文件时保持纯文本
    is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    if is_tty:
        cyan, magenta, dim, reset = "\033[96m", "\033[95m", "\033[90m", "\033[0m"
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
    else:
        cyan = magenta = dim = reset = ""
    _safe_print(f"""{cyan}
   ███████╗██████╗  █████╗  ██████╗███████╗███████╗██╗ ██████╗ ██╗  ██╗████████╗
   ██╔════╝██╔══██╗██╔══██╗██╔════╝██╔════╝██╔════╝██║██╔════╝ ██║  ██║╚══██╔══╝
   ███████╗██████╔╝███████║██║     █████╗  ███████╗██║██║  ███╗███████║   ██║
   ╚════██║██╔═══╝ ██╔══██║██║     ██╔══╝  ╚════██║██║██║   ██║██╔══██║   ██║
   ███████║██║     ██║  ██║╚██████╗███████╗███████║██║╚██████╔╝██║  ██║   ██║
   ╚══════╝╚═╝     ╚═╝  ╚═╝ ╚═════╝╚══════╝╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝
{reset}{magenta}   // 空间探针 · STORAGE CARTOGRAPHY  v{APP_VERSION}{reset}
{dim}   ─────────────────────────────────────────────────────────────────────{reset}""")
    _safe_print(f"   {cyan}◈{reset} 界面地址   {url}")
    _safe_print(f"   {cyan}◈{reset} 数据文件   {DB_PATH}")
    _safe_print(f"   {cyan}◈{reset} 关闭服务   在界面右上角点击「停机」/ 或按 Ctrl+C\n")


def main(argv: list[str] | None = None) -> int:
    global DATA_DIR, DB_PATH
    argv = list(sys.argv[1:] if argv is None else argv)
    host = "127.0.0.1"
    port = 8765
    open_browser = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2
        elif a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]; i += 2
        elif a == "--data-dir" and i + 1 < len(argv):
            # 支持多套独立数据（演示/截图时不想污染主数据库）
            DATA_DIR = os.path.abspath(argv[i + 1])
            DB_PATH = os.path.join(DATA_DIR, "spacesight.db")
            i += 2
        elif a in ("--no-browser", "-n"):
            open_browser = False; i += 1
        elif a in ("--help", "-h"):
            _safe_print("用法: python run.py [--port 8765] [--host 127.0.0.1] "
                        "[--data-dir <目录>] [--no-browser]")
            return 0
        else:
            i += 1

    os.makedirs(DATA_DIR, exist_ok=True)
    if existing_instance(port):
        url = f"http://{host}:{port}/"
        _safe_print(f"SpaceSight 已在运行：{url}")
        if open_browser:
            webbrowser.open(url)
        return 0

    port = find_free_port(host, port)
    store = Store(DB_PATH)
    app_state = App(store, host, port)

    if str(store.get_setting("watch_enabled", "1")) == "1":
        det = detector_mod.Detector(
            store,
            on_install=app_state.on_install_detected,
            on_folder=app_state.on_folder_detected,
            on_root_change=app_state.on_root_changed,
        )
        app_state.detector = det
        det.start()

    Handler.app = app_state
    httpd = SpaceHTTPServer((host, port), Handler)
    app_state.httpd = httpd
    url = f"http://{host}:{port}/"
    try:
        with open(os.path.join(DATA_DIR, "server.port"), "w", encoding="utf-8") as fh:
            fh.write(str(port))
    except OSError:
        pass

    print_banner(url)
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        _safe_print("\n收到 Ctrl+C，正在关闭 …")
    finally:
        if app_state.detector:
            app_state.detector.stop()
        try:
            store.add_event("system", label="服务", message="SpaceSight 已停止")
        except Exception:
            pass
        httpd.server_close()
        store.close()
    return 0
