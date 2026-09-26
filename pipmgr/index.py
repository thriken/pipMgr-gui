"""在线索引：多源支持、包名搜索、可用版本列表。

pip 官方已移除 `pip search`（PyPI XML-RPC 下线），这里直接读取各源的
PEP503 Simple 索引页面（<index>/simple/），全部镜像源都兼容。
首次搜索会下载索引并缓存 7 天。
"""

import hashlib
import json
import os
import re
import time
import urllib.request

from . import config
from .pkgs import normalize, version_key

USER_AGENT = "PipMgr/1.0 (+python-stdlib)"
HREF_RE = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
CACHE_TTL = 7 * 24 * 3600


def normalize_url(url):
    url = (url or "").strip()
    if url and not url.endswith("/"):
        url += "/"
    return url


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", "replace")


def _cache_file(url):
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return os.path.join(config.cache_dir(), "index_%s.txt" % digest)


def parse_simple_index(html):
    names = set()
    for href, text in HREF_RE.findall(html):
        text = TAG_RE.sub("", text).strip()
        segment = href.split("#")[0].rstrip("/").rsplit("/", 1)[-1]
        name = text or segment
        if not name or "/" in name or name.startswith("."):
            continue
        names.add(normalize(name))
    return sorted(names)


def load_index_names(url, force=False, timeout=60):
    """下载（或读取缓存）某个源的包名列表。"""
    url = normalize_url(url)
    cache = _cache_file(url)
    if not force and os.path.isfile(cache):
        if time.time() - os.path.getmtime(cache) < CACHE_TTL:
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    names = [line.strip() for line in fh if line.strip()]
                if names:
                    return names
            except OSError:
                pass
    html = _get(url, timeout=timeout)
    names = parse_simple_index(html)
    if names:
        try:
            with open(cache, "w", encoding="utf-8") as fh:
                fh.write("\n".join(names))
        except OSError:
            pass
    return names


def search_names(url, query, limit=300, force=False):
    """在指定源中模糊搜索包名，前缀命中优先。"""
    names = load_index_names(url, force=force)
    key = normalize(query)
    if not key:
        return names[:limit]
    starts = [n for n in names if n.startswith(key)]
    contains = [n for n in names if key in n and not n.startswith(key)]
    return (starts + contains)[:limit]


def _extract_versions(filenames):
    versions = set()
    for name in filenames:
        low = name.lower()
        version = None
        if low.endswith(".whl"):
            parts = name[:-4].split("-")
            if len(parts) >= 2:
                version = parts[1]
        else:
            for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip", ".egg"):
                if low.endswith(suffix):
                    core = name[: -len(suffix)]
                    if "-" in core:
                        version = core.rsplit("-", 1)[-1]
                    break
        if version and re.match(r"^\d", version):
            versions.add(version)
    return versions


def fetch_versions(name, url, timeout=20):
    """从指定源的 simple 页面提取某包的可用版本（新->旧）。"""
    page = normalize_url(url) + normalize(name) + "/"
    html = _get(page, timeout=timeout)
    filenames = []
    for href, _text in HREF_RE.findall(html):
        filename = href.split("#")[0].rstrip("/").rsplit("/", 1)[-1]
        if filename:
            filenames.append(filename)
    return sorted(_extract_versions(filenames), key=version_key, reverse=True)


def pypi_info(name, timeout=10):
    """从 PyPI 官方 JSON 接口取简介等元信息（仅描述用，失败返回空字典）。"""
    try:
        data = json.loads(_get("https://pypi.org/pypi/%s/json" % name, timeout=timeout))
    except Exception:
        return {}
    info = data.get("info") or {}
    return {
        "name": info.get("name") or name,
        "version": info.get("version") or "",
        "summary": (info.get("summary") or "").strip(),
        "home_page": info.get("home_page") or info.get("project_url") or "",
        "author": info.get("author") or "",
        "requires_python": info.get("requires_python") or "",
    }
