""""自主察觉"模块 —— SpaceSight 的核心感知层。

三路信号融合（都无需管理员权限）：

1. 注册表台账（每 5s）：遍历 HKLM/HKCU 的 Uninstall 键，拿到 DisplayName /
   InstallLocation / EstimatedSize。所有已安装程序形成一个目录台账；首次运行建立
   基线（静默导入），之后新出现的条目即视为"刚刚装了什么"。
2. 安装进程监控（每 3s）：用 Toolhelp 快照枚举进程名，匹配 setup/install/msiexec/
   7z/winrar 等安装、解压类特征。安装器启动时对常用安装目录拍快照，退出后 diff，
   找出新增目录（覆盖绿色版/免安装/解压型程序）。
3. 目录变更侦测（每 20s）：对常用安装位置、固定磁盘根、以及所有追踪根做"一级子项"
   差异比对。安装位置出现新目录 → 自动纳入追踪；追踪根内出现新目录 → 自动重扫该根。

设计原则：所有信号都是"提示"，永不阻塞 UI；注册表提供器 / 进程提供器可注入，
便于离线单测。
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time

# --------------------------------------------------------------------------- 常量
INSTALLER_RE = re.compile(
    r"(setup|install|installer|unpack|extract|expand|msiexec|inno|nsis|dxsetup|"
    r"vc_?redist|dotnetfx|bundle|7zg|7zfm|winrar|unrar|bandizip|peazip|haozip|"
    r"2345zip|360zip|patcher|update_|_update|updater)", re.I)
INSTALLER_EXCLUDE_RE = re.compile(
    r"(unins|trustedinstaller|tiworker|usoclient|wusa|wuauclt|musnotification|"
    r"setup_?check|spacesight|python|msedge|chrome|crashpad|software_reporter)", re.I)

SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "config.msi", "$winreagent",
    "recovery", "perflogs", "documents and settings", "windows.old", "temp", "tmp",
    "node_modules", ".git", "$sysreset", "found.000", "onedrivetemp", "msdownld.tmp",
}
SKIP_DIR_PREFIX = ("$", "~", ".", "ntuser")

# 大文件提示阈值：安装位置新增的、超过该大小的单个文件会出现在事件流里
BIG_FILE_HINT = 500 * 1024 * 1024

# --------------------------------------------------------------------------- 注册表
_UNINSTALL_KEYS = (
    ("HKLM64", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0x0100),
    ("HKLM64", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", 0x0100),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
)

_UPDATE_NAME_RE = re.compile(
    r"^(security update|update for|hotfix|kb\d{6,}|visual c\+\+ \d{4} redistributable|"
    r"microsoft \.net|windows sdk|windows driver)", re.I)


def read_registry_installs() -> dict[str, dict]:
    """读取注册表安装台账。返回 {key: entry}。

    非 Windows 或读取失败时返回空字典（保证程序在开发环境也能跑）。
    """
    try:
        import winreg  # noqa: F401
    except Exception:
        return {}

    result: dict[str, dict] = {}
    roots = {"HKLM64": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
    for hive_name, subkey, extra in _UNINSTALL_KEYS:
        hive = roots[hive_name]
        try:
            key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | extra)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
        except OSError:
            continue
        for i in range(count):
            try:
                name = winreg.EnumKey(key, i)
            except OSError:
                continue
            full_key = f"{subkey}\\{name}"
            try:
                with winreg.OpenKey(hive, full_key, 0, winreg.KEY_READ | extra) as sk:
                    def val(n, default=""):
                        try:
                            return winreg.QueryValueEx(sk, n)[0]
                        except OSError:
                            return default
                    display = str(val("DisplayName") or "").strip()
                    if not display or _UPDATE_NAME_RE.search(display):
                        continue
                    if int(val("SystemComponent", 0) or 0) == 1:
                        continue
                    if val("ParentKeyName", ""):
                        continue
                    location = str(val("InstallLocation") or "").strip().strip('"')
                    est_kb = int(val("EstimatedSize", 0) or 0)
                    entry = {
                        "name": display,
                        "path": location,
                        "size": est_kb * 1024,
                        "version": str(val("DisplayVersion") or ""),
                        "publisher": str(val("Publisher") or ""),
                        "install_date": str(val("InstallDate") or ""),
                        "reg_key": full_key,
                    }
                    result[f"{hive_name}|{full_key}"] = entry
            except OSError:
                continue
        try:
            key.Close()
        except Exception:
            pass
    return result


# --------------------------------------------------------------------------- 进程
def list_process_names() -> set[str]:
    """用 Toolhelp32 快照枚举当前进程的可执行文件名（小写），无需管理员权限。"""
    names: set[str] = set()
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return names
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
            while ok:
                names.add(entry.szExeFile.lower())
                ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
        finally:
            k32.CloseHandle(ctypes.c_void_p(snap))
    except Exception:
        return names
    return names


# --------------------------------------------------------------------------- 侦测器
def _install_watch_dirs() -> list[tuple[str, str]]:
    """常用安装位置。返回 (目录, 模式)。

    模式 add    = 出现新目录时自动纳入追踪（软件真正安装的地方）
    模式 notify = 只记录事件、不自动追踪（AppData / ProgramData 这类目录变化极其频繁，
                  自动追踪会把浏览器缓存、扩展配置等碎片塞满追踪列表）
    """
    out: list[tuple[str, str]] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        p = os.environ.get(env)
        if p and os.path.isdir(p):
            out.append((p, "add"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        p = os.path.join(local, "Programs")
        if os.path.isdir(p):
            out.append((p, "add"))
        if os.path.isdir(local):
            out.append((local, "notify"))
    roaming = os.environ.get("APPDATA")
    if roaming and os.path.isdir(roaming):
        out.append((roaming, "notify"))
    pd = os.environ.get("ProgramData")
    if pd and os.path.isdir(pd):
        out.append((pd, "notify"))
    return out


def _fixed_drives() -> list[str]:
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if os.path.exists(root):
            try:
                import ctypes
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
                if drive_type == 3:  # DRIVE_FIXED
                    out.append(root)
            except Exception:
                out.append(root)
    return out


class Detector(threading.Thread):
    """后台侦测线程。回调：on_install(entry) / on_folder(entry) / on_root_change(root_id, entry)"""

    def __init__(self, store, on_install=None, on_folder=None, on_root_change=None,
                 registry_provider=read_registry_installs, process_provider=list_process_names,
                 log=print):
        super().__init__(daemon=True, name="spacesight-detector")
        self.store = store
        self.on_install = on_install or (lambda e: None)
        self.on_folder = on_folder or (lambda e: None)
        self.on_root_change = on_root_change or (lambda rid, e: None)
        self.registry_provider = registry_provider
        self.process_provider = process_provider
        self.log = log
        self._stop = threading.Event()
        self.active_installers: set[str] = set()
        self.installer_snapshots: dict[str, dict[str, set[str]]] = {}
        self.known_dirs: dict[str, set[str]] = {}
        self._change_stamp: dict[int, float] = {}
        self.last_ticks = {"registry": 0.0, "process": 0.0, "folder": 0.0}
        self.status = {"registry_ok": 0, "apps": 0, "installers": [], "watched": 0,
                       "last_registry": 0.0, "last_folder": 0.0}

    # ---------------------------------------------------------------- 生命周期
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.log("[detector] 启动察觉线程")
        # 尊重"后台察觉"开关：暂停状态下启动时也不做任何轮询
        if self.store.get_setting("watch_enabled", 1):
            self._safe(self.poll_registry, "registry")
            self._safe(self.poll_folder_watch, "folder")
            self._safe(self.poll_processes, "process")
        while not self._stop.is_set():
            if self.store.get_setting("watch_enabled", 1):
                now = time.time()
                if now - self.last_ticks["registry"] >= 5:
                    self._safe(self.poll_registry, "registry")
                if now - self.last_ticks["process"] >= 3:
                    self._safe(self.poll_processes, "process")
                if now - self.last_ticks["folder"] >= 20:
                    self._safe(self.poll_folder_watch, "folder")
            self._stop.wait(1.0)

    def _safe(self, fn, tag: str) -> None:
        try:
            fn()
        except Exception as exc:  # 侦测失败不能影响主程序
            self.last_ticks[tag] = time.time()
            self.log(f"[detector] {tag} 异常: {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 注册表
    def poll_registry(self, baseline_silent: bool = True) -> int:
        self.last_ticks["registry"] = time.time()
        installs = self.registry_provider() or {}
        first = str(self.store.get_setting("apps_baseline_done", "0")) != "1"
        new_entries = []
        for key, entry in installs.items():
            entry = dict(entry)
            entry["key"] = key
            created = self.store.upsert_app(entry, mark_new=not first)
            if created and not first:
                new_entries.append(entry)
        self.status["registry_ok"] = 1
        self.status["apps"] = len(installs)
        self.status["last_registry"] = time.time()
        if first:
            self.store.set_setting("apps_baseline_done", "1")
            self.store.add_event("registry", label="安装台账基线已建立",
                                 message=f"共导入 {len(installs)} 个已安装程序")
            self.log(f"[detector] 台账基线: {len(installs)} 条")
            return 0
        for entry in new_entries:
            path = (entry.get("path") or "").strip()
            size = int(entry.get("size") or 0)
            self.store.add_event(
                "install", label=entry.get("name", ""), path=path, size=size,
                message=f"检测到新安装程序 v{entry.get('version') or '?'}"
                        + ("（注册表未提供安装路径）" if not path else ""))
            self.log(f"[detector] 新安装: {entry.get('name')} -> {path or '(无路径)'}")
            self.on_install(entry)
        return len(new_entries)

    # ---------------------------------------------------------------- 进程
    def poll_processes(self) -> None:
        self.last_ticks["process"] = time.time()
        names = set(self.process_provider() or set())
        candidates = {n for n in names
                      if INSTALLER_RE.search(n) and not INSTALLER_EXCLUDE_RE.search(n)}
        started = candidates - self.active_installers
        finished = self.active_installers - candidates
        self.active_installers = candidates
        self.status["installers"] = sorted(candidates)[:8]

        for name in sorted(started):
            self.store.add_event("installer", label=name, message="安装/解压程序启动，开始监视文件落点")
            self.log(f"[detector] 安装器启动: {name}")
            self.installer_snapshots[name] = self._snapshot_watch_dirs()

        for name in sorted(finished):
            snap = self.installer_snapshots.pop(name, None)
            if snap:
                new_dirs = self._diff_dirs(snap)
                self.poll_registry()
                self._report_new_dirs(new_dirs, reason=f"{name} 退出后")
            self.store.add_event("installer", label=name, message="安装/解压程序已退出")
            self.log(f"[detector] 安装器退出: {name}")

    # ---------------------------------------------------------------- 目录侦测
    def _watch_targets(self) -> list[tuple[str, str, int]]:
        """[(目录, 模式, root_id)]，模式：'add'（新目录纳入追踪）/ 'notify'（只记事件）/ 'rescan'（重扫该根）"""
        targets: list[tuple[str, str, int]] = []
        seen: dict[str, int] = {}          # 路径 -> targets 下标，便于合并更强的模式

        def add(path: str, mode: str, root_id: int = 0) -> None:
            key = path.lower()
            if key in seen:
                idx = seen[key]
                if targets[idx][1] == "notify" and mode != "notify":
                    targets[idx] = (path, mode, root_id)
                return
            seen[key] = len(targets)
            targets.append((path, mode, root_id))

        for d, mode in _install_watch_dirs():
            add(d, mode)
        for d in _fixed_drives():
            add(d, "add")
        for root in self.store.list_roots():
            if root.get("kind") == "file":
                continue
            p = root.get("path") or ""
            if p and os.path.isdir(p):
                add(p, "rescan", int(root["id"]))
        return targets

    def _snapshot_watch_dirs(self) -> dict[str, set[str]]:
        snap = {}
        for path, _mode, _rid in self._watch_targets():
            try:
                with os.scandir(path) as it:
                    snap[path] = {e.name for e in it}
            except OSError:
                continue
        return snap

    def _diff_dirs(self, snap: dict[str, set[str]]) -> dict[str, dict]:
        """返回 {(父目录, 新名字): {...}}"""
        found = {}
        for path, old_names in snap.items():
            try:
                with os.scandir(path) as it:
                    current = {e.name for e in it}
            except OSError:
                continue
            for name in current - old_names:
                low = name.lower()
                if low in SKIP_DIR_NAMES or low.startswith(SKIP_DIR_PREFIX):
                    continue
                full = os.path.join(path, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                import stat as _stat
                found[full.lower()] = {
                    "path": full, "name": name,
                    "is_dir": _stat.S_ISDIR(st.st_mode),
                    "size": 0 if _stat.S_ISDIR(st.st_mode) else st.st_size,
                    "mtime": st.st_mtime, "parent": path,
                }
        return found

    def poll_folder_watch(self) -> int:
        self.last_ticks["folder"] = time.time()
        targets = self._watch_targets()
        self.status["watched"] = len(targets)
        self.status["last_folder"] = time.time()
        current: dict[str, set[str]] = {}
        for path, _mode, _rid in targets:
            try:
                with os.scandir(path) as it:
                    current[path] = {e.name for e in it}
            except OSError:
                continue
        if not self.known_dirs:  # 首次建立基线
            self.known_dirs = current
            return 0
        mode_by_path = {p: (m, rid) for p, m, rid in targets}
        new_items: list[tuple[str, dict]] = []
        for path, names in current.items():
            mode, rid = mode_by_path.get(path, ("add", 0))
            prev = self.known_dirs.get(path)
            if prev is None:
                continue  # 新纳入监视的位置：只建立基线，不当作"新增"
            for name in names - prev:
                low = name.lower()
                if low in SKIP_DIR_NAMES or low.startswith(SKIP_DIR_PREFIX):
                    continue
                full = os.path.join(path, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                import stat as _stat
                is_dir = _stat.S_ISDIR(st.st_mode)
                if not is_dir and st.st_size < BIG_FILE_HINT:
                    continue
                new_items.append((mode, {
                    "path": full, "name": name, "is_dir": is_dir,
                    "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime,
                    "parent": path, "root_id": rid,
                }))
        self.known_dirs = current
        for mode, item in new_items[:20]:
            if mode == "rescan":
                # 追踪根内的变化：日志 10 分钟最多记一条，避免 VS Code 配置目录之类刷屏
                last = self._change_stamp.get(item["root_id"], 0.0)
                if time.time() - last > 600:
                    self._change_stamp[item["root_id"]] = time.time()
                    self.store.add_event("change", label=item["name"], path=item["path"],
                                         message="追踪目录内出现新内容，已排队重扫")
                self.on_root_change(item["root_id"], item)
            elif mode == "notify":
                if item["is_dir"]:
                    self.store.add_event("folder", label=item["name"], path=item["path"],
                                         message=f"安装位置出现新目录：{item['parent']}（仅记录）")
                else:
                    self.store.add_event("bigfile", label=item["name"], path=item["path"],
                                         size=item["size"], message="安装位置出现大文件")
            elif item["is_dir"]:
                self.store.add_event("folder", label=item["name"], path=item["path"],
                                     message=f"安装位置出现新目录：{item['parent']}")
                self.on_folder(item)
            else:
                self.store.add_event("bigfile", label=item["name"], path=item["path"],
                                     size=item["size"], message="安装位置出现大文件")
        return len(new_items)

    def _report_new_dirs(self, new_dirs: dict[str, dict], reason: str) -> None:
        for item in list(new_dirs.values())[:20]:
            if not item["is_dir"]:
                if item["size"] >= BIG_FILE_HINT:
                    self.store.add_event("bigfile", label=item["name"], path=item["path"],
                                         size=item["size"], message=f"{reason}出现大文件")
                continue
            self.store.add_event("folder", label=item["name"], path=item["path"],
                                 message=f"{reason}检测到新增目录：{item['parent']}")
            self.on_folder(item)

    # ---------------------------------------------------------------- 手动触发
    def detect_now(self) -> dict:
        n_new_apps = self._safe_ret(self.poll_registry, 0)
        self._safe(self.poll_folder_watch, "folder")
        return {"new_apps": n_new_apps, "installers": self.status.get("installers", []),
                "watched": self.status.get("watched", 0),
                "apps": self.status.get("apps", 0)}

    def _safe_ret(self, fn, default):
        try:
            return fn()
        except Exception:
            return default

    def status_dict(self) -> dict:
        return {
            "apps": self.status.get("apps", 0),
            "installers": self.status.get("installers", []),
            "watched": self.status.get("watched", 0),
            "registry_ok": bool(self.status.get("registry_ok")),
            "last_registry": self.status.get("last_registry", 0),
            "last_folder": self.status.get("last_folder", 0),
            "running": not self._stop.is_set(),
        }


def disk_usage(path: str | None = None) -> dict:
    try:
        target = path or (os.environ.get("SystemDrive", "C:") + "\\")
        usage = shutil.disk_usage(target)
        return {"total": usage.total, "used": usage.used, "free": usage.free,
                "path": target}
    except Exception:
        return {}
