#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖发布脚本：把当前目录作为一个 commit 推到 GitHub。

不需要安装 git / gh，只用 Python 标准库 + GitHub REST API。

用法
----
    :: 方式 1：token 放环境变量（推荐，不会留在命令历史里）
    set GITHUB_TOKEN=ghp_xxxxxxxx
    python tools\\publish-to-github.py --repo spacesight

    :: 方式 2：运行后交互输入（不回显、不落盘）
    python tools\\publish-to-github.py --repo spacesight

    :: 只体检不上传
    python tools\\publish-to-github.py --dry-run

    :: 指定描述 / 私有仓库 / 只用已有仓库
    python tools\\publish-to-github.py --repo spacesight --desc "..." --private
    python tools\\publish-to-github.py --repo spacesight --no-create

Token 需要什么权限
------------------
Fine-grained token：Repository permissions → **Contents: Read and write**、
**Administration: Read and write**（创建仓库需要，`--no-create` 时可不给）。
Classic token：勾选 `repo`（创建仓库再勾 `workflow` 以包含 Actions 文件）。

安全设计
--------
* 只上传白名单内的文件（自动排除 data/、*.db、*.log、*.exe、*.lnk、__pycache__）
* 上传前做**隐私扫描**：文本里出现真实 `C:\\Users\\<名字>`、本机用户名、
  AppData 具体路径 → 直接失败（除非显式 --force）
* token 只存在内存里：不写文件、不打印、不进提交内容
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOPICS = ["disk-space", "disk-usage", "windows", "python", "treemap",
          "storage", "visualization", "disk-cleaner", "cyberpunk"]

# ── 只发布这些（白名单），避免误传本机数据 ────────────────────────────────
INCLUDE_FILES = [
    "README.md", "LICENSE", "CHANGELOG.md", ".gitignore", ".gitattributes",
    "run.py", "start-spacesight.bat", "publish-to-github.bat",
]
INCLUDE_DIRS = ["app", "web", "docs", "tools", ".github"]

SKIP_DIR_NAMES = {"__pycache__", ".git", "data", ".venv", "venv", "env", "node_modules"}
SKIP_EXT = {".pyc", ".pyo", ".db", ".db-shm", ".db-wal", ".log", ".lnk", ".exe",
            ".zip", ".7z", ".rar", ".tmp"}
SKIP_NAMES = {"server.port", "desktop.ini", "Thumbs.db", ".DS_Store"}

# ── 隐私扫描 ─────────────────────────────────────────────────────────────
SCAN_EXT = {".md", ".py", ".js", ".mjs", ".css", ".html", ".cs", ".bat", ".yml",
            ".yaml", ".txt", ".json", ".gitignore", ".gitattributes"}
PRIVACY_PATTERNS = [
    (re.compile(r"[A-Za-z]:\\Users\\(?!%|<|…|\.\.\.|用户名|your)[^\s\\\"'`<>|]+"),
     "出现具体用户目录（C:\\Users\\<名字>）"),
    (re.compile(r"AppData\\(?:Local|Roaming|LocalLow)\\"), "出现 AppData 具体路径"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"), "出现邮箱地址"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "出现疑似 GitHub token"),
]


class ApiError(RuntimeError):
    pass


def api(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    url = API + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "spacesight-publisher")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return exc.code, parsed


def collect_files() -> list[str]:
    files: list[str] = []
    for name in INCLUDE_FILES:
        if os.path.isfile(os.path.join(ROOT, name)):
            files.append(name)
    for top in INCLUDE_DIRS:
        base = os.path.join(ROOT, top)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
            for fn in sorted(filenames):
                if fn in SKIP_NAMES or os.path.splitext(fn)[1].lower() in SKIP_EXT:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                files.append(rel)
    return sorted(set(files))


def privacy_scan(files: list[str]) -> list[str]:
    problems: list[str] = []
    username = (os.environ.get("USERNAME") or os.environ.get("USER") or "").strip()
    for rel in files:
        if os.path.splitext(rel)[1].lower() not in SCAN_EXT and os.path.basename(rel) not in SCAN_EXT:
            continue
        with open(os.path.join(ROOT, rel), "r", encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, 1):
                if username and len(username) >= 3 and username.lower() in line.lower():
                    problems.append(f"{rel}:{lineno} 含本机用户名 “{username}”")
                for pattern, desc in PRIVACY_PATTERNS:
                    if pattern.search(line):
                        snippet = line.strip()[:90]
                        problems.append(f"{rel}:{lineno} {desc} → {snippet}")
    return problems


def make_token() -> str:
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if token:
        print("  使用环境变量中的 GITHUB_TOKEN")
        return token
    print("\n  需要一个 GitHub Personal Access Token（只存于内存，不落盘）：")
    print("    https://github.com/settings/tokens")
    print("    fine-grained: Contents=Read/Write + Administration=Read/Write")
    print("    classic:      勾选 repo（以及 workflow）")
    token = getpass.getpass("  Token（输入时不显示）: ").strip()
    if not token:
        raise SystemExit("  没有 token，已取消。")
    return token


def main() -> int:
    argv = sys.argv[1:]
    repo = "spacesight"
    desc = ("SpaceSight · 装了什么、装在哪、占多大：自动察觉安装落点的空间测绘工具"
            "（目录树 + 矩形图，纯 Python 标准库，本地只读）")
    private = False
    dry_run = False
    force = False
    no_create = False
    branch = "main"
    message = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--repo" and i + 1 < len(argv):
            repo = argv[i + 1]; i += 2
        elif a == "--desc" and i + 1 < len(argv):
            desc = argv[i + 1]; i += 2
        elif a == "--branch" and i + 1 < len(argv):
            branch = argv[i + 1]; i += 2
        elif a == "--message" and i + 1 < len(argv):
            message = argv[i + 1]; i += 2
        elif a == "--private":
            private = True; i += 1
        elif a in ("--dry-run", "-n"):
            dry_run = True; i += 1
        elif a == "--force":
            force = True; i += 1
        elif a == "--no-create":
            no_create = True; i += 1
        elif a in ("--help", "-h"):
            print(__doc__)
            return 0
        else:
            i += 1

    files = collect_files()
    total = sum(os.path.getsize(os.path.join(ROOT, f)) for f in files)
    print(f"\n待发布文件：{len(files)} 个 / {total/1024:.0f} KB")
    for f in files:
        print(f"  · {f}")

    print("\n隐私扫描 …")
    problems = privacy_scan(files)
    if problems:
        print(f"  ✗ 发现 {len(problems)} 处可能的隐私泄露：")
        for p in problems[:40]:
            print("    - " + p)
        if not force:
            print("\n已中止。请清理上述内容，或确认无害后加 --force。")
            return 1
        print("  已按 --force 继续。")
    else:
        print("  ✓ 未发现本机用户名 / 真实用户目录 / 邮箱 / token")
    print("  ⚠ 截图（PNG）无法文本扫描，请自行确认 docs/img/*.png 里没有你的真实路径")

    if dry_run:
        print("\n--dry-run：只体检，不上传。")
        return 0

    token = make_token()
    status, me = api("GET", "/user", token)
    if status != 200:
        print(f"  ✗ token 校验失败（HTTP {status}）: {me.get('message')}")
        return 1
    owner = me["login"]
    print(f"  登录身份：{owner}")

    status, info = api("GET", f"/repos/{owner}/{repo}", token)
    if status == 404:
        if no_create:
            print(f"  ✗ 仓库 {owner}/{repo} 不存在，且指定了 --no-create")
            return 1
        print(f"  创建仓库 {owner}/{repo} …")
        status, info = api("POST", "/user/repos", token, {
            "name": repo, "description": desc[:350], "private": private,
            "has_issues": True, "has_wiki": False, "has_projects": False,
            # auto_init=True 让 GitHub 建一个初始提交，规避空仓库的 409
            "auto_init": True,
        })
        if status not in (200, 201):
            print(f"  ✗ 创建失败（HTTP {status}）: {info.get('message')}")
            return 1
        print("  ✓ 已创建")
        status, info = api("GET", f"/repos/{owner}/{repo}", token)
    elif status == 200:
        print(f"  仓库已存在：{info['html_url']}")
    else:
        print(f"  ✗ 查询仓库失败（HTTP {status}）: {info.get('message')}")
        return 1

    # 用仓库真实的默认分支（可能是 main，也可能是 master）
    branch = info.get("default_branch") or branch
    print(f"  默认分支：{branch}")

    # ── 关键：GitHub 不允许在**空仓库**上调用 git/blobs|trees|commits（409）──
    #    先查有没有提交；没有就用 Contents API 建出第一个提交作为父节点。
    parent = None
    status, commits = api("GET", f"/repos/{owner}/{repo}/commits?per_page=1&sha={branch}", token)
    if status == 200 and isinstance(commits, list) and commits:
        parent = commits[0]["sha"]
        print(f"  已有提交 {parent[:8]}，将以其为父提交")
    else:
        print("  仓库是空的 → 先用 Contents API 建立初始提交（绕开 409 Git Repository is empty）")
        first_rel = "README.md" if "README.md" in files else files[0]
        with open(os.path.join(ROOT, first_rel), "rb") as fh:
            first_b64 = base64.b64encode(fh.read()).decode("ascii")
        status, boot = api("PUT", f"/repos/{owner}/{repo}/contents/{first_rel}", token, {
            "message": "chore: bootstrap repository",
            "content": first_b64,
        })
        if status not in (200, 201):
            print(f"  ✗ 引导提交失败（HTTP {status}）: {boot.get('message')}")
            print("    提示：也可以先在网页上手动创建一个文件（如 README.md），再重跑本脚本。")
            return 1
        parent = boot["commit"]["sha"]
        print(f"  ✓ 引导提交 {parent[:8]}（{first_rel}）")

    print(f"\n上传 {len(files)} 个文件 …")
    tree = []
    for idx, rel in enumerate(files, 1):
        with open(os.path.join(ROOT, rel), "rb") as fh:
            content = base64.b64encode(fh.read()).decode("ascii")
        status, blob = api("POST", f"/repos/{owner}/{repo}/git/blobs", token,
                           {"content": content, "encoding": "base64"})
        if status not in (200, 201):
            print(f"  ✗ 上传 {rel} 失败（HTTP {status}）: {blob.get('message')}")
            return 1
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if idx % 5 == 0 or idx == len(files):
            print(f"  · {idx}/{len(files)}")

    tree_obj = create_tree(owner, repo, token, tree, parent)
    if not tree_obj:
        print("  切换到逐文件上传（Contents API）—— 会生成较多提交，但一定能发上去 …")
        return publish_by_contents(owner, repo, token, files, branch, message, desc)

    status, commit = api("POST", f"/repos/{owner}/{repo}/git/commits", token, {
        "message": message or "chore: publish SpaceSight snapshot",
        "tree": tree_obj["sha"],
        "parents": [parent] if parent else [],
    })
    if status not in (200, 201):
        print(f"  ✗ 创建 commit 失败（HTTP {status}）: {commit.get('message')}")
        return 1
    print(f"  ✓ commit {commit['sha'][:8]}")

    if parent:
        status, res = api("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}", token,
                          {"sha": commit["sha"], "force": True})
        if status not in (200, 201):
            # 分支名与默认分支不一致等少见情况：直接创建分支
            status, res = api("POST", f"/repos/{owner}/{repo}/git/refs", token,
                              {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    else:
        status, res = api("POST", f"/repos/{owner}/{repo}/git/refs", token,
                          {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    if status not in (200, 201):
        print(f"  ✗ 更新分支失败（HTTP {status}）: {res.get('message')}")
        return 1

    api("PATCH", f"/repos/{owner}/{repo}", token, {
        "description": desc[:350],
        "homepage": "",
        "topics": TOPICS,
    })
    print(f"\n🎉 发布完成：https://github.com/{owner}/{repo}")
    print("   建议再补：About 里的描述已填好，如需 Release 可上传自己编译的 SpaceSight.exe。")
    return 0


def create_tree(owner: str, repo: str, token: str, tree: list, parent: str | None,
                attempts: int = 4) -> dict | None:
    """创建 tree（带 base_tree 与重试）。

    新版仓库的 Git 后端有时还没就绪，`POST /git/trees` 会返回 404 Not Found
    （对象写入已经成功，但树写入还看不到 ref）。这里带上 base_tree（等价于
    git commit 时的增量树），失败后指数退避重试；全部失败再返回 None 交给上层降级。
    """
    payload: dict = {"tree": tree}
    if parent:
        status, cinfo = api("GET", f"/repos/{owner}/{repo}/git/commits/{parent}", token)
        if status == 200:
            base = (cinfo.get("tree") or {}).get("sha")
            if base:
                payload["base_tree"] = base
                print(f"  base_tree {base[:8]}（基于父提交增量）")
    for attempt in range(attempts):
        status, res = api("POST", f"/repos/{owner}/{repo}/git/trees", token, payload)
        if status in (200, 201):
            return res
        wait = 1.5 * (attempt + 1)
        print(f"  ⚠ 创建 tree 失败（HTTP {status}）: {res.get('message')}"
              f" → {wait:.1f}s 后重试（{attempt + 1}/{attempts}）")
        if status == 422:
            break          # 参数问题，重试无意义
        time.sleep(wait)
    print(f"  请求详情：POST https://api.github.com/repos/{owner}/{repo}/git/trees"
          f"，{len(tree)} 个条目，base_tree={'有' if 'base_tree' in payload else '无'}")
    return None


def publish_by_contents(owner: str, repo: str, token: str, files: list, branch: str,
                        message: str, desc: str) -> int:
    """降级方案：用 Contents API 逐个上传（每个文件一个提交）。

    Git Data API 在某些新仓库/权限组合下会 404，这条路更"笨"但更稳。
    """
    msg = message or "chore: publish SpaceSight snapshot"
    failed = []
    for idx, rel in enumerate(files, 1):
        with open(os.path.join(ROOT, rel), "rb") as fh:
            content = base64.b64encode(fh.read()).decode("ascii")
        quoted = urllib.parse.quote(rel)
        payload = {"message": f"{msg} — {rel}", "content": content, "branch": branch}
        # 已存在的文件必须带上它的 blob sha，否则 PUT 会 422
        status, cur = api("GET",
                          f"/repos/{owner}/{repo}/contents/{quoted}?ref={urllib.parse.quote(branch)}",
                          token)
        if status == 200 and isinstance(cur, dict) and cur.get("sha"):
            payload["sha"] = cur["sha"]
        status, res = 0, {}
        for attempt in range(3):
            status, res = api("PUT", f"/repos/{owner}/{repo}/contents/{quoted}", token, payload)
            if status in (200, 201):
                break
            if status in (401, 403, 404, 422):
                break      # 重试无意义
            time.sleep(1.0 * (attempt + 1))
        if status not in (200, 201):
            failed.append(f"{rel}（HTTP {status}: {res.get('message')}）")
        if idx % 5 == 0 or idx == len(files):
            print(f"  · {idx}/{len(files)}")
    if failed:
        print(f"  ✗ 有 {len(failed)} 个文件仍失败：")
        for f in failed[:10]:
            print("    - " + f)
        return 1
    api("PATCH", f"/repos/{owner}/{repo}", token, {
        "description": desc[:350], "homepage": "", "topics": TOPICS,
    })
    print(f"\n🎉 发布完成（逐文件模式）：https://github.com/{owner}/{repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
