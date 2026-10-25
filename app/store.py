"""SpaceSight 持久化层。

全部数据落地到单个 SQLite 文件，WAL 模式，进程内加锁串行化写操作。
表结构：
    roots   追踪的根节点（扫描目标 / 已察觉的安装目录）
    nodes   扫描快照（每个根下所有目录与大文件，扁平存储，按 parent_key 索引）
    notes   用户对任意路径的标注（跨根共享，按小写路径键关联）
    events  察觉日志（安装/新目录/扫描等事件流）
    apps    注册表安装清单（用于"我装了什么、装在哪"）
    settings 键值配置
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roots(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    path        TEXT NOT NULL,
    label       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'folder',
    source      TEXT NOT NULL DEFAULT 'manual',
    meta        TEXT NOT NULL DEFAULT '{}',
    is_new      INTEGER NOT NULL DEFAULT 0,
    pinned      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    scanned_at  TEXT,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    file_count  INTEGER NOT NULL DEFAULT 0,
    dir_count   INTEGER NOT NULL DEFAULT 0,
    scan_ms     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes(
    root_id    INTEGER NOT NULL,
    key        TEXT NOT NULL,
    path       TEXT NOT NULL,
    parent_key TEXT NOT NULL,
    name       TEXT NOT NULL,
    is_dir     INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    files      INTEGER NOT NULL DEFAULT 0,
    dirs       INTEGER NOT NULL DEFAULT 0,
    mtime      REAL NOT NULL DEFAULT 0,
    depth      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(root_id, key)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(root_id, parent_key, size DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_size   ON nodes(root_id, size DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_name   ON nodes(root_id, name);

CREATE TABLE IF NOT EXISTS notes(
    key        TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    flagged    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    path    TEXT NOT NULL DEFAULT '',
    label   TEXT NOT NULL DEFAULT '',
    size    INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS apps(
    key         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    version     TEXT NOT NULL DEFAULT '',
    publisher   TEXT NOT NULL DEFAULT '',
    install_date TEXT NOT NULL DEFAULT '',
    reg_key     TEXT NOT NULL DEFAULT '',
    root_id     INTEGER,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    is_new      INTEGER NOT NULL DEFAULT 0
);
"""

DEFAULTS = {
    "threshold": 100 * 1024 * 1024,   # 100 MB，树中只显示大于它的节点
    "auto_track": "1",                # 察觉到的安装自动纳入追踪
    "watch_enabled": "1",             # 后台察觉（安装监控 / 新目录侦测）
    "min_file_keep": 64 * 1024,       # 快照中保留的最小文件（64 KB）
    "scan_workers": 4,
    "first_run_done": "0",
}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def norm_path(path: str) -> str:
    """规范化绝对路径：去引号/空白，展开环境变量，abspath，保留盘符根为 C:\\ 形式。"""
    p = (path or "").strip().strip('"').strip("'").strip()
    if not p:
        return ""
    p = os.path.expandvars(p)
    p = os.path.expanduser(p)
    p = os.path.abspath(p)
    return p


def pkey(path: str) -> str:
    """用于比较/关联的路径键：Windows 大小写不敏感。"""
    return os.path.normpath(norm_path(path)).lower()


class Store:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=20.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.commit()
            self._seed_settings()

    # ------------------------------------------------------------------ 基础
    def _seed_settings(self):
        for k, v in DEFAULTS.items():
            self._conn.execute("INSERT OR IGNORE INTO settings(k, v) VALUES(?, ?)", (k, str(v)))
        self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- 设置项
    def get_setting(self, k: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        if row is None:
            return DEFAULTS.get(k, default)
        raw = row["v"]
        dv = DEFAULTS.get(k)
        try:
            if isinstance(dv, int):
                return int(raw)
            if isinstance(dv, float):
                return float(raw)
        except (TypeError, ValueError):
            return dv
        return raw

    def set_setting(self, k: str, v) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)),
            )
            self._conn.commit()

    def get_settings(self) -> dict:
        out = dict(DEFAULTS)
        with self._lock:
            for row in self._conn.execute("SELECT k, v FROM settings"):
                k, raw = row["k"], row["v"]
                dv = DEFAULTS.get(k)
                try:
                    if isinstance(dv, int):
                        out[k] = int(raw)
                        continue
                    if isinstance(dv, float):
                        out[k] = float(raw)
                        continue
                except (TypeError, ValueError):
                    out[k] = dv
                    continue
                out[k] = raw
        return out

    # -------------------------------------------------------------- 根节点 CRUD
    def _root_row(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except Exception:
            d["meta"] = {}
        d["exists"] = os.path.exists(d["path"])
        return d

    def list_roots(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM roots ORDER BY pinned DESC, total_bytes DESC, id ASC"
            ).fetchall()
        return [self._root_row(r) for r in rows]

    def get_root(self, root_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM roots WHERE id=?", (root_id,)).fetchone()
        return self._root_row(row) if row else None

    def find_root_by_key(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM roots WHERE key=?", (key,)).fetchone()
        return self._root_row(row) if row else None

    def add_root(self, path: str, label: str = "", kind: str = "folder",
                 source: str = "manual", meta: dict | None = None,
                 is_new: int = 0) -> dict | None:
        path = norm_path(path)
        if not path:
            return None
        key = pkey(path)
        if not label:
            label = os.path.basename(path.rstrip("\\/")) or path
        with self._lock:
            exists = self._conn.execute("SELECT id FROM roots WHERE key=?", (key,)).fetchone()
            if exists:
                return None
            cur = self._conn.execute(
                """INSERT INTO roots(key, path, label, kind, source, meta, is_new, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (key, path, label, kind, source, json.dumps(meta or {}, ensure_ascii=False),
                 int(is_new), now_iso()),
            )
            self._conn.commit()
            rid = cur.lastrowid
        return self.get_root(rid)

    def update_root(self, root_id: int, **fields) -> dict | None:
        allowed = {"label", "kind", "source", "is_new", "pinned", "status", "scanned_at",
                   "total_bytes", "file_count", "dir_count", "scan_ms", "meta"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "meta" and isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return self.get_root(root_id)
        vals.append(root_id)
        with self._lock:
            self._conn.execute(f"UPDATE roots SET {', '.join(sets)} WHERE id=?", vals)
            self._conn.commit()
        return self.get_root(root_id)

    def delete_root(self, root_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM nodes WHERE root_id=?", (root_id,))
            self._conn.execute("DELETE FROM roots WHERE id=?", (root_id,))
            self._conn.execute("UPDATE apps SET root_id=NULL WHERE root_id=?", (root_id,))
            self._conn.commit()

    # -------------------------------------------------------------- 快照读写
    def clear_nodes(self, root_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM nodes WHERE root_id=?", (root_id,))
            self._conn.commit()

    def insert_nodes(self, root_id: int, rows: list[tuple]) -> None:
        """rows: (key, path, parent_key, name, is_dir, size, files, dirs, mtime, depth)"""
        if not rows:
            return
        payload = [(root_id,) + tuple(r) for r in rows]
        with self._lock:
            self._conn.executemany(
                """INSERT OR REPLACE INTO nodes
                   (root_id, key, path, parent_key, name, is_dir, size, files, dirs, mtime, depth)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
            self._conn.commit()

    def node(self, root_id: int, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE root_id=? AND key=?", (root_id, key)
            ).fetchone()
        return dict(row) if row else None

    def children(self, root_id: int, parent_key: str, min_size: int,
                 limit: int = 800, offset: int = 0) -> tuple[list[dict], int]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM nodes WHERE root_id=? AND parent_key=? AND size>=?
                   ORDER BY size DESC, name ASC LIMIT ? OFFSET ?""",
                (root_id, parent_key, min_size, limit, offset),
            ).fetchall()
            hidden = self._conn.execute(
                "SELECT COUNT(*) c FROM nodes WHERE root_id=? AND parent_key=? AND size<?",
                (root_id, parent_key, min_size),
            ).fetchone()["c"]
            total = self._conn.execute(
                "SELECT COUNT(*) c FROM nodes WHERE root_id=? AND parent_key=? AND size>=?",
                (root_id, parent_key, min_size),
            ).fetchone()["c"]
        return [dict(r) for r in rows], hidden, total

    def ancestors(self, root_id: int, key: str) -> list[dict]:
        """从根到该节点的祖先链（不含自身）。"""
        chain = []
        cur = key
        guard = 0
        while guard < 200:
            guard += 1
            with self._lock:
                row = self._conn.execute(
                    "SELECT parent_key FROM nodes WHERE root_id=? AND key=?", (root_id, cur)
                ).fetchone()
            if not row:
                break
            pk = row["parent_key"]
            if not pk:
                break
            with self._lock:
                parent = self._conn.execute(
                    "SELECT * FROM nodes WHERE root_id=? AND key=?", (root_id, pk)
                ).fetchone()
            if not parent:
                break
            chain.append(dict(parent))
            cur = pk
        chain.reverse()
        return chain

    def search(self, root_id: int | None, query: str, min_size: int, limit: int = 60) -> list[dict]:
        q = f"%{query.strip()}%"
        sql = "SELECT * FROM nodes WHERE name LIKE ? AND size>=?"
        args: list = [q, min_size]
        if root_id:
            sql += " AND root_id=?"
            args.append(root_id)
        sql += " ORDER BY size DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            with self._lock:
                rr = self._conn.execute(
                    "SELECT label, path FROM roots WHERE id=?", (d["root_id"],)
                ).fetchone()
            d["root_label"] = rr["label"] if rr else ""
            d["root_path"] = rr["path"] if rr else ""
            out.append(d)
        return out

    def top_nodes(self, root_id: int, min_size: int, limit: int = 20,
                  only_dirs: bool = False) -> list[dict]:
        sql = "SELECT * FROM nodes WHERE root_id=? AND size>=?"
        args: list = [root_id, min_size]
        if only_dirs:
            sql += " AND is_dir=1"
        sql += " ORDER BY size DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 标注
    def notes_for(self, keys: list[str]) -> dict:
        if not keys:
            return {}
        out = {}
        chunk = 400
        with self._lock:
            for i in range(0, len(keys), chunk):
                part = keys[i:i + chunk]
                ph = ",".join("?" * len(part))
                for row in self._conn.execute(
                    f"SELECT key, note, tags, flagged FROM notes WHERE key IN ({ph})", part
                ):
                    out[row["key"]] = {"note": row["note"], "tags": row["tags"],
                                       "flagged": bool(row["flagged"])}
        return out

    def set_note(self, path: str, note: str = "", tags: str = "",
                 flagged: bool = False) -> dict:
        key = pkey(path)
        now = now_iso()
        with self._lock:
            if not note and not tags and not flagged:
                self._conn.execute("DELETE FROM notes WHERE key=?", (key,))
            else:
                self._conn.execute(
                    """INSERT INTO notes(key, path, note, tags, flagged, updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET note=excluded.note, tags=excluded.tags,
                       flagged=excluded.flagged, updated_at=excluded.updated_at""",
                    (key, norm_path(path), note, tags, int(flagged), now),
                )
            self._conn.commit()
        return {"key": key, "note": note, "tags": tags, "flagged": flagged,
                "updated_at": now}

    def all_notes(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notes ORDER BY flagged DESC, updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 事件
    def add_event(self, kind: str, label: str = "", path: str = "",
                  size: int = 0, message: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events(ts, kind, path, label, size, message) VALUES(?,?,?,?,?,?)",
                (now_iso(), kind, path or "", label or "", int(size or 0), message or ""),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_events(self, since_id: int = 0, limit: int = 120) -> list[dict]:
        with self._lock:
            if since_id:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?",
                    (since_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def clear_events(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events")
            self._conn.commit()

    # ---------------------------------------------------------------- 安装清单
    def upsert_app(self, entry: dict, mark_new: bool = False) -> bool:
        """返回 True 表示这是首次见到该条目。mark_new 仅作用于新插入的条目。"""
        now = now_iso()
        with self._lock:
            row = self._conn.execute("SELECT key FROM apps WHERE key=?", (entry["key"],)).fetchone()
            first = row is None
            if first:
                self._conn.execute(
                    """INSERT INTO apps(key, name, path, size, version, publisher, install_date,
                                        reg_key, first_seen, last_seen, is_new)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (entry["key"], entry.get("name", ""), entry.get("path", ""),
                     int(entry.get("size") or 0), entry.get("version", ""),
                     entry.get("publisher", ""), entry.get("install_date", ""),
                     entry.get("reg_key", ""), now, now, 1 if mark_new else 0),
                )
            else:
                # 已存在的条目只刷新元数据，不改变 NEW 标记（否则每轮轮询都会重新点亮 NEW）
                self._conn.execute(
                    """UPDATE apps SET name=?, path=?, size=?, version=?, publisher=?,
                       install_date=?, last_seen=? WHERE key=?""",
                    (entry.get("name", ""), entry.get("path", ""), int(entry.get("size") or 0),
                     entry.get("version", ""), entry.get("publisher", ""),
                     entry.get("install_date", ""), now, entry["key"]),
                )
            self._conn.commit()
        return first

    def list_apps(self, query: str = "", only_tracked: bool = False) -> list[dict]:
        sql = "SELECT apps.*, roots.label AS root_label FROM apps LEFT JOIN roots ON roots.id=apps.root_id WHERE 1=1"
        args: list = []
        if query:
            sql += " AND (apps.name LIKE ? OR apps.path LIKE ?)"
            args += [f"%{query}%", f"%{query}%"]
        if only_tracked:
            sql += " AND apps.root_id IS NOT NULL"
        sql += " ORDER BY apps.is_new DESC, apps.size DESC, apps.name ASC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def track_app(self, app_key: str, root_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE apps SET root_id=?, is_new=0 WHERE key=?", (root_id, app_key))
            self._conn.commit()

    def app_exists(self, app_key: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM apps WHERE key=?", (app_key,)).fetchone() is not None

    def clear_apps_new_flags(self) -> None:
        with self._lock:
            self._conn.execute("UPDATE apps SET is_new=0")
            self._conn.commit()

    # ---------------------------------------------------------------- 统计
    def totals(self) -> dict:
        with self._lock:
            r = self._conn.execute(
                """SELECT COUNT(*) roots, COALESCE(SUM(total_bytes),0) total,
                          COALESCE(SUM(file_count),0) files, COUNT(*) FILTER (WHERE status='ready') ready
                   FROM roots"""
            ).fetchone()
            n = self._conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
            notes = self._conn.execute("SELECT COUNT(*) c FROM notes WHERE note<>'' OR tags<>''").fetchone()["c"]
            flagged = self._conn.execute("SELECT COUNT(*) c FROM notes WHERE flagged=1").fetchone()["c"]
            newapps = self._conn.execute("SELECT COUNT(*) c FROM apps WHERE is_new=1").fetchone()["c"]
            newroots = self._conn.execute("SELECT COUNT(*) c FROM roots WHERE is_new=1").fetchone()["c"]
        return {"roots": r["roots"], "total_bytes": r["total"], "files": r["files"],
                "nodes": n, "notes": notes, "flagged": flagged,
                "new_apps": newapps, "new_roots": newroots, "ready_roots": r["ready"]}
