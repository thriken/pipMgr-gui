"""全局配置：应用目录、pip 源列表、管理员权限检测与提权。"""

import ctypes
import json
import os
import sys
from typing import Any, Dict, List

APP_NAME = "PipMgr"
APP_VERSION = "1.0.0"

# 内置 pip 源（name 用于界面展示，url 为 PEP503 simple 索引地址）
BUILTIN_SOURCES: List[Dict[str, Any]] = [
    {"name": "清华大学", "url": "https://pypi.tuna.tsinghua.edu.cn/simple"},
    {"name": "阿里云", "url": "https://mirrors.aliyun.com/pypi/simple/"},
    {"name": "中国科技大学", "url": "https://pypi.mirrors.ustc.edu.cn/simple/"},
    {"name": "豆瓣", "url": "https://pypi.doubanio.com/simple/"},
    {"name": "PyPI 官方", "url": "https://pypi.org/simple"},
]


# ---------------------------------------------------------------- 目录与配置
def app_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = os.path.expanduser("~")
    return path


def cache_dir():
    path = os.path.join(app_dir(), "cache")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def config_path():
    return os.path.join(app_dir(), "config.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_value(key, default=None):
    return load_config().get(key, default)


def set_value(key, value):
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


# ---------------------------------------------------------------- pip 源
def load_sources() -> List[Dict[str, Any]]:
    """内置源 + 用户自定义源。"""
    sources: List[Dict[str, Any]] = [dict(s) for s in BUILTIN_SOURCES]
    for item in load_config().get("custom_sources") or []:
        if isinstance(item, dict) and item.get("url"):
            sources.append({
                "name": item.get("name") or item["url"],
                "url": item["url"],
                "custom": True,
            })
    return sources


def save_custom_sources(sources):
    cfg = load_config()
    cfg["custom_sources"] = [
        {"name": s["name"], "url": s["url"]}
        for s in sources
        if s.get("custom") and s.get("url")
    ]
    save_config(cfg)


# ---------------------------------------------------------------- 管理员权限
def is_admin():
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        return False


def _relaunch_params():
    """构造提权重启时的命令行参数，兼容 `python main.py` 与 `python -m pipmgr`。"""
    main_mod = sys.modules.get("__main__")
    path = getattr(main_mod, "__file__", "") or sys.argv[0]
    path = os.path.abspath(path)
    if os.path.basename(path).lower() == "__main__.py" and \
            os.path.basename(os.path.dirname(path)).lower() == "pipmgr":
        return "-m pipmgr", os.path.dirname(os.path.dirname(path))
    return '"%s"' % path, os.path.dirname(path)


def relaunch_as_admin():
    """以管理员身份重启本程序（UAC）。成功返回 True。"""
    if os.name != "nt":
        return False
    params, workdir = _relaunch_params()
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, workdir, 1
        )
        return rc > 32
    except Exception:
        return False
