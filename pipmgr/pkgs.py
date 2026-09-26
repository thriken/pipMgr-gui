"""读取本机已安装的 pip 包：版本、磁盘占用、依赖关系。

基于标准库 importlib.metadata，无需调用 pip，速度快且不依赖网络。
"""

import os
import re
import sys

from importlib.metadata import distributions

_PEP503_RE = re.compile(r"[-_.]+")
_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_META_FIELDS = ("Name", "Version", "Summary")


def meta_fields(dist):
    """读取 METADATA 中需要的字段，返回普通字典（读取失败返回空字典）。"""
    meta = dist.metadata
    getter = getattr(meta, "get", None)
    if getter is None:
        return {}
    values = {}
    for field in _META_FIELDS:
        try:
            values[field] = getter(field) or ""
        except Exception:
            values[field] = ""
    return values


def normalize(name):
    """PEP 503 名称规范化：foo_bar / Foo.Bar -> foo-bar。"""
    return _PEP503_RE.sub("-", (name or "").strip()).lower()


def version_key(version):
    """可比较的版本键，用于排序（1.10 > 1.9）。"""
    parts = re.split(r"[._-]+", str(version or ""))
    out = []
    for part in parts:
        if part.isdigit():
            out.append((0, int(part), ""))
        else:
            head = re.match(r"\d+", part)
            num = int(head.group(0)) if head else 0
            out.append((1, num, part))
    return tuple(out)


def parse_requirement_name(req):
    """从 Requires-Dist 字符串中取出包名，忽略 extras/版本约束/环境标记。"""
    if not req:
        return None
    text = req.split("#", 1)[0].strip()
    text = text.split(";", 1)[0].strip()          # 去掉环境标记
    text = re.sub(r"\s*\(.*?\)\s*", " ", text)    # 去掉 (>=1.0,<2.0) 之类
    text = re.sub(r"\[.*?\]", " ", text).strip()  # 去掉 extras
    match = _NAME_RE.match(text)
    if not match:
        return None
    return normalize(match.group(1))


def _dist_size(dist):
    try:
        files = dist.files
    except Exception:
        files = None
    if not files:
        return 0
    total = 0
    for entry in files:
        try:
            path = dist.locate_file(entry)
        except Exception:
            try:
                path = os.path.join(str(dist._path.parent), str(entry))
            except Exception:
                continue
        try:
            if os.path.isfile(path):
                total += os.path.getsize(path)
        except OSError:
            pass
    return total


def scan_installed():
    """扫描已安装包（不含磁盘大小，大小由 iter_sizes 异步填充）。"""
    items = {}
    for dist in distributions():
        try:
            meta = meta_fields(dist)
            name = (meta.get("Name") or "").strip()
            if not name:
                continue
            key = normalize(name)
            version = ""
            try:
                version = dist.version or ""
            except Exception:
                version = meta.get("Version") or ""
            requires = set()
            for req in dist.requires or []:
                dep = parse_requirement_name(req)
                if dep:
                    requires.add(dep)
            location = str(getattr(dist, "_path", "") or "")
            item = {
                "name": name,
                "key": key,
                "version": version,
                "summary": (meta.get("Summary") or "").strip(),
                "size": None,                 # None = 未计算
                "requires": sorted(requires),
                "used_by": [],
                "location": location,
            }
            old = items.get(key)
            if old is None or version_key(version) > version_key(old["version"]):
                items[key] = item
        except Exception:
            continue

    result = sorted(items.values(), key=lambda x: x["key"])
    installed = set(items)
    by_key = items
    for item in result:
        item["requires"] = [r for r in item["requires"] if r in installed and r != item["key"]]
    for item in result:
        for dep in item["requires"]:
            by_key[dep]["used_by"].append(item["key"])
    for item in result:
        item["used_by"] = sorted(item["used_by"])
    return result


def iter_sizes():
    """逐个产出 (key, size)，用于后台计算磁盘占用。"""
    for dist in distributions():
        try:
            key = normalize(meta_fields(dist).get("Name") or "")
        except Exception:
            continue
        if not key:
            continue
        try:
            size = _dist_size(dist)
        except Exception:
            size = 0
        yield key, size


# ----------------------------------------------------------------- 无效残留修复
# pip 在卸载被中断时会把目录重命名为 '~' + 原名[1:]（去掉首字符），例如
# opencc-1.4.2.dist-info -> ~pencc-1.4.2.dist-info。这样 pip 会忽略它，
# 但它仍会被 importlib.metadata 读到，造成"列表里有、却卸不掉"的现象。
def _site_dirs():
    import site

    found = set()
    try:
        for directory in site.getsitepackages():
            found.add(os.path.abspath(directory))
    except Exception:
        pass
    try:
        user = site.getusersitepackages()
        if user:
            found.add(os.path.abspath(user))
    except Exception:
        pass
    for directory in sys.path:  # type: ignore[name-defined]
        if directory and os.path.isdir(directory):
            found.add(os.path.abspath(directory))
    return sorted(found)


def _read_meta_name_version(dist_dir):
    for filename in ("METADATA", "PKG-INFO"):
        path = os.path.join(dist_dir, filename)
        if not os.path.isfile(path):
            continue
        name = version = ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("Name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("Version:"):
                        version = line.split(":", 1)[1].strip()
                    if name and version:
                        break
        except Exception:
            continue
        if name:
            return name, version
    return "", ""


def find_invalid_leftovers():
    """扫描各 site-packages，找出 '~' 开头的残留 dist-info/egg-info/libs 目录。"""
    results = []
    for base in _site_dirs():
        if not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue

        all_names = []
        for entry in entries:
            low = entry.lower()
            if (low.endswith(".dist-info") or low.endswith(".egg-info")) and os.path.isdir(
                os.path.join(base, entry)
            ):
                nm, _ver = _read_meta_name_version(os.path.join(base, entry))
                if nm:
                    all_names.append(normalize(nm))

        for entry in entries:
            low = entry.lower()
            if not entry.startswith("~") or not os.path.isdir(os.path.join(base, entry)):
                continue
            if low.endswith(".dist-info"):
                nm, ver = _read_meta_name_version(os.path.join(base, entry))
                recovered = ("%s-%s.dist-info" % (normalize(nm), ver)) if nm and ver else entry[1:]
                kind = "dist-info"
            elif low.endswith(".egg-info"):
                nm, ver = _read_meta_name_version(os.path.join(base, entry))
                recovered = ("%s-%s.egg-info" % (normalize(nm), ver)) if nm and ver else entry[1:]
                kind = "egg-info"
            elif low.endswith(".libs"):
                recovered = None
                for nm in all_names:
                    if "~" + (nm + ".libs")[1:] == entry:
                        recovered = nm + ".libs"
                        break
                if recovered is None:
                    recovered = entry[1:]
                kind = "libs"
            else:
                continue
            results.append({
                "path": os.path.join(base, entry),
                "mangled": entry,
                "recovered": recovered,
                "kind": kind,
            })
    return results


def repair_leftover(item):
    """把残留目录重命名回正确名字，返回 (成功, 说明)。"""
    target = os.path.join(os.path.dirname(item["path"]), item["recovered"])
    if os.path.exists(target):
        return False, "已存在同名目录，跳过：%s" % target
    try:
        os.rename(item["path"], target)
    except PermissionError:
        return False, "无写入权限，请以管理员身份运行本程序后再试。"
    except OSError as exc:
        return False, str(exc)
    return True, target
