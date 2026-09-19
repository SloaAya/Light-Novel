#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 OPDS 服务从当前控制台**摘出去**再启动 / 重启 —— 窗口关了服务照跑。

要解决的问题
------------
Windows 上进程和控制台是绑定的：控制台窗口被关掉（点右上角 ×）或按 Ctrl+C 时，
系统会给**那个控制台里的所有进程**发关闭/中断事件，服务跟着一起消失。前台跑
``python -m lightnovel opds``（现在只剩 ``launchers/run_opds.bat`` 这一个双击入口）
就是这种情况 —— 看起来像「服务自己暂停了」，其实是窗口没了，进程被系统顺手带走了。

解绑用两条彼此独立的措施（任一成立就够，两条都给上）：

1. **pythonw.exe**（GUI 子系统）—— 它压根不分配控制台，没有窗口可关；
2. **DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP（+ BREAKAWAY_FROM_JOB）** ——
   即使只能用 ``python.exe`` / 打包后的 exe，也在 ``CreateProcess`` 时声明
   「不要父控制台」，并尝试从可能存在的 Job 对象里脱离（某些终端把子进程
   放进 kill-on-close 的 Job 里，那才是「关掉窗口全都没了」的真正元凶）。

所以本脚本所在的窗口退出、被 ×、被 Ctrl+C，都与服务无关。

两个细节
--------
* 输出**必须**重定向到文件，不能用 ``subprocess.PIPE``：管道会随父进程退出而断，
  服务下一次写日志就是 ``BrokenPipeError``。
* 停止服务时只认「PID 文件里的进程」与「正在监听该端口的进程」，并校验进程名 ——
  不做任何范围的横扫（``stop_opds.bat`` 那种按端口 taskkill 的写法在换端口后就失效了）。
"""
import argparse
import os
import socket
import subprocess
import sys
import time

from . import paths as P
from .sync.monitor import _pid_alive

# ---------------------------- 常量 ----------------------------
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# 可能是「我们的」服务的进程名（停止时据此确认，避免误杀 PID 复用后的无关进程）
_OUR_IMAGE_NAMES = ("python.exe", "pythonw.exe", "lightnovel.exe")

_STDOUT_LOG = os.path.join(P.LOG_DIR, "opds-stdout.log")


def pid_file(port=None):
    """后台实例的 PID 文件（**按端口分开**：临时实例不会踩掉正式实例的记录）。

    路径由 ``LOG_DIR`` 现算，测试换目录时也跟得上。
    """
    return os.path.join(P.LOG_DIR, "opds-%d.pid" % int(port or P.PORT))


def stdout_log():
    return _STDOUT_LOG


# ---------------------------- 小工具 ----------------------------
def _no_window():
    return _CREATE_NO_WINDOW if sys.platform == "win32" else 0


def detach_flags(with_breakaway=True):
    """脱离控制台所需的创建标志（非 Windows 交给 ``start_new_session``）。"""
    if sys.platform != "win32":
        return 0
    flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    if with_breakaway:
        flags |= _CREATE_BREAKAWAY_FROM_JOB
    return flags


def gui_python():
    """优先 ``pythonw.exe``（GUI 子系统 = 没有控制台）；打包 exe 时返回 exe 自身。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    exe = sys.executable or "python"
    name = "pythonw.exe" if sys.platform == "win32" else "pythonw"
    cand = os.path.join(os.path.dirname(exe), name)
    return cand if os.path.isfile(cand) else exe


def entry_args():
    """源码运行要在前面补 ``-m lightnovel``；exe 直接把自己的参数透传。"""
    return [] if getattr(sys, "frozen", False) else ["-m", "lightnovel"]


def port_open(port, host="127.0.0.1", timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def port_owners(port):
    """谁在 LISTENING 这个端口 → ``["1234", ...]``（只读查询）。"""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=_no_window(), timeout=10)
    except Exception:
        return []
    want = ":%d" % int(port)
    pids = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(want) \
                and parts[3].upper() == "LISTENING" and parts[4] not in pids:
            pids.append(parts[4])
    return pids


def image_name(pid):
    """进程映像名（小写）；查不到返回空串。"""
    try:
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=_no_window(), timeout=10)
    except Exception:
        return ""
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith('"'):
            return line.split('","')[0].strip('"').lower()
    return ""


def is_ours(pid):
    """这个 PID 看起来是我们的服务吗（防 PID 复用后误杀无关程序）。"""
    return image_name(pid) in _OUR_IMAGE_NAMES


# ---------------------------- PID 文件 ----------------------------
def read_pid(port=None):
    try:
        with open(pid_file(port), "r", encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def write_pid(pid, port=None):
    try:
        os.makedirs(P.LOG_DIR, exist_ok=True)
        with open(pid_file(port), "w", encoding="utf-8") as f:
            f.write(str(int(pid)))
    except OSError:
        pass


def clear_pid(port=None):
    try:
        os.remove(pid_file(port))
    except OSError:
        pass


def service_pids(port):
    """属于本服务的 PID（升序）：PID 文件里那个 + 正在监听端口的那些。"""
    pids = set()
    pid = read_pid(port)
    if pid and _pid_alive(pid):
        pids.add(pid)
    for s in port_owners(port):
        try:
            pids.add(int(s))
        except ValueError:
            pass
    return sorted(pids)


def status(port=None):
    """只读状态：``{pid, alive, listening, pids}``。"""
    port = int(port or P.PORT)
    pid = read_pid(port)
    return {"pid": pid,
            "alive": bool(pid and _pid_alive(pid)),
            "listening": port_open(port),
            "pids": service_pids(port),
            "port": port,
            "pidfile": pid_file(port),
            "log": stdout_log()}


# ---------------------------- 停止 / 启动 ----------------------------
def stop(port=None, grace=10.0):
    """停掉后台实例，返回被结束的 PID 列表（空 = 本来就没在跑）。"""
    port = int(port or P.PORT)
    killed, skipped = [], []
    for pid in service_pids(port):
        if pid in (0, os.getpid()):
            continue
        if not is_ours(pid):
            skipped.append(pid)           # 不是我们的进程：绝不碰
            continue
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, creationflags=_no_window())
        killed.append(pid)
    deadline = time.time() + grace
    while time.time() < deadline and port_open(port):
        time.sleep(0.3)
    if not port_open(port):
        clear_pid(port)
    return {"killed": killed, "skipped": skipped, "port_free": not port_open(port)}


def spawn(port=None, bind=None, tunnel=None, extra=()):
    """以「脱离控制台」的方式拉起服务 → ``(pid, 日志路径, 隧道是否复用)``。

    ``tunnel`` 指定时，若 cloudflared **已经在跑**就不再起第二份：同一个 named
    tunnel 挂两套连接，Cloudflare 只会在两份之间来回挑，轻则连接抖动、重则 502。
    复用已有的那份即可 —— 它的 ingress 本来就指向 127.0.0.1:<port>。
    """
    port = int(port or P.PORT)
    reused = bool(tunnel) and cloudflared_running()
    if reused:
        tunnel = None
    os.makedirs(P.LOG_DIR, exist_ok=True)
    argv = entry_args() + ["opds", "--port", str(port), "--no-qr"]
    if bind:
        argv += ["--bind", str(bind)]
    if tunnel:
        argv += ["--tunnel", str(tunnel)]
    argv += list(extra)

    exe = gui_python()
    log = open(stdout_log(), "ab", buffering=0)
    try:
        log.write(("\n===== %s 启动：%s %s =====\n"
                   % (time.strftime("%Y-%m-%d %H:%M:%S"), exe, " ".join(argv))).encode("utf-8"))
        kwargs = dict(cwd=P.TARGET_DIR, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = detach_flags()
        try:
            proc = subprocess.Popen([exe] + argv, **kwargs)
        except OSError:
            # 所在 Job 不允许 breakaway 时会 ERROR_ACCESS_DENIED：去掉该位重试
            if sys.platform != "win32":
                raise
            kwargs["creationflags"] = detach_flags(with_breakaway=False)
            proc = subprocess.Popen([exe] + argv, **kwargs)
    finally:
        log.close()          # 只关父进程这一份；子进程自己那份继续用
    write_pid(proc.pid, port)
    return proc.pid, stdout_log(), reused


def wait_ready(port, timeout=45.0):
    """等端口真的起来（服务要先扫一遍书库，冷启动几秒很正常）。"""
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.4)
    return False


def log_tail(lines=14, path=None):
    """启动失败时把日志尾巴端出来 —— 不然「没起来」没法排查。"""
    path = path or stdout_log()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read().splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])


def http_probe(port, path="/opds/stats", timeout=6):
    """真发一个 HTTP 请求（端口开着 ≠ 服务真能用）。返回状态码，失败返回 0。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (int(port), path), timeout=timeout) as r:
            return r.status
    except Exception:
        return 0


def log_size(path=None):
    """标准输出日志当前大小 —— 启动前记下来，之后只读新增的那一段。"""
    try:
        return os.path.getsize(path or stdout_log())
    except OSError:
        return 0


def read_log_from(offset, path=None):
    """读 ``offset`` 之后新写入的日志内容。"""
    try:
        with open(path or stdout_log(), "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def wait_tunnel(offset, timeout=45.0):
    """等隧道连上：只看**本次启动新写入**的那段日志（``offset`` 之后）。

    返回 ``"up"`` / ``"failed"`` / ``"timeout"``。

    为什么要等：cloudflared 建连通常要 5~30 秒（实测 22 秒）。之前这里是无条件报
    「公网隧道已拉起」，用户点完立刻刷公网地址，看到的是 Cloudflare **Error 1033**
    （域名挂在隧道上但还没有在线连接），于是以为整个脚本失败了。
    """
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        txt = read_log_from(offset)
        if "named tunnel 已连接" in txt:
            return "up"
        if ("named tunnel 启动后立刻退出" in txt
                or "未找到 cloudflared" in txt
                or ("未在" in txt and "中找到 hostname" in txt)):
            return "failed"
        time.sleep(1.0)
    return "timeout"


def _tunnel_url():
    """config.yml 里配的公网地址（读不到就空串，只用于打印）。"""
    try:
        from .opds.server import read_named_hostname
        host = read_named_hostname()
        return "https://%s/" % host if host else ""
    except Exception:
        return ""


def restart(port=None, bind=None, tunnel=None, timeout=45.0):
    """停旧 → 起新（后台）→ 等就绪 → HTTP 探活。返回结果字典。"""
    port = int(port or P.PORT)
    result = {"port": port, "stopped": [], "skipped": [], "pid": 0,
              "ready": False, "http": 0, "log": stdout_log(), "tail": "",
              "tunnel_reused": False}
    st = stop(port)
    result["stopped"], result["skipped"] = st["killed"], st["skipped"]
    pid, logp, reused = spawn(port, bind=bind, tunnel=tunnel)
    result["pid"], result["log"], result["tunnel_reused"] = pid, logp, reused
    result["ready"] = wait_ready(port, timeout)
    if result["ready"]:
        result["http"] = http_probe(port)
    else:
        result["tail"] = log_tail()
    return result


# ---------------------------- CLI ----------------------------
def _lan_url(port):
    try:
        from .opds.server import local_ip
        return "http://%s:%d/" % (local_ip(), int(port))
    except Exception:
        return "http://127.0.0.1:%d/" % int(port)


def cloudflared_pids():
    """列出所有 cloudflared 进程的 PID（没有则空列表）。"""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_no_window(), timeout=10)
    except Exception:
        return []
    pids = []
    for line in (r.stdout or "").splitlines():
        cols = [c.strip().strip('"') for c in line.split(",")]
        if len(cols) >= 2 and cols[0].lower() == "cloudflared.exe":
            try:
                pids.append(int(cols[1]))
            except ValueError:
                pass
    return pids


def kill_cloudflared():
    """杀掉所有 cloudflared 进程，返回杀掉的个数。

    为什么必须能杀：隧道会遇到「进程活着、连接全断」的状态（QUIC 被线路丢包）。
    这时若直接重建，``spawn`` 会按「cloudflared 已在跑」把它**复用**回来，重建
    成了空转 —— 守护于是陷入「发现不健康 → 重建 → 还是不健康」的死循环。
    先清掉旧的，新的一份才有机会用 http2 重新连上。
    """
    n = 0
    for pid in cloudflared_pids():
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, creationflags=_no_window())
        n += 1
    return n


def cloudflared_running():
    """cloudflared 是否在跑（只读查询）。用它决定要不要再起一份隧道。"""
    return image_name_by_name("cloudflared.exe") == "cloudflared.exe"


def tunnel_ready(timeout=2.0):
    """隧道**连接**是否健康 —— 三态，供守护判活。

    ``True``  metrics 在线且有活动连接（``/ready`` 返回 200）
    ``False`` metrics 在线但**零连接** —— 隧道断了，进程却还活着
    ``None``  探测不到（端口没监听 / 不是 cloudflared 的 metrics）→ 状态未知

    ``None`` 与 ``False`` 必须分开：cloudflared 刚启动时 metrics 端口还没监听，
    那一刻若判「不健康」，守护会把刚拉起的隧道立刻杀掉，陷入「起了就杀」的
    死循环。只有**明确**收到 503 才算断。

    为什么要这一层：QUIC 被线路丢包时，cloudflared 会一直活着、却一条连接都
    注册不上（公网 530 / 502），此时「进程在不在」这个判据完全看不出问题。
    """
    try:
        from .opds import server as S
        addr = S.tunnel_metrics_addr()
    except Exception:                       # 导入失败就当未知，绝不因此判死
        return None
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen("http://%s/ready" % addr, timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except urllib.error.HTTPError as exc:
        return exc.code == 200              # 503 = 没有活动连接
    except Exception:                       # 连接被拒 / 超时 → 未知，不当作故障
        return None


def image_name_by_name(exe):
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + exe],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=_no_window(), timeout=10)
    except Exception:
        return ""
    return exe if exe.lower() in (r.stdout or "").lower() else ""


def main_restart(argv=None):
    ap = argparse.ArgumentParser(
        prog="lightnovel restart",
        description="重启 OPDS 服务：新实例以「脱离控制台」的方式后台常驻，"
                    "本窗口关掉（× 或 Ctrl+C）也不影响它。")
    ap.add_argument("--port", type=int, default=P.PORT, help="监听端口（默认 %d）" % P.PORT)
    ap.add_argument("--bind", default=None, help="监听地址（默认沿用服务默认 0.0.0.0）")
    ap.add_argument("--tunnel", choices=["named", "cloudflared"], default=None,
                    help="顺带拉起公网隧道；默认**不动**已在运行的隧道（重启期间公网地址不变）")
    ap.add_argument("--timeout", type=float, default=45.0, help="等待就绪的秒数（默认 45）")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    print("重启 OPDS 服务（后台常驻）")
    print("  端口 %d ｜ 脱离控制台方式：%s" % (a.port, gui_python()))

    st = stop(a.port)
    if st["killed"]:
        print("  [1/4] 已停止旧实例：PID %s" % ", ".join(str(p) for p in st["killed"]))
    elif st["skipped"]:
        print("  [1/4] 端口被非本服务的进程占用，未处理：PID %s" % ", ".join(str(p) for p in st["skipped"]))
    else:
        print("  [1/4] 没有正在运行的旧实例")

    if not st["port_free"]:
        print("  ✗ 端口 %d 仍被占用，请先处理占用者（或换 --port）。" % a.port)
        return 1

    log_off = log_size()           # 只认本次启动新写入的日志（见 wait_tunnel）
    pid, logp, reused = spawn(a.port, bind=a.bind, tunnel=a.tunnel)
    print("  [2/4] 已启动新实例：PID %d（无控制台，关窗口不会带走它）" % pid)

    t0 = time.time()
    if not wait_ready(a.port, a.timeout):
        print("  ✗ [3/4] 等了 %.0f 秒服务仍未监听端口，下面是日志尾部：" % a.timeout)
        tail = log_tail()
        print("\n".join("      " + ln for ln in tail.splitlines()) if tail else "      （日志为空）")
        print("  完整日志：%s" % logp)
        return 1
    print("  [3/4] 服务已就绪（%.1f 秒）" % (time.time() - t0))

    code = http_probe(a.port)
    print("  [4/4] 健康检查 /opds/stats → HTTP %s" % (code or "无响应"))
    print("")
    print("  局域网订阅地址： %s" % _lan_url(a.port))
    if reused:
        print("  公网隧道：      已经在跑 → 复用它（没有再起一份，免得同一隧道挂两套连接）")
    elif a.tunnel:
        print("  公网隧道：      正在建连（cloudflared 实测要 5~30 秒）…")
        tun = wait_tunnel(log_off, 45)
        if tun == "up":
            print("  公网隧道：      已连接  %s" % _tunnel_url())
        elif tun == "failed":
            print("  公网隧道：      ✗ 启动失败 —— 原因见下面的日志尾部")
            tail = log_tail()
            print("\n".join("      " + ln for ln in tail.splitlines()) if tail else "      （日志为空）")
        else:
            print("  公网隧道：      还在建连（已等 45 秒）——**先别急着刷公网地址**，"
                  "过一会儿再看；仍打不开就查 %s" % logp)
    elif cloudflared_running():
        print("  公网隧道：      已在运行（本脚本没有动它，端口一恢复公网即刻可用）")
    else:
        print("  公网隧道：      未运行；要公网就用 launchers\\launch_online.bat 启动")
    print("  日志：          %s" % P.OPDS_LOG_FILE)
    print("  标准输出：      %s" % logp)
    print("  PID：           %d（记在 %s）" % (pid, pid_file(a.port)))
    print("")
    print("  现在可以放心关掉本窗口（× 或 Ctrl+C），服务在后台继续跑。")
    print("  停止服务：      python -m lightnovel stop  或  launchers\\stop_opds.bat")
    return 0 if code else 1


def main_stop(argv=None):
    ap = argparse.ArgumentParser(prog="lightnovel stop", description="停止后台常驻的 OPDS 服务")
    ap.add_argument("--port", type=int, default=P.PORT, help="监听端口（默认 %d）" % P.PORT)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    st = stop(a.port)
    if st["killed"]:
        print("已停止 OPDS 服务：PID %s" % ", ".join(str(p) for p in st["killed"]))
    elif st["skipped"]:
        print("端口 %d 被非本服务的进程占用（PID %s），未做处理。"
              % (a.port, ", ".join(str(p) for p in st["skipped"])))
        return 1
    else:
        print("OPDS 服务当前没有在运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main_restart())
