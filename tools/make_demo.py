# -*- coding: utf-8 -*-
"""生成"演示数据"库：用于文档截图，绝不读取真实磁盘/注册表。

做法：
  1. 新建一个独立 data 目录（--data-dir 使用），关掉后台察觉；
  2. 把安装台账直接写成一批评虚构条目（避免把本机真实软件清单带进截图）；
  3. 追踪目标在服务启动后由脚本通过 API 添加（指向 C:\\SpaceSightDemo 演示目录）。

用法： python tools/make_demo.py <data-dir>
"""
import os
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app.store import Store  # noqa: E402

MB = 1024 * 1024

DEMO_APPS = [
    ("demo|blender",   "Blender 4.2 LTS",          "Blender Foundation",        "4.2.0",     612 * MB, r"C:\Program Files\Blender Foundation\Blender 4.2", 0),
    ("demo|davinci",   "DaVinci Resolve",          "Blackmagic Design",         "19.0.2",   2100 * MB, r"C:\Program Files\Blackmagic Design\DaVinci Resolve", 0),
    ("demo|obs",       "OBS Studio",               "OBS Project",               "30.2.3",    320 * MB, r"C:\Program Files\obs-studio", 0),
    ("demo|steam",     "Steam",                    "Valve Corporation",         "2.10.91.91", 980 * MB, r"C:\Program Files (x86)\Steam", 1),
    ("demo|vscode",    "Visual Studio Code",        "Microsoft Corporation",     "1.93.1",    410 * MB, r"C:\Program Files\Microsoft VS Code", 0),
    ("demo|7zip",      "7-Zip 24.08 (x64)",        "Igor Pavlov",               "24.08",       6 * MB, r"C:\Program Files\7-Zip", 0),
    ("demo|python",    "Python 3.12.7 (64-bit)",   "Python Software Foundation", "3.12.7150", 120 * MB, r"C:\Program Files\Python312", 0),
    ("demo|figma",     "Figma Agent",              "Figma, Inc.",               "125.7",     180 * MB, "", 0),
    ("demo|nodejs",    "Node.js",                  "OpenJS Foundation",         "22.9.0",    160 * MB, r"C:\Program Files\nodejs", 0),
    ("demo|affinity",  "Affinity Photo 2",         "Serif Europe Ltd.",         "2.5.5",     890 * MB, r"C:\Program Files\Affinity\Photo 2", 0),
]


def main() -> int:
    data_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                               os.path.join(BASE, "data", "_demo"))
    os.makedirs(data_dir, exist_ok=True)
    db = os.path.join(data_dir, "spacesight.db")
    store = Store(db)
    try:
        # 关掉后台察觉：演示库不读取真实注册表/目录变化
        store.set_setting("watch_enabled", 0)
        store.set_setting("apps_baseline_done", 1)
        store.set_setting("threshold", 100 * MB)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with store._lock:  # noqa: SLF001 - 演示脚本直接用内部连接
            store._conn.execute("DELETE FROM apps")
            for key, name, pub, ver, size, path, is_new in DEMO_APPS:
                store._conn.execute(
                    """INSERT INTO apps(key,name,path,size,version,publisher,install_date,
                                        reg_key,root_id,first_seen,last_seen,is_new)
                       VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?)""",
                    (key, name, path, size, ver, pub, "20250101", "", now, now, is_new))
            store._conn.commit()
        print(f"演示库已就绪: {db}")
        print(f"  后台察觉 = 关闭，安装台账 = {len(DEMO_APPS)} 条虚构数据")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
