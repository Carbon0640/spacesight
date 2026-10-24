# SpaceSight // 空间探针

> **装了什么、装在哪、占多大 —— 从此不用等扫盘。**
>
> A local, read-only disk-space cartographer for Windows: it *notices* what you install,
> remembers where it landed and how big it is, then draws it as a neon tree & treemap you can annotate.
> **Python 3 standard library only · nothing to pip install · no telemetry · UI runs in your browser.**

![目录树视图](docs/img/01-tree.png)

---

## 它解决什么问题

装游戏、下镜像、解压素材……用久了磁盘越来越满，但**你不知道是哪个东西吃掉了 200GB**，
只能等一款软件把整块盘扫一遍。

SpaceSight 换个思路：**在安装/解压发生的那一刻就记账**。

- 装游戏时 → 察觉"哪里多了新目录"，自动量出占用
- 解压完成后 → 发现哪个文件夹刚变大，自动重扫
- 手动下载的大文件 → 用「大文件体检」几秒钟挖出来

记录下来的东西用**目录树**和**矩形图**呈现：大到 100GB 的游戏、小到 200MB 的补丁包，
都用面积和颜色直观区分，还能给每个目录写备注 —— "这游戏通关了，可以删"。

**它只读不写**：只读取文件大小与目录结构，从不删除、移动或修改你的任何文件。

## 核心能力

| 能力 | 说明 |
|---|---|
| **自动察觉安装** | 三路信号融合：注册表安装台账轮询 · 安装/解压进程监控 · 常用安装位置目录差异侦测。发现新软件即自动记账并测量占用 |
| **手动补录** | 内置路径浏览器（盘符 → 逐级进入 → 粘贴路径），支持整个目录或单个大文件 |
| **目录树** | 真正的文件系统层级，每个节点显示名称与精确大小；按阈值动态裁剪，小于阈值的项目对用户透明（并提示隐藏了多少项） |
| **尺寸语义化** | 越大的节点行更高、字更大、图标更亮；按 64MB / 256MB / 1GB / 4GB / 16GB / 64GB 六档热度着色 |
| **矩形图** | Treemap 布局，面积严格正比于占用；二级下钻、双击放大、面包屑回退 |
| **标注备注** | 任意节点可写备注、加标签、标记「待清理」，自动保存到本地数据库 |
| **大文件体检** | 一次性遍历指定目录，列出最大的文件与文件夹，一键加入追踪 |
| **安装台账** | 读取注册表 Uninstall 键，列出"我装了什么、装在哪、多大"，逐条可追踪 |
| **纯本地** | 只监听 `127.0.0.1`，零联网、零遥测；数据在单个 SQLite 文件里，删目录即卸载 |

## 界面

| 矩形图（面积 ∝ 占用） | 安装台账 |
|---|---|
| ![矩形图](docs/img/02-treemap.png) | ![安装台账](docs/img/03-apps.png) |

## 30 秒上手

```text
1. 装 Python 3.9+（installer 里务必勾选 "Add python.exe to PATH"）
2. 下载本仓库 → 解压到任意目录
3. 双击 start-spacesight.bat   → 浏览器自动打开 http://127.0.0.1:8765/
4. 点右上角「＋ 添加追踪」→ 选一个目录（如 D:\Games）→ 自动扫描建索引
```

之后每次安装游戏 / 解压大文件，它都会自己发现并记账。
关服务：点界面右上角 **⏻**，或关掉那个控制台窗口。

> 想要"双击一个 exe、没有黑窗口"的体验？运行 `tools\build-launcher.bat` 会在本地编译出一个
> 6KB 的 `SpaceSight.exe`（用 Windows 自带的 .NET Framework 编译器，不需要装任何东西）。

完整的部署说明（含便携 Python、无网络环境、排错清单）：**[docs/部署与运行.md](docs/部署与运行.md)**

## 它是怎么"察觉"的

```text
① 注册表台账   每 5s 读 HKLM/HKCU 的 Uninstall 键 → 新条目 = 刚装了什么（名称/版本/安装位置/估算大小）
② 进程监控     每 3s 用 Toolhelp 快照匹配 setup/install/msiexec/7z/winrar… → 安装器启动时给常用安装位置拍快照，
               退出后 diff 出新增目录（覆盖绿色版 / 免安装 / 解压型程序）
③ 目录变更     每 20s 对常用安装位置、磁盘根、以及你追踪的每个目录做一级子项比对
               → 新目录自动追踪；已追踪目录内出现新内容则自动重扫
```

三路信号都是"提示"，永不阻塞界面；注册表提供器 / 进程提供器可注入，便于离线测试。

## 隐私与安全

- **只读**：唯一的写操作在你自己的 `data/` 目录（SQLite + 日志）
- **不联网**：无 CDN、无字体服务、无遥测；所有前端资源都是本地文件
- **只监听本机**：`127.0.0.1`，局域网内其他设备访问不到
- **无删除功能**（设计如此）：清理动作仍需你在资源管理器里完成
- 仓库里**不含**任何作者本机的数据库、截图里的真实路径或目录结构

## 项目结构

```
SpaceSight/
├─ run.py                    启动入口（日志重定向 / --data-dir / --port）
├─ start-spacesight.bat      双击启动（控制台版）
├─ app/                      后端：纯标准库
│  ├─ server.py              HTTP 服务 + API 路由 + 扫描调度
│  ├─ store.py               SQLite 持久化
│  ├─ scanner.py             多线程目录扫描 / 体检
│  ├─ detectors.py           三路安装察觉
│  └─ jobs.py                后台任务与进度
├─ web/                      前端：原生 HTML/CSS/JS，无框架
├─ docs/                     用户说明 / 技术文档 / 部署指南 / 截图
└─ tools/                    自检、CDP 测试、截图、启动器编译
```

## 开发

```bat
:: 离线自检：34 项断言（扫描聚合 / 阈值 / 标注 / 察觉 / 体检）
python tools\selftest.py

:: 浏览器行为测试（CDP，11 项断言）
:: 先让浏览器带 --remote-debugging-port=9333 打开界面，然后：
node tools\cdp-test.mjs "<测试目录>" [截图输出.png]

:: 生成演示库（用于截图，不读取真实磁盘/注册表）
python tools\make_demo.py data\_demo
```

CI 在 Windows + Python 3.9/3.12 下跑语法检查、自检与"启动→探活→停机"冒烟测试。

## 文档

- **[部署与运行](docs/部署与运行.md)** — 给别人看的安装 / 启动 / 排错手册
- **[用户使用说明](docs/用户使用说明.md)** — 每个按钮怎么用、常见问题怎么解
- **[技术文档](docs/技术文档.md)** — 架构、扫描算法、数据模型、API、性能与限制
- **[CHANGELOG](CHANGELOG.md)** — 版本变更记录

## English TL;DR

SpaceSight is a **read-only, local** disk-space visualizer for Windows that *learns where things get installed*
instead of rescanning your whole drive: it watches registry uninstall keys, installer/extractor processes,
and directory changes, stores sizes in SQLite, then renders a neon hierarchical tree plus a treemap
(area ∝ bytes, color by size tier) with per-node notes.

```text
Requirements: Windows 10/11 + Python 3.9+ (stdlib only, nothing to pip install)
Run:          double-click start-spacesight.bat  →  http://127.0.0.1:8765/
```

No telemetry, no network calls, no file deletion. MIT licensed.

## 已知限制

- 安装察觉是**启发式**的：单文件绿色程序、写进已有深层目录的安装器可能漏检 → 用「＋ 添加追踪」补录
- 注册表 `EstimatedSize` 是安装时的估算值，与真实占用不同（以扫描结果为准）
- `C:\Windows\WinSxS` 这类硬链接目录会被重复计数，数字偏大属正常
- 仅支持 Windows（注册表、进程枚举、盘符枚举都是 Win32 API）
- 目前没有删除功能（有意为之）

## 路线图

| 优先级 | 事项 |
|---|---|
| P0 | USN Journal / MFT 全盘秒级首扫（需管理员权限） |
| P0 | Steam / Epic / GOG 库解析（直接读 `libraryfolders.vdf`、`*.acf`） |
| P1 | 目录增长趋势与快照对比（"这个月谁长了 20GB"） |
| P1 | 右键菜单集成 / 导出 CSV 报告 |
| P2 | 虚拟滚动、路径压缩存储（节点体积降 60%） |

## License

MIT — 见 [LICENSE](LICENSE)。
