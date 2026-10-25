using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

/// <summary>
/// SpaceSight 无控制台启动器（.NET Framework 4 / WinForms）。
/// 职责：定位一个**真正可用**的 Python 3 解释器 → 隐藏窗口启动 run.py → 立即退出。
/// 启动失败时给出可操作的提示，并把决策过程写入 data\launcher.log 便于排查。
///
/// 注意：必须验证候选解释器（有些机器上 PATH 里的 python.exe 只是
/// Microsoft Store 的 0 字节占位别名，执行后只会打开应用商店）。
/// </summary>
static class Launcher
{
    static string _logPath;

    [STAThread]
    static void Main()
    {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        _logPath = Path.Combine(baseDir, @"data\launcher.log");
        try { Directory.CreateDirectory(Path.Combine(baseDir, "data")); } catch { }
        Log("=== 启动 SpaceSight，程序目录 " + baseDir);

        string runPy = Path.Combine(baseDir, "run.py");
        if (!File.Exists(runPy))
        {
            Fail("未找到 run.py。\n请把 SpaceSight.exe 放在 SpaceSight 程序目录内（与 run.py 同级）。");
            return;
        }

        string python = FindPython(baseDir);
        if (python == null)
        {
            Fail("未检测到可用的 Python 3 运行环境。\n\n" +
                 "1) 从 https://www.python.org/downloads/windows/ 安装 Python 3.9+；\n" +
                 "2) 安装时勾选 “Add python.exe to PATH”；\n" +
                 "3) 重新双击 SpaceSight.exe。\n\n" +
                 "也可以双击 “start-spacesight.bat” 查看详细日志。");
            return;
        }
        Log("选用解释器: " + python);

        var psi = new ProcessStartInfo(python, "\"" + runPy + "\"")
        {
            WorkingDirectory = baseDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        // 无控制台运行时 stdout 句柄无效，必须让 Python 把输出写进日志文件
        psi.EnvironmentVariables["SPACESIGHT_LOG"] = "1";
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        psi.EnvironmentVariables["PYTHONUTF8"] = "1";

        try
        {
            Process p = Process.Start(psi);
            Log("已启动 python 进程: pid=" + (p == null ? "?" : p.Id.ToString()));
        }
        catch (Exception ex)
        {
            Log("启动失败: " + ex);
            Fail("启动失败：" + ex.Message + "\n\n请改用 “start-spacesight.bat” 以查看完整错误信息。");
        }
    }

    static void Log(string message)
    {
        try
        {
            File.AppendAllText(_logPath,
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + message + Environment.NewLine);
        }
        catch { }
    }

    static void Fail(string message)
    {
        Log("失败并提示用户: " + message.Replace("\n", " "));
        MessageBox.Show(message, "SpaceSight // 空间探针",
                        MessageBoxButtons.OK, MessageBoxIcon.Warning);
    }

    /// <summary>按优先级返回第一个可正常执行的 python.exe，找不到返回 null。</summary>
    static string FindPython(string baseDir)
    {
        // 0) 显式指定（环境变量 SPACESIGHT_PYTHON 指向任意 python.exe）
        string fromEnv = Environment.GetEnvironmentVariable("SPACESIGHT_PYTHON");
        if (!string.IsNullOrWhiteSpace(fromEnv) && File.Exists(fromEnv) && IsWorking(fromEnv))
        {
            Log("使用 SPACESIGHT_PYTHON: " + fromEnv);
            return fromEnv;
        }

        // 1) 便携运行时（放在程序目录下即可完全自包含）
        string local = Path.Combine(baseDir, @"runtime\python.exe");
        if (File.Exists(local) && IsWorking(local)) return local;
        Log("runtime 内置运行时不可用（正常，继续查找）");

        // 2) 系统 PATH
        int idx = 0;
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string raw in path.Split(';'))
        {
            idx++;
            string dir = raw.Trim().Trim('"');
            if (dir.Length == 0) continue;
            string candidate;
            try { candidate = Path.Combine(dir, "python.exe"); }
            catch { continue; }
            if (!File.Exists(candidate)) continue;
            bool ok = IsWorking(candidate);
            Log(string.Format("PATH[{0}] {1} -> {2}", idx, candidate, ok ? "可用" : "不可用"));
            if (ok) return candidate;
        }

        // 3) 常见安装位置（按版本号从高到低）
        string pyRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            @"Programs\Python");
        if (Directory.Exists(pyRoot))
        {
            string[] dirs = Directory.GetDirectories(pyRoot, "Python3*");
            Array.Sort(dirs);
            Array.Reverse(dirs);
            foreach (string d in dirs)
            {
                string candidate = Path.Combine(d, "python.exe");
                if (File.Exists(candidate) && IsWorking(candidate))
                {
                    Log("命中 LOCALAPPDATA: " + candidate);
                    return candidate;
                }
            }
        }

        // 4) 兜底：某些开发工具链自带的 Python 运行时（不装 Python 的机器也能跑）
        string bundled = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            @".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe");
        if (File.Exists(bundled))
        {
            bool ok = IsWorking(bundled);
            Log("兜底运行时 " + bundled + " -> " + (ok ? "可用" : "不可用"));
            if (ok) return bundled;
        }

        return null;
    }

    /// <summary>真实执行一次 `python -c "import sys"` 判断解释器是否可用。</summary>
    static bool IsWorking(string exe)
    {
        try
        {
            // Microsoft Store 的 App Execution Alias 是 0 字节的重解析点，直接排除
            FileInfo fi = new FileInfo(exe);
            if (!fi.Exists) return false;
            if (fi.Length < 20000) return false;
            if (exe.IndexOf(@"\WindowsApps\", StringComparison.OrdinalIgnoreCase) >= 0) return false;

            var psi = new ProcessStartInfo(exe, "-c \"import sys\"")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                WorkingDirectory = Path.GetDirectoryName(exe),
            };
            using (Process p = Process.Start(psi))
            {
                if (p == null) return false;
                if (!p.WaitForExit(12000))
                {
                    try { p.Kill(); } catch { }
                    Log("解释器探测超时: " + exe);
                    return false;
                }
                Log("解释器探测退出码 " + p.ExitCode + ": " + exe);
                return p.ExitCode == 0;
            }
        }
        catch (Exception ex)
        {
            Log("解释器探测异常: " + exe + " " + ex.Message);
            return false;
        }
    }
}

