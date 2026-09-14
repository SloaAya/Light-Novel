#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tkinter 图形控制面板。

把日常要用的几件事收进一个窗口：启停 OPDS 书源、启停目录监控、执行一次
同步 / 镜像、查看镜像差异、跑隧道向导，外加实时状态灯与输出日志。

实现要点：
  * 只依赖标准库（tkinter），打包 exe 时无需额外依赖；
  * 子进程统一走 :func:`_spawn`，源码运行与 exe 运行都能正确拉起自己
    （frozen 时 ``sys.executable`` 就是 exe，参数透传给同一套 CLI）；
  * 子进程输出用队列回传主线程再写进 Text，避免跨线程操作 Tk 组件。
"""

import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import paths as P

# Windows：不弹出控制台黑框
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

TITLE = "Light-Novel 书库控制面板"


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

        self.title(TITLE)
        self.geometry("860x620")
        self.minsize(720, 520)

        self._build_status()
        self._build_actions()
        self._build_log()

        self.after(120, self._drain)
        self.after(400, self._refresh)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log("面板已就绪。服务与监控以独立进程运行，关闭本窗口不会停止它们。")

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
        self.log = tk.Text(wrap, height=14, wrap="none", background="#fbfbfb",
                           foreground="#202020", insertbackground="#202020",
                           font=("Consolas", 9), relief="solid", borderwidth=1)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.configure(state="disabled")

        self.status = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status, anchor="w", foreground="#444444").pack(
            fill="x", padx=12, pady=(0, 8))

    # ------------------------------------------------------------ 日志
    def _log(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", "[%s] %s\n" % (time.strftime("%H:%M:%S"), text))
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _drain(self):
        try:
            while True:
                kind, text = self.q.get_nowait()
                self._log(text)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _pump(self, proc, tag):
        def reader():
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        self.q.put(("out", "%s | %s" % (tag, line)))
            except Exception:
                pass
            rc = proc.poll()
            self.q.put(("out", "%s 进程结束（退出码 %s）" % (tag, rc)))
        threading.Thread(target=reader, daemon=True).start()

    # ------------------------------------------------------------ 子进程
    def _run(self, tag, args, console=False, track=None):
        if track and track in self.procs and self.procs[track].poll() is None:
            self._log("%s 已在运行。" % tag)
            return None
        cmd = _spawn_args(args)
        flags = _NEW_CONSOLE if console else _NO_WINDOW
        self._log("$ %s" % " ".join(args))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=flags, cwd=P.TARGET_DIR)
        except Exception as exc:
            self._log("启动失败：%s" % exc)
            messagebox.showerror(TITLE, "启动失败：\n%s" % exc)
            return None
        if track:
            self.procs[track] = proc
        self._pump(proc, tag)
        return proc

    def _start_opds(self):
        self._run("OPDS", ["opds", "--port", str(self.port)], track="opds")
        self.status.set("OPDS 服务启动中…")
        self.after(1200, self._refresh)

    def _start_monitor(self):
        self._run("监控", ["sync"], track="monitor")
        self.status.set("目录监控启动中…")
        self.after(1200, self._refresh)

    def _run_once(self, tag, *args):
        proc = self._run(tag, list(args))
        if proc is None:
            return
        self.status.set("%s 执行中…" % tag)

        def waiter():
            proc.wait()
            self.q.put(("out", "%s 完成（退出码 %s）" % (tag, proc.returncode)))
        threading.Thread(target=waiter, daemon=True).start()

    def _tunnel_wizard(self):
        self._log("隧道向导需要交互输入，将在独立控制台窗口中打开。")
        self._run("隧道向导", ["tunnel-setup"], console=True)

    def _stop(self, key):
        proc = self.procs.get(key)
        if proc and proc.poll() is None:
            self._log("正在停止 %s（PID %s）…" % (key, proc.pid))
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
                self._log("已强制结束 %s" % owners)
                self.after(600, self._refresh)
            return
        messagebox.showinfo(TITLE, "该服务不是由本面板启动，未做处理。")

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

        pid = self._monitor_pid()
        if pid:
            self._set("monitor", True, "运行中（PID %d）" % pid)
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
        """读锁文件并确认那个 PID 还活着（只读探测，不会杀进程）。"""
        try:
            from .sync.monitor import _pid_alive
        except Exception:
            return None
        try:
            with open(P.LOCK_FILE, "r", encoding="utf-8") as f:
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
            self._log("当前平台不支持直接打开目录：%s" % path)

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
