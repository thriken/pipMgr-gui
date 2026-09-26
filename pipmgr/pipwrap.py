"""pip 命令行封装：安装 / 升级 / 卸载 / 检查更新。

统一使用 `sys.executable -m pip`，避免多 Python 环境下调用错 pip。
"""

import json
import os
import subprocess
import sys
from typing import Any, Dict
from urllib.parse import urlparse

from .pkgs import normalize

IS_WIN = os.name == "nt"


def _popen(cmd):
    """启动 pip 进程（Windows 下隐藏控制台窗口）。"""
    common: Dict[str, Any] = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_env(),
    )
    if IS_WIN:
        return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW, **common)
    return subprocess.Popen(cmd, **common)


def _env():
    env = os.environ.copy()
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def pip_cmd(args):
    return [sys.executable, "-m", "pip"] + [str(a) for a in args]


def host_of(url):
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def index_args(index_url):
    """生成 -i / --trusted-host 参数。"""
    if not index_url:
        return []
    args = ["-i", index_url]
    host = host_of(index_url)
    if host and index_url.startswith("http://"):
        args += ["--trusted-host", host]
    return args


def run(args, on_line=None, timeout=None):
    """执行 pip 命令，实时回吐输出行，返回 (returncode, output)。"""
    proc = _popen(pip_cmd(args))
    lines = []
    stream = proc.stdout
    try:
        if stream is not None:
            for line in stream:
                line = line.rstrip("\n")
                lines.append(line)
                if on_line:
                    on_line(line)
    finally:
        if stream is not None:
            stream.close()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = -1
    return code, "\n".join(lines)


def _extract_json(text):
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def pip_version():
    code, out = run(["--version"])
    return out.strip() if code == 0 else ""


def list_outdated(index_url=None):
    """返回 {规范化包名: 最新可用版本}。"""
    args = ["list", "--outdated", "--format=json"]
    code, out = run(args + index_args(index_url))
    if code != 0 and index_url:
        code, out = run(args)
    data = _extract_json(out)
    result = {}
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            key = normalize(row.get("name", ""))
            latest = row.get("latest_version") or row.get("latest") or ""
            if key:
                result[key] = latest
    return result


def install(specs, upgrade=False, user=False, index_url=None, extra=None, on_line=None):
    args = ["install"]
    if upgrade:
        args.append("--upgrade")
    if user:
        args.append("--user")
    args += index_args(index_url)
    if extra:
        args += [str(a) for a in extra]
    args += [str(s) for s in specs]
    return run(args, on_line=on_line)


def uninstall(names, on_line=None):
    return run(["uninstall", "-y"] + [str(n) for n in names], on_line=on_line)
