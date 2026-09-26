"""Tkinter 图形界面。

布局：顶部工具条（解释器 / 安装源 / 权限）+ 中部标签页（已安装 / 在线安装）
+ 底部日志与进度条。所有耗时操作都在后台线程执行，通过队列回主线程更新。
"""

import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import config, index as net, pipwrap, pkgs

INSTALLED_COLS = (
    ("name", "包名", 180, "w"),
    ("version", "当前版本", 100, "center"),
    ("latest", "最新版本", 100, "center"),
    ("size", "占用大小", 90, "e"),
    ("requires", "依赖数", 70, "center"),
    ("used_by", "被依赖数", 80, "center"),
    ("summary", "说明", 340, "w"),
)

RESULT_COLS = (
    ("name", "包名", 240, "w"),
    ("local", "本地版本", 100, "center"),
    ("state", "状态", 100, "center"),
)

CORE_PACKAGES = {"pip", "setuptools", "wheel", "distribute"}


def human_size(value):
    if value is None:
        return "计算中"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return "%d B" % int(size)
    return "%.1f %s" % (size, units[idx])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("%s —— PIP 包管理器  v%s" % (config.APP_NAME, config.APP_VERSION))
        self.geometry("1020x680")
        self.minsize(900, 600)

        self._queue = queue.Queue()
        self._busy = 0
        self.packages = []
        self.by_key = {}
        self.outdated = {}
        self.sources = config.load_sources()
        self._key_iid = {}
        self._iid_by_key = {}
        self._detail_name = ""
        self._detail_versions = []
        self._action_buttons = []

        self._build_style()
        self._build_header()
        self._build_notebook()
        self._build_log()

        self.after(60, self._drain_queue)
        self.after(150, self._on_startup)

    # ------------------------------------------------------------ 基础工具
    def _build_style(self):
        style = ttk.Style(self)
        for name in ("clam", "vista", "xpnative", "default"):
            if name in style.theme_names():
                try:
                    style.theme_use(name)
                    break
                except tk.TclError:
                    continue
        style.configure("Treeview", rowheight=24)

    def post(self, func):
        """后台线程 -> 主线程执行。"""
        self._queue.put(func)

    def _drain_queue(self):
        try:
            while True:
                func = self._queue.get_nowait()
                try:
                    func()
                except Exception as exc:  # 防止单个回调炸掉轮询
                    self._log("[内部错误] %s" % exc)
        except queue.Empty:
            pass
        self.after(60, self._drain_queue)

    def log_async(self, line):
        self.post(lambda: self._log(line))

    def _log(self, text):
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def run_async(self, func, on_done=None, status=""):
        self._busy += 1
        self._set_busy(True, status)

        def worker():
            try:
                result = func()
                error = None
            except Exception as exc:
                result, error = None, exc
            self.post(lambda: self._async_finished(result, error, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _async_finished(self, result, error, on_done):
        self._busy -= 1
        if self._busy <= 0:
            self._busy = 0
            self._set_busy(False, "就绪")
        if error is not None:
            self._log("[错误] %s" % error)
            messagebox.showerror("出错了", str(error))
            return
        if on_done:
            on_done(result)

    def _set_busy(self, busy, status=""):
        for btn in self._action_buttons:
            btn.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        if status:
            self.status_var.set(status)

    def _button(self, parent, text: str, command, width: int = 0):
        btn = ttk.Button(parent, text=text, command=command, width=width)
        self._action_buttons.append(btn)
        return btn

    # ------------------------------------------------------------ 顶部工具条
    def _build_header(self):
        head = ttk.Frame(self, padding=(8, 6))
        head.pack(fill="x")

        ttk.Label(head, text="解释器：").pack(side="left")
        ttk.Label(head, text=sys.executable, foreground="#555").pack(side="left")

        ttk.Separator(head, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Label(head, text="安装源：").pack(side="left")
        self.source_var = tk.StringVar()
        self.source_box = ttk.Combobox(
            head, textvariable=self.source_var, width=26, state="readonly",
            values=[s["name"] for s in self.sources],
        )
        saved = config.get_value("source")
        idx = 0
        for i, src in enumerate(self.sources):
            if src["url"] == saved:
                idx = i
                break
        self.source_box.current(idx)
        self.source_box.pack(side="left")
        self.source_box.bind("<<ComboboxSelected>>", self._on_source_changed)

        ttk.Button(head, text="管理源", width=8, command=self._manage_sources).pack(side="left", padx=4)

        self.admin_var = tk.StringVar(value="")
        self.admin_label = ttk.Label(head, textvariable=self.admin_var)
        self.admin_label.pack(side="left", padx=8)
        ttk.Button(head, text="权限", width=6, command=self._show_privilege).pack(side="left")

    def _current_source(self):
        idx = self.source_box.current()
        if 0 <= idx < len(self.sources):
            return self.sources[idx]
        return self.sources[0]

    def _on_source_changed(self, _event=None):
        config.set_value("source", self._current_source()["url"])

    def _update_admin_label(self):
        if config.is_admin():
            self.admin_var.set("● 管理员权限")
            self.admin_label.configure(foreground="#1a7f37")
        else:
            self.admin_var.set("● 普通权限（系统目录安装可能失败）")
            self.admin_label.configure(foreground="#b35900")

    def _show_privilege(self):
        if config.is_admin():
            messagebox.showinfo("权限", "当前已以管理员身份运行。")
            return
        if messagebox.askyesno("权限", "是否以管理员身份重启程序？\n（不重启仍可操作，安装到用户目录请勾选 --user）"):
            if config.relaunch_as_admin():
                self.destroy()
            else:
                messagebox.showwarning("权限", "提权失败或被取消。")

    def _on_startup(self):
        self._update_admin_label()
        self._log("%s v%s 启动" % (config.APP_NAME, config.APP_VERSION))
        self._log("解释器：%s" % sys.executable)
        version = pipwrap.pip_version()
        self._log("pip：%s" % (version or "不可用"))
        if not version:
            messagebox.showwarning("pip 不可用", "未检测到 pip，安装/升级/卸载功能将无法使用。")
        if not config.is_admin():
            if messagebox.askyesno(
                "权限提示",
                "当前不是管理员权限，安装/卸载系统目录时可能被拒绝。\n\n"
                "是否立即以管理员身份重启本程序？\n（选择“否”将以普通权限继续，可勾选 --user 安装）",
            ):
                if config.relaunch_as_admin():
                    self.destroy()
                    return
                messagebox.showwarning("权限", "提权失败或被取消，将以普通权限继续。")
        self._refresh_installed()

    # ------------------------------------------------------------ 标签页
    def _build_notebook(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.installed_tab = ttk.Frame(nb, padding=6)
        self.online_tab = ttk.Frame(nb, padding=6)
        nb.add(self.installed_tab, text="  已安装包  ")
        nb.add(self.online_tab, text="  在线搜索 / 安装  ")
        self._build_installed_tab()
        self._build_online_tab()

    # ------------------------------------------------------------ 已安装包页
    def _build_installed_tab(self):
        top = ttk.Frame(self.installed_tab)
        top.pack(fill="x")

        ttk.Label(top, text="筛选：").pack(side="left")
        self.filter_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.filter_var, width=24)
        entry.pack(side="left")
        entry.bind("<Return>", lambda _e: self._populate_installed())
        ttk.Button(top, text="应用", width=6, command=self._populate_installed).pack(side="left", padx=4)

        self.only_outdated_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top, text="仅显示可更新", variable=self.only_outdated_var,
            command=self._populate_installed,
        ).pack(side="left", padx=8)

        self.count_var = tk.StringVar(value="共 0 个包")
        ttk.Label(top, textvariable=self.count_var, foreground="#555").pack(side="right")

        body = ttk.Frame(self.installed_tab)
        body.pack(fill="both", expand=True, pady=6)

        self.tree = ttk.Treeview(
            body, columns=[c[0] for c in INSTALLED_COLS],
            show="headings", selectmode="extended",
        )
        for col, title, width, anchor in INSTALLED_COLS:
            self.tree.heading(col, text=title, command=lambda c=col: self._sort_tree(c))
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "summary"))
        self.tree.tag_configure("outdated", foreground="#b35900")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", self._show_detail)

        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        bottom = ttk.Frame(self.installed_tab)
        bottom.pack(fill="x")
        self._button(bottom, "刷新", self._refresh_installed, width=8).pack(side="left")
        self._button(bottom, "检查更新", self._check_outdated, width=10).pack(side="left", padx=4)
        self._button(bottom, "升级所选", self._upgrade_selected, width=10).pack(side="left")
        self._button(bottom, "升级全部可更新", self._upgrade_all, width=14).pack(side="left", padx=4)
        self._button(bottom, "卸载所选", self._uninstall_selected, width=10).pack(side="left")
        self._button(bottom, "复制包名", self._copy_names, width=10).pack(side="left", padx=4)
        self._button(bottom, "清理无效残留", self._cleanup_dialog, width=12).pack(side="right", padx=4)

    def _sort_tree(self, col):
        rows = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children("")]
        if col in ("size", "requires", "used_by"):
            rows.sort(key=lambda r: self._numeric_value(r[1], col))
        else:
            rows.sort(key=lambda r: (r[0] or "").lower())
        if getattr(self, "_last_sort", None) == col:
            rows.reverse()
            self._last_sort = None
        else:
            self._last_sort = col
        for index, (_value, iid) in enumerate(rows):
            self.tree.move(iid, "", index)

    def _numeric_value(self, iid, col):
        key = self._key_iid.get(iid)
        item = self.by_key.get(key or "")
        if not item:
            return 0
        if col == "size":
            return item.get("size") or 0
        if col == "requires":
            return len(item.get("requires") or [])
        return len(item.get("used_by") or [])

    def _refresh_installed(self):
        self.run_async(
            pkgs.scan_installed,
            on_done=self._on_scanned,
            status="正在读取已安装包...",
        )

    def _on_scanned(self, items):
        self.packages = items or []
        self.by_key = {item["key"]: item for item in self.packages}
        self._populate_installed()
        self._log("已加载 %d 个已安装包。" % len(self.packages))
        threading.Thread(target=self._compute_sizes, daemon=True).start()

    def _compute_sizes(self):
        for key, size in pkgs.iter_sizes():
            self.post(lambda k=key, s=size: self._apply_size(k, s))
        self.post(self._update_count)

    def _apply_size(self, key, size):
        item = self.by_key.get(key)
        if not item:
            return
        item["size"] = (item.get("size") or 0) + size
        iid = self._iid_by_key.get(key)
        if iid and self.tree.exists(iid):
            values = list(self.tree.item(iid, "values"))
            values[3] = human_size(item["size"])
            self.tree.item(iid, values=values)

    def _populate_installed(self):
        self.tree.delete(*self.tree.get_children())
        self._key_iid = {}
        self._iid_by_key = {}
        query = self.filter_var.get().strip().lower()
        only_outdated = self.only_outdated_var.get()
        for item in self.packages:
            if only_outdated and item["key"] not in self.outdated:
                continue
            if query and query not in item["name"].lower() \
                    and query not in (item["summary"] or "").lower():
                continue
            latest = self.outdated.get(item["key"], "")
            tags = ("outdated",) if latest else ()
            iid = self.tree.insert("", "end", tags=tags, values=(
                item["name"],
                item["version"],
                latest,
                human_size(item["size"]),
                len(item["requires"]),
                len(item["used_by"]),
                item["summary"],
            ))
            self._key_iid[iid] = item["key"]
            self._iid_by_key[item["key"]] = iid
        self._update_count()

    def _update_count(self):
        total = sum(item["size"] or 0 for item in self.packages)
        pending = sum(1 for item in self.packages if item["size"] is None)
        suffix = "" if not pending else "（%d 个大小计算中）" % pending
        self.count_var.set("共 %d 个包，占用 %s%s" % (len(self.packages), human_size(total), suffix))

    def _selected_keys(self):
        keys = []
        for iid in self.tree.selection():
            key = self._key_iid.get(iid)
            if key:
                keys.append(key)
        return keys

    def _selected_names(self):
        return [self.by_key[k]["name"] for k in self._selected_keys() if k in self.by_key]

    def _check_outdated(self):
        source = self._current_source()["url"]

        def job():
            self.log_async("> 正在检查更新（pip list --outdated），请稍候...")
            return pipwrap.list_outdated(source)

        def done(result):
            all_out = result or {}
            # 仅保留本工具能识别并操作的包：pip 列出的某些包（如 setuptools）可能
            # 未出现在 importlib.metadata 的扫描结果中，直接计入会虚报数量。
            self.outdated = {k: v for k, v in all_out.items() if k in self.by_key}
            skipped = len(all_out) - len(self.outdated)
            self._populate_installed()
            self._log("可更新包：%d 个。" % len(self.outdated))
            if skipped:
                self._log("（已忽略 %d 个本工具无法识别的包：%s）" % (
                    skipped, ", ".join(sorted(k for k in all_out if k not in self.by_key))))
            if not self.outdated:
                messagebox.showinfo("检查更新", "所有包都是最新的。")

        self.run_async(job, on_done=done, status="正在检查更新...")

    def _upgrade_selected(self):
        names = self._selected_names()
        if not names:
            messagebox.showinfo("升级", "请先在列表中选择要升级的包。")
            return
        self._do_upgrade(names)

    def _upgrade_all(self):
        if not self.outdated:
            if messagebox.askyesno("升级", "尚未检查更新，是否先检查？"):
                self._check_outdated()
            return
        names = [self.by_key[k]["name"] for k in self.outdated if k in self.by_key]
        if not names:
            return
        if not messagebox.askyesno("升级", "将升级 %d 个包，确定继续？" % len(names)):
            return
        self._do_upgrade(names)

    def _do_upgrade(self, names):
        source = self._current_source()["url"]

        def job():
            self.log_async("> pip install --upgrade %s" % " ".join(names))
            return pipwrap.install(names, upgrade=True, index_url=source, on_line=self.log_async)

        def done(result):
            self._finish_pip(result, "升级完成")
            self.outdated = {}

        self.run_async(job, on_done=done, status="正在升级：%s" % "、".join(names[:3]))

    def _uninstall_selected(self):
        keys = self._selected_keys()
        if not keys:
            messagebox.showinfo("卸载", "请先在列表中选择要卸载的包。")
            return
        names = [self.by_key[k]["name"] for k in keys if k in self.by_key]

        dependents = set()
        for key in keys:
            for dep in self.by_key[key]["used_by"]:
                if dep not in keys:
                    dependents.add(dep)

        extra = []
        if dependents:
            dep_names = sorted(self.by_key[d]["name"] for d in dependents)
            if messagebox.askyesno(
                "存在依赖包",
                "以下已安装的包依赖于 %s：\n\n  %s\n\n是否同时卸载这些依赖包？\n"
                "（选择“否”只卸载 %s）" % ("、".join(names), "\n  ".join(dep_names), "、".join(names)),
            ):
                extra = dep_names

        targets = names + extra
        warning = ""
        risky = sorted(set(targets) & CORE_PACKAGES)
        if risky:
            warning = "\n\n警告：%s 属于 Python 基础组件，卸载可能导致环境损坏！" % "、".join(risky)
        if not messagebox.askyesno("确认卸载", "确定卸载以下 %d 个包？\n\n  %s%s"
                                   % (len(targets), "\n  ".join(targets), warning)):
            return

        def job():
            self.log_async("> pip uninstall -y %s" % " ".join(targets))
            return pipwrap.uninstall(targets, on_line=self.log_async)

        self.run_async(job, on_done=lambda r: self._finish_pip(r, "卸载完成"), status="正在卸载...")

    def _copy_names(self):
        names = self._selected_names()
        if not names:
            return
        self.clipboard_clear()
        self.clipboard_append(" ".join(names))
        self.status_var.set("已复制：%s" % " ".join(names))

    def _show_detail(self, _event=None):
        keys = self._selected_keys()
        if not keys:
            return
        item = self.by_key[keys[0]]
        requires = [self.by_key[k]["name"] for k in item["requires"] if k in self.by_key]
        used_by = [self.by_key[k]["name"] for k in item["used_by"] if k in self.by_key]
        messagebox.showinfo(
            "包详情 —— %s" % item["name"],
            "版本：%s\n大小：%s\n路径：%s\n\n说明：%s\n\n依赖（%d）：%s\n\n被依赖（%d）：%s"
            % (
                item["version"],
                human_size(item["size"]),
                item["location"] or "-",
                item["summary"] or "-",
                len(requires), "、".join(requires) or "无",
                len(used_by), "、".join(used_by) or "无",
            ),
        )

    # ------------------------------------------------------------ 清理无效残留
    def _cleanup_dialog(self):
        items = pkgs.find_invalid_leftovers()
        if not items:
            messagebox.showinfo("清理无效残留", "未发现中断卸载的残留目录（~ 开头的 dist-info / egg-info / libs）。")
            return

        win = tk.Toplevel(self)
        win.title("清理无效残留")
        win.transient(self)
        win.geometry("720x420")
        win.grab_set()

        ttk.Label(
            win,
            text="这些是 pip 卸载被中断后留下的 '~' 前缀目录，pip 会忽略它们、但本工具仍能读到的包。\n"
                 "修复 = 重命名回正确名字，之后即可在“已安装包”里正常卸载。",
            justify="left", wraplength=680,
        ).pack(anchor="w", padx=10, pady=8)

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=10)
        tree = ttk.Treeview(
            frame, columns=("kind", "mangled", "recovered"), show="headings", height=12,
        )
        tree.heading("kind", text="类型")
        tree.heading("mangled", text="残留目录")
        tree.heading("recovered", text="修复为")
        tree.column("kind", width=80, anchor="center")
        tree.column("mangled", width=280)
        tree.column("recovered", width=300)
        tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)

        iids = []
        for item in items:
            iid = tree.insert("", "end", values=(item["kind"], item["mangled"], item["recovered"]))
            iids.append(iid)
        for iid in iids:
            tree.selection_add(iid)

        status = tk.StringVar(value="共 %d 个残留，已默认全选。" % len(items))
        ttk.Label(win, textvariable=status, foreground="#555").pack(anchor="w", padx=10)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=8)

        def select_all():
            tree.selection_set(iids)

        def select_none():
            tree.selection_remove(iids)

        def repair_selected():
            chosen = [items[tree.index(iid)] for iid in tree.selection()]
            if not chosen:
                messagebox.showinfo("清理", "请先选择要修复的目录。")
                return
            ok = fail = 0
            lines = []
            for item in chosen:
                success, note = pkgs.repair_leftover(item)
                lines.append(("OK  " if success else "FAIL ") + note)
                if success:
                    ok += 1
                else:
                    fail += 1
            for line in lines:
                self.log_async(line)
            status.set("已修复 %d 个，失败 %d 个。" % (ok, fail))
            if ok:
                messagebox.showinfo(
                    "清理完成",
                    "已修复 %d 个目录，它们现在会正常出现在“已安装包”列表中，可正常卸载。\n"
                    "（失败项通常是权限不足，请以管理员身份运行本程序后重试）" % ok,
                )
                win.destroy()
                self._refresh_installed()
            else:
                messagebox.showwarning("清理失败", "全部失败，多半是权限不足：请以管理员身份运行本程序后重试。")

        ttk.Button(bar, text="全选", width=8, command=select_all).pack(side="left")
        ttk.Button(bar, text="全不选", width=8, command=select_none).pack(side="left", padx=6)
        ttk.Button(bar, text="修复选中", width=12, command=repair_selected).pack(side="left", padx=12)
        ttk.Button(bar, text="关闭", width=8, command=win.destroy).pack(side="right")

    # ------------------------------------------------------------ 在线安装页
    def _build_online_tab(self):
        top = ttk.Frame(self.online_tab)
        top.pack(fill="x")

        ttk.Label(top, text="关键字：").pack(side="left")
        self.query_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query_var, width=28)
        entry.pack(side="left")
        entry.bind("<Return>", lambda _e: self._do_search())
        self._button(top, "搜索", self._do_search, width=8).pack(side="left", padx=4)
        self._button(top, "重建索引缓存", lambda: self._do_search(force=True), width=14).pack(side="left")

        self.result_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.result_var, foreground="#555").pack(side="right")

        body = ttk.Frame(self.online_tab)
        body.pack(fill="both", expand=True, pady=6)

        self.result_tree = ttk.Treeview(
            body, columns=[c[0] for c in RESULT_COLS], show="headings", height=12,
        )
        for col, title, width, anchor in RESULT_COLS:
            self.result_tree.heading(col, text=title)
            self.result_tree.column(col, width=width, anchor=anchor, stretch=(col == "name"))
        self.result_tree.pack(side="left", fill="both", expand=True)
        self.result_tree.bind("<<TreeviewSelect>>", self._on_result_selected)

        scroll = ttk.Scrollbar(body, orient="vertical", command=self.result_tree.yview)
        scroll.pack(side="right", fill="y")
        self.result_tree.configure(yscrollcommand=scroll.set)

        detail = ttk.LabelFrame(self.online_tab, text="安装", padding=8)
        detail.pack(fill="x")

        ttk.Label(detail, text="包名：").grid(row=0, column=0, sticky="w")
        self.detail_name_var = tk.StringVar(value="—")
        ttk.Label(detail, textvariable=self.detail_name_var, foreground="#0b5fff").grid(
            row=0, column=1, sticky="w")

        ttk.Label(detail, text="版本：").grid(row=0, column=2, sticky="e", padx=(16, 0))
        self.version_var = tk.StringVar(value="最新")
        self.version_box = ttk.Combobox(
            detail, textvariable=self.version_var, width=18, state="readonly", values=["最新"])
        self.version_box.grid(row=0, column=3, sticky="w")

        self.upgrade_var = tk.BooleanVar(value=False)
        self.user_var = tk.BooleanVar(value=not config.is_admin())
        ttk.Checkbutton(detail, text="升级安装 (--upgrade)", variable=self.upgrade_var).grid(
            row=0, column=4, sticky="w", padx=10)
        ttk.Checkbutton(detail, text="仅当前用户 (--user)", variable=self.user_var).grid(
            row=0, column=5, sticky="w")
        self._button(detail, "安装", self._install_current, width=10).grid(
            row=0, column=6, sticky="e", padx=(10, 0))
        detail.columnconfigure(6, weight=1)

        self.info_var = tk.StringVar(value="搜索结果中选择一个包后显示简介与可用版本。")
        ttk.Label(detail, textvariable=self.info_var, wraplength=900, justify="left",
                  foreground="#444").grid(row=1, column=0, columnspan=7, sticky="w", pady=(6, 0))

    def _do_search(self, force=False):
        query = self.query_var.get().strip()
        if not query:
            messagebox.showinfo("搜索", "请输入包名关键字。")
            return
        source = self._current_source()["url"]

        def job():
            self.log_async("> 在 %s 搜索 “%s”%s"
                           % (source, query, "（重建索引缓存）" if force else ""))
            self.post(lambda: self.result_var.set("正在获取索引（首次较慢）..."))
            return net.search_names(source, query, force=force)

        def done(names):
            names = names or []
            self.result_tree.delete(*self.result_tree.get_children())
            for name in names:
                local = self.by_key.get(net.normalize(name))
                self.result_tree.insert("", "end", values=(
                    name,
                    local["version"] if local else "",
                    "已安装" if local else "",
                ))
            self.result_var.set("命中 %d 个包名" % len(names))
            self._log("搜索 “%s” 命中 %d 个。" % (query, len(names)))
            if not names:
                messagebox.showinfo("搜索", "没有找到匹配的包名。")

        self.run_async(job, on_done=done, status="正在搜索：%s" % query)

    def _on_result_selected(self, _event=None):
        selection = self.result_tree.selection()
        if not selection:
            return
        name = self.result_tree.set(selection[0], "name")
        self._detail_name = name
        self.detail_name_var.set(name)
        self.version_box.configure(values=["最新"], state="readonly")
        self.version_var.set("最新")
        self._detail_versions = []
        source = self._current_source()["url"]
        self.info_var.set("正在获取 %s 的可用版本与简介..." % name)

        def job():
            versions = []
            try:
                versions = net.fetch_versions(name, source)
            except Exception as exc:
                self.log_async("[提示] 从 %s 获取版本列表失败：%s" % (source, exc))
            info = net.pypi_info(name)
            return versions, info

        def done(result):
            versions, info = result
            self._detail_versions = versions
            values = ["最新"] + versions[:40]
            self.version_box.configure(values=values, state="readonly")
            self.version_var.set("最新")
            summary = info.get("summary") or ""
            extra = []
            if info.get("version"):
                extra.append("最新版 %s" % info["version"])
            if info.get("author"):
                extra.append("作者 %s" % info["author"])
            if info.get("requires_python"):
                extra.append("Python %s" % info["requires_python"])
            if info.get("home_page"):
                extra.append(info["home_page"])
            tail = "  |  ".join(extra)
            self.info_var.set((summary + ("\n" + tail if tail else "")) if summary else tail or "无简介信息")

        self.run_async(job, on_done=done, status="正在获取 %s 的信息..." % name)

    def _install_current(self):
        name = self._detail_name
        if not name:
            messagebox.showinfo("安装", "请先在搜索结果中选择一个包。")
            return
        version = self.version_var.get()
        if self.upgrade_var.get() or not version or version == "最新":
            spec = name
        else:
            spec = "%s==%s" % (name, version)
        source = self._current_source()["url"]
        user = self.user_var.get()
        upgrade = self.upgrade_var.get()

        def job():
            self.log_async("> pip install %s%s -i %s" % (spec, " --upgrade" if upgrade else "", source))
            return pipwrap.install([spec], upgrade=upgrade, user=user,
                                   index_url=source, on_line=self.log_async)

        self.run_async(
            job,
            on_done=lambda r: self._finish_pip(r, "安装完成"),
            status="正在安装 %s ..." % spec,
        )

    # ------------------------------------------------------------ 日志区
    def _build_log(self):
        bottom = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom.pack(fill="x")

        bar = ttk.Frame(bottom)
        bar.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.progress.pack(side="left", padx=8)
        ttk.Button(bar, text="清空日志", width=10,
                   command=lambda: self.log_text.delete("1.0", "end")).pack(side="right")

        self.log_text = tk.Text(bottom, height=9, wrap="none", relief="solid")
        self.log_text.pack(fill="x", pady=(4, 0))
        scroll = ttk.Scrollbar(self.log_text, orient="vertical", command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)

    # ------------------------------------------------------------ 通用收尾
    def _finish_pip(self, result, ok_message):
        code, _out = result if isinstance(result, tuple) else (0, "")
        if code != 0:
            self._log("pip 退出码：%s" % code)
            messagebox.showerror("执行失败", "pip 返回码 %s，详情请查看日志。" % code)
        else:
            self.status_var.set(ok_message)
            self._log(ok_message)
        self._refresh_installed()

    def _manage_sources(self):
        win = tk.Toplevel(self)
        win.title("安装源管理")
        win.transient(self)
        win.geometry("560x380")
        win.grab_set()

        box = ttk.Frame(win, padding=8)
        box.pack(fill="both", expand=True)

        ttk.Label(box, text="已配置的 pip 源（Simple 索引地址）").pack(anchor="w")
        list_frame = ttk.Frame(box)
        list_frame.pack(fill="both", expand=True, pady=4)
        lb = tk.Listbox(list_frame)
        lb.pack(side="left", fill="both", expand=True)
        lb_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=lb.yview)
        lb_scroll.pack(side="right", fill="y")
        lb.configure(yscrollcommand=lb_scroll.set)

        def refresh_list():
            lb.delete(0, "end")
            for src in self.sources:
                lb.insert("end", "%s    %s%s" % (src["name"], src["url"],
                                                 "   [自定义]" if src.get("custom") else ""))
        refresh_list()

        form = ttk.Frame(box)
        form.pack(fill="x")
        ttk.Label(form, text="名称：").pack(side="left")
        name_var = tk.StringVar()
        ttk.Entry(form, textvariable=name_var, width=12).pack(side="left")
        ttk.Label(form, text="地址：").pack(side="left", padx=(8, 0))
        url_var = tk.StringVar()
        ttk.Entry(form, textvariable=url_var, width=40).pack(side="left", fill="x", expand=True)

        def add_source():
            url = url_var.get().strip()
            if not url:
                return
            if not url.startswith("http"):
                messagebox.showwarning("地址无效", "请输入 http/https 开头的索引地址。")
                return
            name = name_var.get().strip() or url
            self.sources.append({"name": name, "url": net.normalize_url(url), "custom": True})
            config.save_custom_sources(self.sources)
            url_var.set("")
            name_var.set("")
            refresh_list()
            self._sync_source_box()

        def remove_source():
            idx = lb.curselection()
            if not idx:
                return
            src = self.sources[idx[0]]
            if not src.get("custom"):
                messagebox.showinfo("无法删除", "内置源不能删除。")
                return
            self.sources.pop(idx[0])
            config.save_custom_sources(self.sources)
            refresh_list()
            self._sync_source_box()

        def use_source():
            idx = lb.curselection()
            if not idx:
                return
            self.source_box.current(idx[0])
            self._on_source_changed()
            win.destroy()

        btns = ttk.Frame(box)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="添加", width=8, command=add_source).pack(side="left")
        ttk.Button(btns, text="删除", width=8, command=remove_source).pack(side="left", padx=6)
        ttk.Button(btns, text="设为当前源", width=12, command=use_source).pack(side="left")
        ttk.Button(btns, text="关闭", width=8, command=win.destroy).pack(side="right")

    def _sync_source_box(self):
        self.source_box.configure(values=[s["name"] for s in self.sources])


def main():
    if sys.version_info < (3, 8):
        raise SystemExit("PipMgr 需要 Python 3.8 或更高版本。")
    app = App()
    app.mainloop()
