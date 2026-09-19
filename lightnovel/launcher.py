#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线启动器：把 OPDS 服务 + Cloudflare 固定域名隧道拉起来，并**常驻守护**。

它解决的是这一串问题：服务要能被 https://ranqing.ccwu.cc/ 访问、断线要能自愈、
**关掉启动器窗口之后一切照跑**。

运行机制
--------
```
launch_online.bat（一个普通控制台窗口，随时可关）
  └─ python -m lightnovel launch          ← 只做编排：停旧 → 起守护 → 等就绪 → 打印 → 退出
       └─ 守护进程（pythonw + DETACHED_PROCESS，无窗口、无控制台）
            ├─ OPDS 服务（8080，独立进程）
            │    └─ cloudflared（OPDS 的子进程，4 条 HA 连接连同一个隧道）
            └─ 巡检循环：每 N 秒查「本地是否 200 + 隧道进程是否活着」，异常自动整栈重启
```

后台驻留方式
------------
守护进程用 **pythonw.exe**（GUI 子系统，Windows 不分配控制台）加上
**DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP（并尝试脱离 Job）** 启动，输出重定向到
文件而不是管道（管道会随父进程退出而断）。所以前台的 bat 窗口、乃至它的父 shell
被关掉、被 Ctrl+C，都与守护进程和服务无关。守护进程的 PID 落在
``.autosync/launcher-<port>.pid``，运行状态落在 ``.autosync/launcher-<port>.json``。

连接保持策略（三层，各管一段）
------------------------------
1. **隧道层** —— cloudflared 自己维持 4 条到 Cloudflare 边缘的 HA 连接，断线自动重连
   （``Registered tunnel connection`` / ``Connection terminated`` 都能在 opds.log 里看到）。
   这一层不用我们操心，但**进程死了不会自己复活**，所以有第 2 层。
2. **进程层** —— 守护循环每 ``interval`` 秒查三件事：本地 HTTP 是否真的返回 200、
   隧道进程是否还在、**隧道的 metrics ``/ready`` 是否还报着有连接**。任一不满足就整栈
   重启（先停后起），失败次数越多退避越久（30s → 上限 5 分钟），免得在真正的故障里疯狂重启。
   第三项是必需的：cloudflared 会陷入「进程活着、连接全断」的状态（QUIC 被线路丢包时
   它只在 QUIC 上重试、不会退回 http2），只查进程的守护会一直判定健康，公网 530 却永远
   不自愈 —— 这正是 2026-09-18 实测踩到的坑。
3. **业务层** —— 用真实的 HTTP 请求判活，而不是"端口开着就当没事"：端口能连上但进程
   卡死的情况它也能发现。

关闭界面后的进程管理
--------------------
* 启动器窗口只承载**前台编排进程**；编排做完就退出，进程树上它没有留下的东西。
* 守护进程与服务都是**独立进程**，父进程是系统而不是那个窗口 —— 关窗口 = 关一个
  什么都不管的 print 程序。
* 停止服务要用 ``python -m lightnovel stop`` 或 ``launchers\\stop_opds.bat``：它们会
  **先停守护、再停服务**（顺序很重要，反了的话守护下一次巡检会把服务又拉起来）。
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time

from . import paths as P
from . import service as SVC
from .sync.monitor import _pid_alive

log = logging.getLogger("launcher")

STATE_VERSION = 1
# 巡检到"不健康"时的退避上限：真故障时别把 CPU 和硬盘日志刷爆
MAX_BACKOFF = 300.0
# Windows：taskkill 等子进程不弹黑框
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------- 路径 ----------------------------
def pid_file(port=None):
    return os.path.join(P.LOG_DIR, "launcher-%d.pid" % int(port or P.PORT))


def state_file(port=None):
    return os.path.join(P.LOG_DIR, "launcher-%d.json" % int(port or P.PORT))


def log_file():
    """守护进程自己的巡检日志（前台窗口关掉之后，这里是唯一能看的地方）。"""
    return os.path.join(P.LOG_DIR, "launcher.log")


def stdout_file():
    """守护进程的 stdout / stderr（捕捉没被 catch 的异常栈）。"""
    return os.path.join(P.LOG_DIR, "launcher-stdout.log")


# ---------------------------- 守护进程的记账 ----------------------------
def read_daemon_pid(port=None):
    try:
        with open(pid_file(port), "r", encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def write_daemon_pid(pid, port=None):
    try:
        os.makedirs(P.LOG_DIR, exist_ok=True)
        with open(pid_file(port), "w", encoding="utf-8") as f:
            f.write(str(int(pid)))
    except OSError:
        pass


def clear_daemon_pid(port=None):
    try:
        os.remove(pid_file(port))
    except OSError:
        pass


def daemon_alive(port=None):
    pid = read_daemon_pid(port)
    return pid if pid and _pid_alive(pid) else 0


def write_state(port=None, **fields):
    """原子写状态文件（临时文件 + os.replace），供 status / 人工查看。"""
    data = {"version": STATE_VERSION,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "port": int(port or P.PORT)}
    data.update(fields)
    path = state_file(port)
    try:
        os.makedirs(P.LOG_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        pass
    return data


def read_state(port=None):
    try:
        with open(state_file(port), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------- 探测 ----------------------------
def check_once(port, tunnel=None):
    """一次巡检：本地服务、隧道进程、**隧道连接** —— 三层各查一遍。"""
    code = SVC.http_probe(port, timeout=6)
    cf = SVC.cloudflared_running() if tunnel else True
    # 连接健康度只在进程活着时才有意义；探测不到（None）不算故障 —— cloudflared
    # 刚起来的那几秒 metrics 端口还没监听，当成故障会「起了就杀」。
    tun = SVC.tunnel_ready() if (tunnel and cf) else None
    return {"http": code,
            "local_ok": code == 200,
            "cf": cf,
            "tunnel_ok": tun,
            "tunnel_broken": tun is False,
            "at": time.time()}


def public_probe(host, timeout=10):
    """从本机访问公网域名的一次尝试（**仅作提示，不作为判活依据**）。

    实测本机直连 Cloudflare 常被重置（curl 35 / WinError 10054，走代理才通），
    所以失败不代表隧道断了 —— 判活靠"本地 200 + 隧道进程 + 隧道已连接"。
    """
    import urllib.request
    try:
        with urllib.request.urlopen("https://%s/opds/stats" % host, timeout=timeout) as r:
            return r.status
    except Exception:
        return 0


def wait_local(port, timeout=60.0):
    """等本地服务返回 200（端口开着 ≠ 服务真能用）。"""
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        if SVC.http_probe(port, timeout=4) == 200:
            return True
        time.sleep(0.6)
    return False


# ---------------------------- 守护进程 ----------------------------
def _setup_daemon_logging():
    log.setLevel(logging.INFO)
    if not log.handlers:
        fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        try:
            fh = logging.FileHandler(log_file(), encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError:
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def rebuild(port, tunnel, drop_tunnel=False):
    """整栈重启：停掉旧服务 → 起新的（带上隧道）→ 等就绪。返回是否成功。

    ``drop_tunnel``：先把**连不上边缘**的那份 cloudflared 清掉。必须这样，否则
    ``spawn`` 看到「cloudflared 已在跑」会复用它 —— 而那份的连接是断的，重建就
    成了空转，守护会陷入「发现不健康 → 重建 → 还是不健康」的死循环。
    """
    SVC.stop(port)
    if drop_tunnel:
        n = SVC.kill_cloudflared()
        if n:
            log.warning("已清掉 %d 个连不上边缘的 cloudflared，重建时会起新的", n)
    offset = SVC.log_size()
    pid, _logp, reused = SVC.spawn(port, tunnel=tunnel)
    log.info("已重新拉起服务：PID %s%s", pid, "（隧道进程复用已在跑的那份）" if reused else "")
    if not SVC.wait_ready(port, 45):
        log.error("重新拉起后服务仍未监听端口；日志尾部：\n%s", SVC.log_tail())
        return False, pid, reused
    if tunnel and not reused and SVC.wait_tunnel(offset, 45) != "up":
        # 隧道没连上不算致命：本地还能用，隧道会自己重试；记下来，下一轮巡检再看
        log.warning("隧道暂未连接（cloudflared 会自己重连），继续巡检")
    return True, pid, reused


def daemon_loop(port, tunnel, interval=30.0):
    """守护主循环：巡检 → 异常则整栈重启 → 退避。此函数**不返回**。"""
    _setup_daemon_logging()
    me = os.getpid()
    write_daemon_pid(me, port)
    log.info("守护进程启动：PID %d ｜ 端口 %d ｜ 隧道 %s ｜ 巡检间隔 %gs ｜ 日志 %s",
             me, port, tunnel or "（无）", interval, log_file())

    fails = 0
    checks = 0
    restarts = 0
    last_error = ""
    first = True
    while True:
        try:
            st = check_once(port, tunnel)
            checks += 1
            # 「进程活着」不等于「隧道能通」：QUIC 被线路丢包时 cloudflared 会
            # 一直活着却零连接（公网 530/502），只看进程的守护永远不修。
            healthy = st["local_ok"] and st["cf"] and not st["tunnel_broken"]
            if not healthy:
                fails += 1
                last_error = "本地 HTTP=%s，隧道进程=%s，隧道连接=%s" % (
                    st["http"], st["cf"],
                    "断" if st["tunnel_broken"] else ("好" if st["tunnel_ok"] else "未知"))
                if first:
                    # 首轮必然要拉一次：前台编排刚刚把旧服务停掉。这是**预期动作**，
                    # 不该和后面的真异常混在一起报警，否则日志第一行永远是 WARNING。
                    log.info("首轮拉起服务（本地 HTTP=%s，隧道进程=%s）", st["http"], st["cf"])
                else:
                    log.warning("巡检发现异常（连续第 %d 次）：%s → 重新拉起服务",
                                fails, last_error)
                ok, spid, reused = rebuild(port, tunnel, drop_tunnel=bool(st["tunnel_broken"]))
                if ok:
                    restarts += 1
                    fails = 0
                    last_error = ""
            else:
                fails = 0
                last_error = ""
                write_state(port, daemon_pid=me, tunnel=tunnel, interval=interval,
                            service_pid=SVC.read_pid(port), healthy=True,
                            http=st["http"], cloudflared=st["cf"],
                            checks=checks, restarts=restarts, last_error="")
        except Exception:                       # noqa: BLE001 —— 守护循环绝不能死
            log.exception("巡检循环出现异常（继续运行）")
            last_error = "巡检异常（见 launcher.log）"
            fails += 1
        first = False

        if fails:
            write_state(port, daemon_pid=me, tunnel=tunnel, interval=interval,
                        service_pid=SVC.read_pid(port), healthy=False,
                        checks=checks, restarts=restarts, last_error=last_error)
        # 健康 → 按固定间隔；不健康 → 退避，别在真故障里疯狂重启
        time.sleep(min(MAX_BACKOFF, float(interval) * (1 if not fails else min(fails + 1, 10))))


def start_daemon(port, tunnel, interval):
    """以「无窗口后台」方式启动守护进程 → 返回它的 PID。"""
    stop_daemon(port)                      # 幂等：同一端口只留一个守护
    # --tunnel 必须**显式**传，包括 none：守护进程是重新拉起的子进程，漏传就会落回
    # argparse 的默认值 named —— 于是"只跑局域网"的启动会变成"等一个根本不存在的隧道"，
    # 巡检判定失真（实测踩到过：日志里写着「隧道 named」而调用方传的是 none）。
    argv = SVC.entry_args() + ["launch", "--daemon",
                               "--port", str(int(port)), "--interval", str(float(interval)),
                               "--tunnel", tunnel or "none"]
    exe = SVC.gui_python()
    os.makedirs(P.LOG_DIR, exist_ok=True)
    out = open(stdout_file(), "ab", buffering=0)
    try:
        out.write(("\n===== %s 守护进程启动：%s %s =====\n"
                   % (time.strftime("%Y-%m-%d %H:%M:%S"), exe, " ".join(argv))).encode("utf-8"))
        kwargs = dict(cwd=P.TARGET_DIR, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = SVC.detach_flags()
        try:
            proc = subprocess.Popen([exe] + argv, **kwargs)
        except OSError:
            if sys.platform != "win32":
                raise
            kwargs["creationflags"] = SVC.detach_flags(with_breakaway=False)
            proc = subprocess.Popen([exe] + argv, **kwargs)
    finally:
        out.close()
    write_daemon_pid(proc.pid, port)
    return proc.pid


def stop_daemon(port=None, grace=6.0):
    """停掉守护进程（必须先于停服务，否则它下一次巡检会把服务又拉起来）。

    只结束 PID 文件里那个进程，并且校验它确实是我们的解释器，不做范围横扫。
    """
    pid = read_daemon_pid(port)
    killed = 0
    if pid and _pid_alive(pid) and SVC.is_ours(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, creationflags=_NO_WINDOW)
        killed = pid
    clear_daemon_pid(port)
    if killed:
        deadline = time.time() + grace
        while time.time() < deadline and _pid_alive(killed):
            time.sleep(0.3)
    return killed


# ---------------------------- CLI ----------------------------
def _tunnel_host():
    try:
        from .opds.server import read_named_hostname
        return read_named_hostname() or ""
    except Exception:
        return ""


def main_launch(argv=None):
    ap = argparse.ArgumentParser(
        prog="lightnovel launch",
        description="启动 OPDS 服务 + 固定域名隧道，并把它交给一个后台守护进程常驻；"
                    "本窗口关掉（× 或 Ctrl+C）不影响它们。")
    ap.add_argument("--port", type=int, default=P.PORT, help="监听端口（默认 %d）" % P.PORT)
    ap.add_argument("--tunnel", choices=["named", "cloudflared", "none"], default="named",
                    help="公网隧道类型；none = 只跑局域网（默认 named = 固定域名）")
    ap.add_argument("--interval", type=float, default=30.0, help="守护巡检间隔秒数（默认 30）")
    ap.add_argument("--timeout", type=float, default=60.0, help="等待就绪的秒数（默认 60）")
    ap.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    tunnel = None if a.tunnel == "none" else a.tunnel
    if a.daemon:                      # 被 start_daemon 拉起的那个隐藏入口
        return daemon_loop(a.port, tunnel, a.interval)

    host = _tunnel_host() if tunnel == "named" else ""
    print("Light-Novel 在线启动器")
    print("  端口 %d ｜ 隧道 %s ｜ 巡检间隔 %.0fs" % (a.port, tunnel or "无", a.interval))

    old = daemon_alive(a.port)
    if old:
        print("  [1/5] 发现旧守护进程 PID %d，先停掉它" % old)
    else:
        print("  [1/5] 没有正在运行的守护进程")
    SVC.stop(a.port)                  # 顺带把旧服务停干净，交给守护重新拉起

    dpid = start_daemon(a.port, tunnel, a.interval)
    print("  [2/5] 守护进程已启动：PID %d（pythonw + DETACHED_PROCESS，无窗口后台）" % dpid)

    if not wait_local(a.port, a.timeout):
        print("  ✗ [3/5] 等了 %.0f 秒本地服务仍不可用，下面是日志尾部：" % a.timeout)
        tail = SVC.log_tail()
        print("\n".join("      " + ln for ln in tail.splitlines()) if tail else "      （日志为空）")
        print("  详细日志：%s" % log_file())
        return 1
    print("  [3/5] 本地服务已就绪：http://127.0.0.1:%d/opds/stats → 200" % a.port)

    if tunnel:
        print("  [4/5] 等待隧道建连（cloudflared 实测要 5~30 秒）…")
        deadline = time.time() + max(0.0, a.timeout - 10)
        state = "timeout"
        while time.time() < deadline:
            st = read_state(a.port)
            if st.get("healthy"):
                state = "up"
                break
            time.sleep(1.0)
        if state == "up":
            print("        隧道已连接%s" % ("  https://%s/" % host if host else ""))
        else:
            print("        还没看到「已连接」；cloudflared 会自己继续重连（详情见 %s）"
                  % P.OPDS_LOG_FILE)
    else:
        print("  [4/5] 未启用隧道（--tunnel none），仅局域网可用")

    code = public_probe(host) if host else 0
    if not host:
        print("  [5/5] 后台驻留中：守护进程每 %.0f 秒巡检一次，本地服务挂了会自动拉起。" % a.interval)
    elif code == 200:
        print("  [5/5] 公网自检：✓ https://%s/ 返回 200" % host)
    else:
        print("  [5/5] 公网自检未通过 —— 注意这**多半是本机网络环境**（实测本机直连 Cloudflare")
        print("        常被 RST，走代理才通），不代表隧道断了；用手机或代理再验证一次。")

    print("")
    print("  局域网： http://%s:%d/" % (_lan_ip(), a.port))
    if host:
        print("  公网：   https://%s/   ← 建连期间访问会看到 Cloudflare 1033，稍等即可" % host)
    print("  守护：   PID %d（状态 %s）" % (dpid, state_file(a.port)))
    print("  日志：   %s" % log_file())
    print("  服务：   %s" % P.OPDS_LOG_FILE)
    print("")
    print("  现在可以放心关掉本窗口（× 或 Ctrl+C）：启动器只是把活儿交给后台守护进程。")
    print("  停止：   python -m lightnovel stop   或   launchers\\stop_opds.bat")
    return 0


def _lan_ip():
    try:
        from .opds.server import local_ip
        return local_ip()
    except Exception:
        return "127.0.0.1"


def main_status(argv=None):
    ap = argparse.ArgumentParser(prog="lightnovel launch-status",
                                 description="查看在线启动器 / 守护进程 / 服务 / 隧道的状态")
    ap.add_argument("--port", type=int, default=P.PORT)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    dpid = read_daemon_pid(a.port)
    alive = bool(dpid and _pid_alive(dpid))
    st = read_state(a.port)
    print("端口            %d" % a.port)
    print("守护进程        %s" % ("PID %d（运行中）" % dpid if alive else "未运行"))
    print("本地服务        %s" % ("HTTP %s" % SVC.http_probe(a.port) if SVC.port_open(a.port) else "未监听"))
    if st.get("tunnel") not in (None, "", "none"):
        print("隧道进程        %s" % ("在运行" if SVC.cloudflared_running() else "未运行"))
        _tun = SVC.tunnel_ready()
        print("隧道连接        %s" % ("已连接" if _tun else (
            "**零连接**（进程活着但没连上边缘，下一轮巡检会重建）" if _tun is False
            else "未知（metrics 端口未监听，可能是刚启动）")))
    print("PID 文件        %s" % pid_file(a.port))
    print("状态文件        %s" % state_file(a.port))
    if st:
        print("最近一次巡检    %s ｜ 健康=%s ｜ 累计巡检 %s 次 ｜ 自动重启 %s 次%s"
              % (st.get("updated", "?"), st.get("healthy", "?"),
                 st.get("checks", "?"), st.get("restarts", "?"),
                 (" ｜ 最近错误：%s" % st["last_error"]) if st.get("last_error") else ""))
    print("巡检日志        %s" % log_file())
    return 0


def main_stop(argv=None):
    ap = argparse.ArgumentParser(prog="lightnovel stop",
                                 description="停止在线启动器守护进程与 OPDS 服务")
    ap.add_argument("--port", type=int, default=P.PORT)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    d = stop_daemon(a.port)
    if d:
        print("已停止守护进程：PID %d（不会再自动拉起服务）" % d)
    else:
        print("守护进程未在运行。")
    r = SVC.stop(a.port)
    if r["killed"]:
        print("已停止 OPDS 服务：PID %s" % ", ".join(str(p) for p in r["killed"]))
    elif r["skipped"]:
        print("端口 %d 被非本服务的进程占用（PID %s），未处理。"
              % (a.port, ", ".join(str(p) for p in r["skipped"])))
        return 1
    else:
        print("OPDS 服务当前没有在运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main_launch())
