#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tkinter 图形控制面板。

把日常要用的几件事收进一个窗口：启停 OPDS 书源、启停目录监控、执行一次
同步 / 镜像、查看镜像差异、跑隧道向导，外加实时状态灯与输出日志。

实现要点：
  * 只依赖标准库（tkinter），打包 exe 时无需额外依赖；
  * 子进程统一走 :func:`_spawn`，源码运行与 exe 运行都能正确拉起自己
    （frozen 时 ``sys.executable`` 就是 exe，参数透传给同一套 CLI）；
  * 子进程输出用队列回传主线程再写进 Text，避免跨线程操作 Tk 组件；
  * 日志区版式见下面的「日志区版式」常量：全文一套字体 / 字号 / 行距，只用颜色与
    字重区分语义，靠三列对齐 + 分块留白分层（详见 :func:`parse_log_line` 与
    :meth:`Panel._row`）。
"""

import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from . import paths as P

# Windows：不弹出控制台黑框
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

TITLE = "Light-Novel 书库控制面板"

# ============================ 日志区版式约束 ============================
# 目标：简洁舒适、层次分明。做法是「版式统一 + 语义配色」而不是花哨装饰 ——
#   1. 字体族**跟随面板 UI 字体**（不指定西文字体），字号全文一个取值；
#   2. 每行都是同一套三列栅格：``时间 | 级别 | 正文``，落点由像素制表位确定；
#   3. 只有「级别 / 结果」用颜色，其余一律文字色，避免颜色打架；
#   4. 一个任务 = 一个块：块前留白 + 标题条（全局唯一底色）+ 命令回显，块尾收一行结果。
#
# ⚠ 为什么**不**指定 Consolas 之类的西文等宽字体：日志是中英混排，而 Consolas 没有
#   汉字字形，中文会被系统「字体链接」回退成另一套字形 —— 同一行两种字体，一眼就能
#   看出来（用户反馈的「字体依旧不一致」就是这个）。改成复制 ``TkDefaultFont``
#   （= 面板其它控件用的那套），日志与界面同族，也就不存在回退。
#   代价是字体不再等宽，所以三列对齐改由 :func:`grid_tabs` 算出的**像素制表位**保证。
LOG_FONT_SIZE = 10          # 唯一字号（字体族跟随 TkDefaultFont）
COL_GUTTER    = 8           # 行首留白（像素）
COL_GAP       = 12          # 列间距（像素）
# 所有级别 / 结果名，用来量出「级别列」要留多宽（取最宽的那个）
_CHIP_NAMES = ("DEBUG", "ERROR", "FAIL", "INFO", "OK", "STOP", "WARN")

_FG_TEXT = "#24292F"        # 正文
_FG_TS   = "#8C959F"        # 时间戳（弱化，退到背景层）
_FG_DIM  = "#6E7781"        # 命令回显 / 说明性尾注
_BG_HDR  = "#EAEEF2"        # 块标题底（全局唯一底色，用来分层）
_BG_OK, _FG_OK     = "#DAFBE1", "#1A7F37"   # 成功
_BG_WARN, _FG_WARN = "#FFF8C5", "#9A6700"   # 警告
_BG_ERR, _FG_ERR   = "#FFEBE9", "#CF222E"   # 失败
_BG_INFO, _FG_INFO = "#DDF4FF", "#0969DA"   # 信息
_BG_LOG            = "#FFFFFF"              # 日志底：纯白，避免与色块互相干扰

# 标签 -> (前景, 背景, 字重)。行距与挂起缩进不在这里配 —— 它们统一由 Text 组件
# 的 spacing* / lmargin2 决定，这样「不管哪种颜色的行，左边界与行高都完全一致」。
LOG_TAGS = {
    "msg":  (_FG_TEXT, None,     "normal"),
    "ts":   (_FG_TS,   None,     "normal"),
    "dim":  (_FG_DIM,  None,     "normal"),
    "hdr":  (_FG_TEXT, _BG_HDR,  "bold"),
    "info": (_FG_INFO, _BG_INFO, "bold"),
    "dbg":  (_FG_TS,   _BG_HDR,  "normal"),
    "warn": (_FG_WARN, _BG_WARN, "bold"),
    "err":  (_FG_ERR,  _BG_ERR,  "bold"),
    "ok":   (_FG_OK,   _BG_OK,   "bold"),
    "stop": (_FG_DIM,  _BG_HDR,  "bold"),
}

# 级别名统一压到 5 字符以内（不换行、不错位）：显示名 -> 标签
_LEVEL_SHOW = {"DEBUG": ("DEBUG", "dbg"), "INFO": ("INFO", "info"),
               "WARNING": ("WARN", "warn"), "ERROR": ("ERROR", "err"),
               "CRITICAL": ("FATAL", "err")}
_CHIP = {"info": "INFO", "dbg": "DEBUG", "warn": "WARN", "err": "ERROR"}

_LEVEL_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) +(\d{2}:\d{2}:\d{2})\]\s+([A-Z]+):\s?(.*)$")


def parse_log_line(line):
    """把子进程的一行输出拆成 ``(时间, 标签, 正文)``。

    子进程日志形如 ``[2026-09-14 20:18:52] INFO: 远程 origin 已配置：…``。面板左侧
    已经有独立的时间列，日期属于纯噪音，所以只取 ``HH:MM:SS``；级别映射成标签
    （见 ``_LEVEL_SHOW``），调用方据此上色。

    认不出来的行（git 原始输出、二维码、报错栈）返回 ``(None, "", line)``，仍按同一
    栅格缩进到正文列 —— 版式不因为「内容不认识」而乱掉。
    """
    m = _LEVEL_RE.match(line)
    if not m:
        return None, "", line
    show = _LEVEL_SHOW.get(m.group(3))
    if not show:
        return m.group(2), "", line
    return m.group(2), show[1], m.group(4).rstrip()


def grid_tabs(regular, bold):
    """算出三列栅格的**像素**制表位：``(时间列, 正文列)``。

    为什么是像素制表位、而不是「按字符数补空格」：日志用面板 UI 字体（比例字体），
    每个字符宽度不等，补空格根本对不齐；制表位与字体宽窄无关，且 ``lmargin2`` 用
    同一个值就能让折行的续行也顶到正文列。级别列宽度取所有级别名里最宽的那个。
    """
    time_tab = COL_GUTTER + regular.measure("00:00:00") + COL_GAP
    chip_w = max(bold.measure(n) for n in _CHIP_NAMES)
    return time_tab, time_tab + chip_w + COL_GAP


def row_parts(ts, chip, tag, text, tail=None):
    """构造一行日志的列片段：``时间 → 制表 → 级别 → 制表 → 正文``（+ 弱化尾注）。

    全篇唯一的「行构造入口」：三列的落点由 :func:`grid_tabs` 统一算出来，所以不管
    字体是比例还是等宽、级别名几个字符，正文都落在同一列。任何一处想加前缀，都得改
    这里 —— 版式不会被别的调用点悄悄改歪。
    """
    return [(ts or "", "ts"),
            ("\t", "msg"),
            ((" %s " % chip) if chip else "", tag),
            ("\t", "msg"),
            (text, "msg"),
            (tail or "", "dim"),
            ("\n", "msg")]


def _spawn_args(args):
    """把子命令转成可执行命令：源码运行时补 ``-m lightnovel``，exe 时直接透传。"""
    if getattr(sys, "frozen", False):
        return [sys.executable] + list(args)
    return [sys.executable, "-m", "lightnovel"] + list(args)


def _port_open(port, host="127.0.0.1", timeout=0.35):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _hide_console():
    """exe 以 console 模式打包时，双击会先弹一个控制台；图形面板启动后自动隐藏它。

    只对 ``ui`` 子命令调用 —— headless 子命令（sync / mirror / opds）需要保留
    控制台看输出，所以不能在 cli 层统一隐藏。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)      # SW_HIDE
    except Exception:
        pass


def _cloudflared_running():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq cloudflared.exe"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=_NO_WINDOW, timeout=8)
        return "cloudflared.exe" in (r.stdout or "")
    except Exception:
        return False


class Panel(tk.Tk):
    def __init__(self, port=None):
        super().__init__()
        self.port = int(port or P.PORT)
        self.procs = {}            # key -> Popen
        self.q = queue.Queue()
        self._stopped = set()      # 被面板主动停掉的 key：页脚显示「已停止」而非 FAIL
        self._last_blank = True    # 连续留白只留一行，避免日志被空行切碎

        self.title(TITLE)
        # 日志区是主角，尽量给它高度（改版前 860x620 只露出 5 行）；但小屏上别顶到
        # 屏幕外，所以按屏幕高度收一收。
        self.geometry("900x%d" % max(560, min(730, self.winfo_screenheight() - 140)))
        self.minsize(780, 560)

        self._build_status()
        self._build_actions()
        self._build_log()

        self.after(120, self._drain)
        self.after(400, self._refresh)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log_block("面板已就绪")
        self._log("服务与监控以独立进程运行；关闭本窗口不会停止它们。", tag="dim")

    # ------------------------------------------------------------ 布局
    def _build_status(self):
        box = ttk.LabelFrame(self, text=" 服务状态 ")
        box.pack(fill="x", padx=10, pady=(10, 6))

        self.dot = {}
        self.detail = {}
        rows = [("opds", "OPDS 书源"), ("monitor", "目录监控"), ("tunnel", "公网隧道")]
        for i, (key, label) in enumerate(rows):
            ttk.Label(box, text=label, width=12).grid(row=i, column=0, sticky="w", padx=(8, 2), pady=3)
            d = tk.Label(box, text="●", fg="#999999", font=("Segoe UI", 12))
            d.grid(row=i, column=1, padx=(0, 4))
            self.dot[key] = d
            t = ttk.Label(box, text="检测中…", foreground="#444444")
            t.grid(row=i, column=2, sticky="w")
            self.detail[key] = t
        box.columnconfigure(2, weight=1)

        url_row = ttk.Frame(box)
        url_row.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
        ttk.Label(url_row, text="订阅地址").pack(side="left")
        self.url_var = tk.StringVar(value="")
        e = ttk.Entry(url_row, textvariable=self.url_var, state="readonly")
        e.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(url_row, text="复制", width=6, command=self._copy_url).pack(side="left")

    def _build_actions(self):
        box = ttk.LabelFrame(self, text=" 操作 ")
        box.pack(fill="x", padx=10, pady=6)

        groups = [
            ("OPDS", [("启动 OPDS 服务", lambda: self._start_opds()),
                      ("停止 OPDS 服务", lambda: self._stop("opds"))]),
            ("监控", [("启动目录监控", lambda: self._start_monitor()),
                      ("停止监控", lambda: self._stop("monitor"))]),
            ("同步", [("同步一次", lambda: self._run_once("同步一次", "sync", "--once")),
                      ("镜像到 F 盘", lambda: self._run_once("镜像 F 盘", "mirror")),
                      ("查看 F 盘差异", lambda: self._run_once("F 盘差异", "mirror", "--status"))]),
            ("其它", [("公网隧道向导", self._tunnel_wizard),
                      ("打开书库目录", lambda: self._open(P.LIGHT_NOVEL_DIR)),
                      ("打开数据目录", lambda: self._open(P.LOG_DIR)),
                      ("刷新状态", self._refresh)]),
        ]
        for col, (name, btns) in enumerate(groups):
            g = ttk.LabelFrame(box, text=" " + name + " ")
            g.grid(row=0, column=col, sticky="nsew", padx=6, pady=6)
            for b in btns:
                ttk.Button(g, text=b[0], width=16, command=b[1]).pack(fill="x", padx=6, pady=3)
        for c in range(len(groups)):
            box.columnconfigure(c, weight=1)

    def _build_log(self):
        box = ttk.LabelFrame(self, text=" 输出 / 日志 ")
        box.pack(fill="both", expand=True, padx=10, pady=(6, 4))

        bar = ttk.Frame(box)
        bar.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Button(bar, text="清空", width=8, command=self._clear_log).pack(side="left")
        ttk.Button(bar, text="打开 opds.log", width=14,
                   command=lambda: self._open(P.OPDS_LOG_FILE)).pack(side="left", padx=4)
        ttk.Button(bar, text="打开 sync.log", width=14,
                   command=lambda: self._open(P.LOG_FILE)).pack(side="left")
        ttk.Button(bar, text="打开镜像看板", width=14,
                   command=lambda: self._open(P.MIRROR_HTML_FILE)).pack(side="left", padx=4)

        wrap = ttk.Frame(box)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)
        # 字体：复制面板自己的 UI 字体（TkDefaultFont）改字号 —— 与控件同族、中英都覆盖，
        # 不会像 Consolas 那样把汉字丢给系统回退成另一套字形。
        regular = tkfont.nametofont("TkDefaultFont").copy()
        regular.configure(size=LOG_FONT_SIZE)
        bold = regular.copy()
        bold.configure(weight="bold")
        self._log_fonts = (regular, bold)      # 留引用，Tk 字体对象被 GC 会退化
        tab_time, tab_msg = grid_tabs(regular, bold)
        self._log_tabs = (tab_time, tab_msg)   # 供排查用
        # spacing1/2/3 = 行距（段前 / 折行间 / 段后），是 Text 组件级选项，所以全篇一致；
        # 制表位 tabs 与挂起缩进 lmargin1/lmargin2 是**标签专属**选项（Text 组件不接受
        # lmargin*，写进构造器会 TclError: unknown option "-lmargin1"），所以下面每个标签
        # 都配同一套值 —— 这是「三列对齐 + 折行续行对齐」的唯一来源，任何行型都不例外。
        self.log = tk.Text(wrap, height=18, wrap="word", background=_BG_LOG,
                           foreground=_FG_TEXT, insertbackground=_FG_TEXT,
                           font=regular, relief="solid", borderwidth=1,
                           padx=10, pady=8, spacing1=2, spacing2=3, spacing3=4,
                           tabs=(tab_time, tab_msg),
                           selectbackground=_BG_INFO, selectforeground=_FG_TEXT,
                           highlightthickness=0)
        for _name, (_fg, _bg, _weight) in LOG_TAGS.items():
            _opts = {"foreground": _fg, "font": bold if _weight == "bold" else regular,
                     "tabs": (tab_time, tab_msg),
                     "lmargin1": COL_GUTTER, "lmargin2": tab_msg}
            if _bg:
                _opts["background"] = _bg
            self.log.tag_configure(_name, **_opts)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.configure(state="disabled")

        self.status = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status, anchor="w", foreground="#444444").pack(
            fill="x", padx=12, pady=(0, 8))

    # ------------------------------------------------------------ 日志
    # 版式原则见文件头「日志区版式约束」：所有行共用一套栅格
    #     时间 → [制表] → 级别 → [制表] → 正文
    # 制表位与挂起缩进由 grid_tabs() 统一算（像素制表位，与字体宽窄无关），
    # 折行的续行由 lmargin2 顶到同一列，所以「对齐」不依赖内容长度、也不依赖等宽字体。
    def _insert(self, *parts):
        """追加一行。``parts`` 是多个 ``(文本, 标签)`` 片段，标签为空则用正文样式。"""
        self.log.configure(state="normal")
        for text, tag in parts:
            self.log.insert("end", text, tag or "msg")
        self.log.see("end")
        self.log.configure(state="disabled")
        self._last_blank = False

    def _blank(self):
        """块与块之间的留白（连续调用只留一行，免得日志被空行切碎）。"""
        if not self._last_blank:
            self._insert(("\n", "msg"))

    def _log(self, text, tag="msg"):
        """面板自己的一句话：同样走三列栅格（时间列有值、级别列留空、正文右移），
        这样「面板说的话」和「子进程说的话」共用同一条左边界，整屏不会有两套对齐。"""
        self._row(time.strftime("%H:%M:%S"), "", tag, text)

    def _log_block(self, label, cmd_line=None):
        """块首（标题层）：留白 + 时间 + 粗体标题条 + 可选命令回显。"""
        self._blank()
        self._insert(("%s\t\t" % time.strftime("%H:%M:%S"), "ts"),
                     ("▌ %s\n" % label, "hdr"))     # 两个制表位跳过级别列 -> 标题与正文同列
        if cmd_line:
            self._insert(("\t\t$ %s\n" % cmd_line, "dim"))

    def _row(self, ts, chip, tag, text, tail=None):
        """一行正文条目：时间 ┊ 色块 ┊ 正文（+ 弱化尾注），三列固定对齐。"""
        self._insert(*row_parts(ts, chip, tag, text, tail))

    def _log_child(self, line):
        """子进程的一行输出：认得出级别就上色，认不出也照样缩进到正文列。"""
        ts, tag, text = parse_log_line(line)
        self._row(ts, _CHIP.get(tag, ""), tag or "msg", text)

    def _log_end(self, label, rc, secs, stopped=False):
        """块尾：一行结果 + 用时，然后留白，块与块之间自然分开。"""
        if stopped:
            chip, tag, text = "STOP", "stop", "已停止（面板主动关闭）"
        elif rc == 0:
            chip, tag, text = "OK", "ok", "完成（退出码 0）"
        else:
            chip, tag, text = "FAIL", "err", "结束（退出码 %s）" % rc
        self._row(time.strftime("%H:%M:%S"), chip, tag,
                  "%s %s" % (label, text), tail="  · 用时 %.1f 秒" % secs)
        self._blank()

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._last_blank = True

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "child":
                    self._log_child(payload)
                elif kind == "end":
                    label, key, rc, secs = payload
                    self._log_end(label, rc, secs, stopped=key in self._stopped)
                    self._stopped.discard(key)
                elif kind == "status":
                    self.status.set(payload)
                else:
                    self._log(payload)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _pump(self, proc, label, key=None):
        """把子进程输出搬进队列（跨线程只碰队列，Tk 组件一律回主线程写）。"""
        start = time.time()

        def reader():
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        self.q.put(("child", line))
            except Exception:
                pass
            rc = proc.wait()        # 等真退出：poll() 在读到 EOF 的瞬间常还是 None，
            self.q.put(("end", (label, key, rc, time.time() - start)))
        threading.Thread(target=reader, daemon=True).start()

    # ------------------------------------------------------------ 子进程
    def _run(self, tag, args, console=False, track=None, note=None):
        if track and track in self.procs and self.procs[track].poll() is None:
            self._log_block(tag)
            self._log("%s 已在运行，未重复启动。" % tag, tag="warn")
            return None
        cmd = _spawn_args(args)
        flags = _NEW_CONSOLE if console else _NO_WINDOW
        self._log_block(tag, cmd_line=" ".join(args))
        if note:
            self._log(note, tag="dim")
        try:
            # 管道里钉死 UTF-8 是子进程自己做的（lightnovel.cli._pipe_utf8）：
            # 给子进程设 PYTHONIOENCODING 对 PyInstaller 打出来的 exe 无效，实测过。
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=flags, cwd=P.TARGET_DIR)
        except Exception as exc:
            self._log("启动失败：%s" % exc, tag="err")
            messagebox.showerror(TITLE, "启动失败：\n%s" % exc)
            return None
        if track:
            self.procs[track] = proc
        self._pump(proc, tag, track)
        return proc

    def _start_opds(self):
        self._run("OPDS 书源", ["opds", "--port", str(self.port)], track="opds")
        self.status.set("OPDS 服务启动中…")
        self.after(1200, self._refresh)

    def _start_monitor(self):
        self._run("目录监控", ["sync"], track="monitor")
        self.status.set("目录监控启动中…")
        self.after(1200, self._refresh)

    def _run_once(self, tag, *args):
        proc = self._run(tag, list(args))
        if proc is None:
            return
        self.status.set("%s 执行中…" % tag)

        def waiter():
            rc = proc.wait()
            # 日志页脚由 _pump 统一收口（只此一处，不再出现两条结束汇报）；
            # 这里只负责把底部状态栏恢复成「已结束」。
            self.q.put(("status", "%s 已结束（退出码 %s）" % (tag, rc)))
        threading.Thread(target=waiter, daemon=True).start()

    def _tunnel_wizard(self):
        self._run("隧道向导", ["tunnel-setup"], console=True,
                  note="该向导需要交互输入，将在独立控制台窗口中打开。")

    def _stop(self, key):
        proc = self.procs.get(key)
        if proc and proc.poll() is None:
            self._log_block("停止 %s" % key)
            self._log("正在停止（PID %s）…" % proc.pid, tag="dim")
            self._stopped.add(key)          # 页脚据此显示「已停止」而不是 FAIL
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self.procs.pop(key, None)
            self.after(600, self._refresh)
            return
        if key == "opds":
            owners = self._port_owners(self.port)
            if not owners:
                messagebox.showinfo(TITLE, "未发现占用 %d 端口的进程，服务应该没在运行。" % self.port)
                return
            if messagebox.askyesno(TITLE, "端口 %d 被外部进程占用：%s\n\n强制结束它？"
                                          "（等价于 launchers\\stop_opds.bat）" % (self.port, owners)):
                for pid in owners:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, creationflags=_NO_WINDOW)
                self._log("已强制结束 %s" % owners, tag="warn")
                self.after(600, self._refresh)
            return
        pid = self._monitor_pid()
        messagebox.showinfo(TITLE,
                            "目录监控不是由本面板启动的%s，未做处理。\n\n"
                            "请到启动它的那个控制台窗口按 Ctrl+C 停止。"
                            % ("（PID %s）" % pid if pid else "（或已经退出）"))

    @staticmethod
    def _port_owners(port):
        try:
            r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               creationflags=_NO_WINDOW, timeout=10)
            pids = set()
            for line in (r.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(":%d" % port) \
                        and parts[3].upper() == "LISTENING":
                    pids.add(parts[4])
            return sorted(pids)
        except Exception:
            return []

    # ------------------------------------------------------------ 状态
    def _refresh(self):
        live = _port_open(self.port)
        self._set("opds", live, ("运行中（端口 %d）" % self.port) if live else "已停止")

        # 目录监控：优先看锁文件；锁还没写但**本面板亲自拉起的进程还活着**也算运行中。
        # 之所以不能只看锁：锁是在初始同步（全量 F 盘镜像 + git 推送，实测两分钟起步）
        # 完成之后才写的，只认锁会让状态灯白白红两分钟，看起来就像「根本没启动」。
        pid = self._monitor_pid()
        booting = False
        if not pid:
            proc = self.procs.get("monitor")
            if proc is not None and proc.poll() is None:
                pid, booting = proc.pid, True
        if pid:
            self._set("monitor", True,
                      "运行中（PID %d%s）" % (pid, "，初始同步中" if booting else ""))
        else:
            self._set("monitor", False, "已停止")

        cf = _cloudflared_running()
        self._set("tunnel", cf, "运行中" if cf else "未运行")

        if live:
            self.url_var.set("http://%s:%d/" % (self._local_ip(), self.port))
        else:
            self.url_var.set("")
        self.after(3000, self._refresh)

    @staticmethod
    def _local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    @staticmethod
    def _monitor_pid():
        """读锁文件并确认那个 PID 还活着（只读探测，不会杀进程）。

        锁路径从 ``sync.monitor.lock_path()`` 取，而不是另写一份文件名 ——
        避免「监控写 A、面板读 B」。"""
        try:
            from .sync.monitor import _pid_alive, lock_path
        except Exception:
            return None
        try:
            with open(lock_path(), "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip())
        except (OSError, ValueError):
            return None
        return pid if _pid_alive(pid) else None

    def _set(self, key, ok, text):
        self.dot[key].configure(fg="#2e9e3e" if ok else "#c0392b")
        self.detail[key].configure(text=text)

    def _copy_url(self):
        url = self.url_var.get()
        if not url:
            messagebox.showinfo(TITLE, "服务未运行，暂无订阅地址。")
            return
        self.clipboard_clear()
        self.clipboard_append(url)
        self.status.set("已复制订阅地址：%s" % url)

    def _open(self, path):
        if not path or not os.path.exists(path):
            messagebox.showwarning(TITLE, "路径还不存在：\n%s" % path)
            return
        try:
            os.startfile(path)          # noqa: S606  (Windows 专用)
        except AttributeError:
            self._log("当前平台不支持直接打开目录：%s" % path, tag="err")

    def _on_close(self):
        running = [k for k, v in self.procs.items() if v.poll() is None]
        if running:
            if not messagebox.askyesno(TITLE, "以下服务仍在后台运行：%s\n\n仅关闭窗口（服务继续），确定吗？"
                                              % "、".join(running)):
                return
        self.destroy()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port = None
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = argv[i + 1]
        elif a.startswith("--port="):
            port = a.split("=", 1)[1]
    panel = Panel(port=port)
    panel.after(80, _hide_console)
    panel.mainloop()
    return 0


if __name__ == "__main__":
    main()
