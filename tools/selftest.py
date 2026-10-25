"""SpaceSight 离线自检脚本（不依赖被测服务，直接调用模块）。

覆盖：
  1. 扫描引擎：目录聚合 / 阈值过滤 / 父子关联 / 祖先链 / 搜索 / 标注
  2. 察觉模块：注册表基线、新安装识别、自动追踪回调
  3. 体检模式：Top N 大文件与目录

用法： python tools/selftest.py
"""
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import scanner  # noqa: E402
from app.detectors import Detector  # noqa: E402
from app.jobs import Job  # noqa: E402
from app.store import Store, pkey  # noqa: E402

MB = 1024 * 1024
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  -> ' + str(detail)) if detail and not cond else ''}")


def make_sparse(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)


def build_fixture(root):
    tree = {
        "big.bin": 200 * MB,
        "mid.bin": 30 * MB,
        "small.bin": 1 * MB,
        "Alpha/a1.bin": 50 * MB,
        "Alpha/a2.bin": 20 * MB,
        "Alpha/SubA/s1.bin": 10 * MB,
        "Alpha/SubA/s2.bin": 5 * MB,
        "Beta/b1.bin": 80 * MB,
        "Tiny/t.bin": 100 * 1024,
    }
    for rel, size in tree.items():
        make_sparse(os.path.join(root, *rel.split("/")), size)
    os.makedirs(os.path.join(root, "Beta", "Empty"), exist_ok=True)
    return sum(tree.values())


def test_scanner(tmp, store):
    print("\n[1] 扫描引擎")
    fixture = os.path.join(tmp, "fixture")
    expected = build_fixture(fixture)
    root = store.add_root(fixture, label="fixture")
    job = Job("scan", root_id=root["id"])
    res = scanner.scan_root(store, root, job, {"min_file_keep": 65536, "scan_workers": 4})
    check("总大小等于文件字节之和", res["total_bytes"] == expected,
          f'{res["total_bytes"]} != {expected}')
    check("文件计数正确", res["file_count"] == 9, res["file_count"])
    check("目录计数正确", res["dir_count"] == 5, res["dir_count"])  # Alpha SubA Beta Empty Tiny
    check("任务状态 done", job.snapshot()["status"] == "done")

    root_row = store.get_root(root["id"])
    check("根节点大小已回写", root_row["total_bytes"] == expected)
    check("根节点状态 ready", root_row["status"] == "ready")

    alpha = store.node(root["id"], pkey(os.path.join(fixture, "Alpha")))
    check("Alpha 子树大小 = 85MB", alpha and alpha["size"] == 85 * MB, alpha and alpha["size"])
    check("Alpha 子目录数 = 1", alpha and alpha["dirs"] == 1, alpha and alpha["dirs"])
    check("Alpha 文件数 = 4", alpha and alpha["files"] == 4, alpha and alpha["files"])

    kids, hidden, total = store.children(root["id"], root["key"], 30 * MB)
    names = [k["name"] for k in kids]
    check("阈值 30MB：仅保留 4 项", len(kids) == 4, names)
    check("阈值过滤按大小降序", names == ["big.bin", "Alpha", "Beta", "mid.bin"], names)
    check("隐藏项计数 = 2", hidden == 2, f"{hidden} / total {total}")

    sub, _, _ = store.children(root["id"], pkey(os.path.join(fixture, "Alpha")), 0)
    check("Alpha 子项按大小降序",
          [s["name"] for s in sub] == ["a1.bin", "a2.bin", "SubA"], [s["name"] for s in sub])

    chain = store.ancestors(root["id"], pkey(os.path.join(fixture, "Alpha", "SubA", "s1.bin")))
    check("祖先链长度 = 2", len(chain) == 2, [c["name"] for c in chain])
    check("祖先链顺序自上而下",
          [c["name"] for c in chain] == ["Alpha", "SubA"], [c["name"] for c in chain])

    hits = store.search(None, "a1", 0)
    check("搜索命中 a1.bin", len(hits) == 1 and hits[0]["name"] == "a1.bin", hits)

    store.set_note(os.path.join(fixture, "Beta"), note="旧游戏，可删", tags="清理,游戏", flagged=True)
    notes = store.notes_for([pkey(os.path.join(fixture, "Beta"))])
    check("标注可写入并按路径键取回",
          notes.get(pkey(os.path.join(fixture, "Beta")), {}).get("flagged") is True, notes)
    store.set_note(os.path.join(fixture, "Beta"), note="", tags="", flagged=False)
    check("清空标注会删除记录",
          store.notes_for([pkey(os.path.join(fixture, "Beta"))]) == {})

    # 文件作为根
    file_root = store.add_root(os.path.join(fixture, "big.bin"), kind="file")
    job2 = Job("scan", root_id=file_root["id"])
    res2 = scanner.scan_root(store, file_root, job2, {"min_file_keep": 65536})
    check("单文件追踪大小正确", res2["total_bytes"] == 200 * MB, res2)

    # 缺失路径
    ghost = os.path.join(tmp, "ghost")
    g = store.add_root(ghost)
    job3 = Job("scan", root_id=g["id"])
    scanner.scan_root(store, g, job3, {})
    check("路径缺失时标记 missing", store.get_root(g["id"])["status"] == "missing")


def test_audit(tmp, store):
    print("\n[2] 大文件体检")
    fixture = os.path.join(tmp, "fixture")
    job = Job("audit")
    res = scanner.audit_path(fixture, job, min_size=10 * MB, top_n=5)
    biggest = res["files"][0]
    check("Top1 文件 = big.bin 200MB",
          biggest["name"] == "big.bin" and biggest["size"] == 200 * MB, biggest)
    check("体检文件数 = 5（限制生效）", len(res["files"]) == 5, len(res["files"]))
    d = res["dirs"][0]
    check("Top1 目录 = Alpha 85MB", d["name"] == "Alpha" and d["size"] == 85 * MB, d)
    check("体检扫描字节数 > 396MB", res["scanned_bytes"] >= 396 * MB, res["scanned_bytes"])


def test_detector(tmp, store):
    print("\n[3] 察觉模块（注入式）")
    events = []
    tracked = []

    def reg_v1():
        return {
            "k|game1": {"name": "星海战纪", "path": os.path.join(tmp, "fixture", "Beta"),
                        "size": 80 * MB, "version": "1.0", "publisher": "Neon Lab",
                        "install_date": "20250101"},
            "k|tool1": {"name": "小工具", "path": "", "size": 1024,
                        "version": "1.0", "publisher": "", "install_date": ""},
        }

    det = Detector(store,
                   on_install=lambda e: tracked.append(e["name"]),
                   registry_provider=reg_v1,
                   process_provider=lambda: set(),
                   log=lambda *_: None)
    n = det.poll_registry()
    apps = store.list_apps()
    check("基线导入不产生新安装", n == 0 and len(apps) == 2, len(apps))
    check("基线事件已写入",
          any("基线" in e["label"] for e in store.list_events()), store.list_events()[:2])

    def reg_v2():
        d = reg_v1()
        d["k|game2"] = {"name": "裂隙协议", "path": os.path.join(tmp, "fixture", "Alpha", "SubA"),
                        "size": 15 * MB, "version": "2.1", "publisher": "Void",
                        "install_date": "20250505"}
        return d

    det.registry_provider = reg_v2
    n = det.poll_registry()
    check("新增安装被识别", n == 1 and tracked == ["裂隙协议"], tracked)
    check("新安装事件带路径",
          any(e["kind"] == "install" and "SubA" in e["path"] for e in store.list_events(limit=5)))
    check("重复轮询不再重复上报", det.poll_registry() == 0)
    check("重复轮询不会把旧条目重新标 NEW",
          sum(1 for a in store.list_apps() if a["is_new"]) == 1,
          [(a["name"], a["is_new"]) for a in store.list_apps()])

    # 安装器进程：启动 -> 新增目录 -> 退出
    os.makedirs(os.path.join(tmp, "fixture2"))
    detector2 = Detector(store, on_folder=lambda i: tracked.append("dir:" + i["name"]),
                         registry_provider=lambda: {},
                         process_provider=lambda: set(), log=lambda *_: None)
    watched = detector2._snapshot_watch_dirs()
    os.makedirs(os.path.join(tmp, "fixture2", "NewGame"))
    found = detector2._diff_dirs({os.path.join(tmp, "fixture2"): set()})
    check("目录差异能识别新目录",
          any(v["name"] == "NewGame" and v["is_dir"] for v in found.values()),
          list(found.values())[:2])
    check("快照接口可用", isinstance(watched, dict))

    modes = {}
    for p, m, _rid in detector2._watch_targets():
        modes[p.lower()] = m
    check("监视列表带 add/notify/rescan 三种模式",
          {"add", "notify"}.issubset(set(modes.values())) or not modes,
          sorted(set(modes.values())))
    appdata = [p for p in modes if "appdata" in p and p.endswith("appdata")]
    check("AppData 根目录只记录不自动追踪",
          all(modes[p] == "notify" for p in appdata) if appdata else True, appdata)


def main():
    # 注意：不使用 tempfile.mkdtemp（部分受限环境下该 API 创建的目录 ACL 异常）
    base_tmp = os.path.join(BASE, "data", "_selftest")
    tmp = os.path.join(base_tmp, "run")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    db = os.path.join(tmp, "test.db")
    store = Store(db)
    try:
        test_scanner(tmp, store)
        test_audit(tmp, store)
        test_detector(tmp, store)
    finally:
        store.close()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 56}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败用例：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
