"""SpaceSight 启动入口。

用法：
    python run.py                # 启动并自动打开浏览器
    python run.py --no-browser   # 只启动服务
    pythonw run.py               # 无控制台窗口启动

环境变量：
    SPACESIGHT_LOG=1   把标准输出/错误重定向到 data/server.log（供无控制台启动器使用）
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

# 必须在任何 print 之前接管输出：无控制台启动时 stdout 句柄无效，print 会抛异常
if os.environ.get("SPACESIGHT_LOG"):
    try:
        _log_dir = os.path.join(BASE, "data")
        os.makedirs(_log_dir, exist_ok=True)
        _log = open(os.path.join(_log_dir, "server.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = _log
        print("\n" + "=" * 70)
    except Exception:
        pass

if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
