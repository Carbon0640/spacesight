"""目录扫描引擎。

设计要点
--------
1. 显式栈 DFS：不使用递归，避免深层目录触发 RecursionError；内存占用只与"目录深度"相关。
2. 后序聚合：进入目录时压栈，目录迭代结束时把 (文件数, 总字节, 子目录数) 回填父节点，
   因此每个目录都能得到完整子树大小，代价是每个文件只 stat 一次。
3. 重解析点（junction / symlink）一律跳过：避免 C:\\Users\\All Users 之类的回环与重复计数。
4. 顶层子目录用线程池并行（磁盘 IO 密集，4 线程通常快 2~3 倍），结果批量写入 SQLite 快照。
5. 权限拒绝、路径过长等 OSError 全部吞掉并计数，扫描不中断。
"""
from __future__ import annotations

import heapq
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

REPARSE_POINT = 0x400

# 默认跳过的目录名（仅当扫描目标是盘符根时生效，避免无意义的系统开销）
SKIP_NAMES = {
    "$recycle.bin", "system volume information", "config.msi", "$winreagent",
    "recovery", "perflogs", "documents and settings",
}


class ScanContext:
    """一次扫描的共享状态：进度计数 + 取消 + 目标快照落库。"""

    def __init__(self, job, min_keep: int, sink):
        self.job = job
        self.min_keep = min_keep
        self.sink = sink
        self.lock = threading.Lock()
        self.files = 0
        self.dirs = 0
        self.bytes = 0
        self.denied = 0
        self._last_emit = 0.0

    def tick(self, path: str, files: int = 0, nbytes: int = 0, dirs: int = 0) -> None:
        with self.lock:
            self.files += files
            self.bytes += nbytes
            self.dirs += dirs
            f, b, d = self.files, self.bytes, self.dirs
            now = time.time()
            # 限流：每 250ms 最多向任务推送一次进度，避免高频锁竞争
            emit = (now - self._last_emit) >= 0.25
            if emit:
                self._last_emit = now
        if emit:
            self.job.progress(files=f, nbytes=b, dirs=d, current=path)

    def deny(self) -> None:
        with self.lock:
            self.denied += 1

    @property
    def cancelled(self) -> bool:
        return self.job.cancelled


class _Frame:
    __slots__ = ("path", "key", "name", "depth", "parent_key", "size",
                 "files", "dirs", "mtime", "it")

    def __init__(self, path: str, name: str, depth: int, parent_key: str, mtime: float):
        self.path = path
        self.key = path.lower()
        self.name = name
        self.depth = depth
        self.parent_key = parent_key
        self.size = 0
        self.files = 0
        self.dirs = 0
        self.mtime = mtime
        self.it = None


def _open_frame(path: str, name: str, depth: int, parent_key: str,
                mtime: float) -> _Frame | None:
    frame = _Frame(path, name, depth, parent_key, mtime)
    try:
        frame.it = os.scandir(path)
    except OSError:
        return None
    return frame


def _finalize(stack: list[_Frame], ctx: ScanContext, emit: bool = True) -> _Frame:
    f = stack.pop()
    if f.it is not None:
        try:
            f.it.close()
        except Exception:
            pass
        f.it = None
    if emit:
        ctx.sink.add((f.key, f.path, f.parent_key, f.name, 1,
                      f.size, f.files, f.dirs, f.mtime, f.depth))
    if stack:
        parent = stack[-1]
        parent.size += f.size
        parent.files += f.files
        parent.dirs += f.dirs + 1
    return f


def walk_dir(top_path: str, top_name: str, top_depth: int, top_parent_key: str,
             top_mtime: float, ctx: ScanContext) -> tuple[int, int, int]:
    """扫描 top_path 整棵子树，返回 (size, files, dirs)。节点通过 ctx.sink 落库。"""
    root_frame = _open_frame(top_path, top_name, top_depth, top_parent_key, top_mtime)
    if root_frame is None:
        ctx.deny()
        return 0, 0, 0
    ctx.tick(top_path, dirs=1)
    stack = [root_frame]
    while stack:
        if ctx.cancelled:
            break
        frame = stack[-1]
        try:
            entry = next(frame.it)
        except StopIteration:
            _finalize(stack, ctx)
            continue
        except OSError:
            ctx.deny()
            _finalize(stack, ctx)
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            ctx.deny()
            continue
        if getattr(st, "st_file_attributes", 0) & REPARSE_POINT:
            continue
        mode = st.st_mode
        if stat.S_ISDIR(mode):
            child = _open_frame(entry.path, entry.name, frame.depth + 1, frame.key,
                                st.st_mtime)
            if child is None:
                ctx.deny()
                continue
            ctx.tick(entry.path, dirs=1)
            stack.append(child)
        elif stat.S_ISREG(mode):
            size = st.st_size
            frame.size += size
            frame.files += 1
            ctx.tick(entry.path, files=1, nbytes=size)
            if size >= ctx.min_keep:
                ctx.sink.add((entry.path.lower(), entry.path, frame.key, entry.name,
                              0, size, 1, 0, st.st_mtime, frame.depth + 1))
        # 其他类型（管道 / 设备 / socket）忽略
    while stack:  # 被取消时把剩余帧弹出，保证 sink 中已有节点的父子关系完好
        _finalize(stack, ctx, emit=False)
    return root_frame.size, root_frame.files, root_frame.dirs


# --------------------------------------------------------------------------- Sink
class DbSink:
    """把扫描结果批量写入 nodes 表；超过 max_nodes 后停止记录（仍继续统计大小）。"""

    def __init__(self, store, root_id: int, batch: int = 5000, max_nodes: int = 600000):
        self.store = store
        self.root_id = root_id
        self.batch = batch
        self.max_nodes = max_nodes
        self.lock = threading.Lock()
        self.rows: list[tuple] = []
        self.total = 0
        self.truncated = False

    def add(self, row: tuple) -> None:
        do_flush = None
        with self.lock:
            if self.total >= self.max_nodes:
                self.truncated = True
                return
            self.rows.append(row)
            self.total += 1
            if len(self.rows) >= self.batch:
                do_flush = self.rows
                self.rows = []
        if do_flush:
            self.store.insert_nodes(self.root_id, do_flush)

    def flush(self) -> None:
        with self.lock:
            rows, self.rows = self.rows, []
        if rows:
            self.store.insert_nodes(self.root_id, rows)


class AuditSink:
    """体检模式：不落库，只保留最大的若干文件与目录。"""

    def __init__(self, top_n: int = 100):
        self.top_n = top_n
        self.lock = threading.Lock()
        self.files: list[tuple] = []
        self.dirs: list[tuple] = []

    def add(self, row: tuple) -> None:
        key, path, parent_key, name, is_dir, size, files, dirs, mtime, depth = row
        if is_dir and depth == 0:
            return  # 体检根自身不计入"Top 目录"
        item = (size, path, name, files, mtime, depth)
        heap = self.files if not is_dir else self.dirs
        with self.lock:
            if len(heap) < self.top_n:
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)

    def result(self) -> dict:
        def dump(heap):
            out = [{"size": h[0], "path": h[1], "name": h[2], "files": h[3],
                    "mtime": h[4], "depth": h[5]} for h in heap]
            out.sort(key=lambda x: -x["size"])
            return out
        return {"files": dump(self.files), "dirs": dump(self.dirs)}


# --------------------------------------------------------------------------- 入口
def _top_children(path: str) -> list[tuple]:
    """列出顶层子项：[(path, name, is_dir, size, mtime)]"""
    out = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if getattr(st, "st_file_attributes", 0) & REPARSE_POINT:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    out.append((entry.path, entry.name, True, 0, st.st_mtime))
                elif stat.S_ISREG(st.st_mode):
                    out.append((entry.path, entry.name, False, st.st_size, st.st_mtime))
    except OSError:
        pass
    return out


def scan_root(store, root: dict, job, cfg: dict) -> dict:
    """扫描一个追踪根并写入快照。返回统计信息。"""
    started = time.time()
    root_id, path, key = root["id"], root["path"], root["key"]
    job.note(f"准备扫描 {path}")
    store.update_root(root_id, status="scanning")

    sink = DbSink(store, root_id, max_nodes=int(cfg.get("max_nodes", 600000)))
    ctx = ScanContext(job, int(cfg.get("min_file_keep", 65536)), sink)
    store.clear_nodes(root_id)

    total_bytes = total_files = total_dirs = 0
    try:
        if not os.path.exists(path):
            store.update_root(root_id, status="missing", total_bytes=0, file_count=0,
                              dir_count=0, scanned_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            job.finish("done", {"missing": True, "total_bytes": 0})
            return {"missing": True, "total_bytes": 0, "file_count": 0, "dir_count": 0,
                    "nodes": 0, "denied": 0, "scan_ms": 0, "truncated": False}

        if os.path.isfile(path):
            st = os.stat(path)
            sink.add((key, path, "", os.path.basename(path), 0, st.st_size, 1, 0,
                      st.st_mtime, 1))
            sink.flush()
            total_bytes, total_files = st.st_size, 1
            job.progress(files=1, nbytes=st.st_size, dirs=0, current=path)
        else:
            children = _top_children(path)
            if path.rstrip("\\/").lower() in {f"{d}:" for d in
                                             "abcdefghijklmnopqrstuvwxyz"}:
                children = [c for c in children if c[1].lower() not in SKIP_NAMES]
            file_children = [c for c in children if not c[2]]
            dir_children = [c for c in children if c[2]]

            for cpath, cname, _isdir, csize, cmtime in file_children:
                if csize >= int(cfg.get("min_file_keep", 65536)):
                    sink.add((cpath.lower(), cpath, key, cname, 0, csize, 1, 0, cmtime, 1))
                total_bytes += csize
                total_files += 1
                ctx.tick(cpath, files=1, nbytes=csize)
            sink.flush()

            workers = max(1, min(int(cfg.get("scan_workers", 4)), len(dir_children) or 1))
            if workers == 1:
                for cpath, cname, _isdir, _s, cmtime in dir_children:
                    size, files, dirs = walk_dir(cpath, cname, 1, key, cmtime, ctx)
                    total_bytes += size
                    total_files += files
                    total_dirs += dirs + 1
            else:
                def _one(item):
                    cpath, cname, _isdir, _s, cmtime = item
                    return walk_dir(cpath, cname, 1, key, cmtime, ctx)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for size, files, dirs in pool.map(_one, dir_children):
                        total_bytes += size
                        total_files += files
                        total_dirs += dirs + 1
            sink.flush()
    except Exception as exc:  # 兜底，避免后台线程静默死亡
        sink.flush()
        store.update_root(root_id, status="error", scan_ms=int((time.time() - started) * 1000))
        job.fail(f"{type(exc).__name__}: {exc}")
        return {"error": str(exc)}

    elapsed_ms = int((time.time() - started) * 1000)
    status = "ready" if not job.cancelled else "partial"
    store.update_root(
        root_id,
        status=status,
        total_bytes=total_bytes,
        file_count=total_files,
        dir_count=total_dirs,
        scan_ms=elapsed_ms,
        scanned_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    if sink.truncated:
        job.warn(f"节点数超过上限 {sink.max_nodes}，已截断记录（大小统计仍然准确）")
    result = {"total_bytes": total_bytes, "file_count": total_files, "dir_count": total_dirs,
              "nodes": sink.total, "denied": ctx.denied, "scan_ms": elapsed_ms,
              "truncated": sink.truncated, "cancelled": job.cancelled}
    job.finish("cancelled" if job.cancelled else "done", result)
    return result


def audit_path(path: str, job, min_size: int = 100 * 1024 * 1024,
               top_n: int = 100) -> dict:
    """大文件体检：扫描目录，返回最大的文件与子目录清单（不写数据库）。"""
    started = time.time()
    sink = AuditSink(top_n=top_n)
    ctx = ScanContext(job, min_keep=min_size, sink=sink)
    if not os.path.exists(path):
        job.fail(f"路径不存在: {path}")
        return {"error": "路径不存在"}
    if os.path.isfile(path):
        st = os.stat(path)
        sink.add((path.lower(), path, "", os.path.basename(path), 0, st.st_size, 1, 0,
                  st.st_mtime, 1))
        res = sink.result()
        job.finish("done", res)
        return res

    base = path.rstrip("\\/") or path
    mtime = os.path.getmtime(path)
    walk_dir(path, os.path.basename(base), 0, "", mtime, ctx)
    res = sink.result()
    res.update({"scanned_bytes": ctx.bytes, "files_scanned": ctx.files,
                "denied": ctx.denied, "scan_ms": int((time.time() - started) * 1000),
                "path": path, "min_size": min_size})
    job.finish("cancelled" if job.cancelled else "done", res)
    return res
