#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPDS 服务层：HTTP Handler、服务启动、cloudflared 隧道、二维码、命令行入口。"""

import argparse
import base64
import errno
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from ..paths import (
    AUTH_PASS,
    AUTH_USER,
    BIND,
    BLANK_PNG,
    CF_CONFIG,
    COVER_CACHE_DIR,
    EPUB_MIME,
    LIGHT_NOVEL_DIR,
    LOG_DIR,
    NAMED_TUNNEL_NAME,
    OPDS_ACQ_TYPE,
    OPDS_NAV_TYPE,
    PORT,
)
from .library import (
    _ZipSink,
    _safe_relpath,
    all_vols,
    get_cover,
    get_library,
    local_ip,
    resolve_under,
)
from .feeds import (
    _accept_wants_xml,
    book_html,
    catalog_html,
    feed_book,
    feed_catalog,
    feed_recent,
    feed_root,
    feed_search,
    index_html,
    opensearch_xml,
    recent_html,
    search_html,
)

log = logging.getLogger("sync")

# ---------------------------- HTTP Handler ----------------------------
class OPDSHandler(BaseHTTPRequestHandler):
    server_version = "LN-OPDS/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        try:
            log.info("OPDS %s - %s", self.address_string(), fmt % args)
        except Exception:
            pass

    def _auth_ok(self):
        if not AUTH_USER and not AUTH_PASS:
            return True
        hdr = self.headers.get("Authorization", "") or ""
        if not hdr.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return user == AUTH_USER and pw == AUTH_PASS

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Light-Novel OPDS", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code, body, ctype, extra=None, head_only=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        extra = extra or {}
        # 同名响应头只能发一次：extra 里已提供的（如封面用的 Cache-Control）不再写默认值。
        # 否则会发出两个 Cache-Control（no-cache + max-age=…），客户端按首个取值读到
        # no-cache，期望的长缓存会静默失效，封面每次都要重新下载。
        extra_keys = {k.lower() for k in extra}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "cache-control" not in extra_keys:
            self.send_header("Cache-Control", "no-cache")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _notfound(self, msg="Not Found"):
        self._send(404, f"<h1>404</h1><p>{html.escape(msg)}</p>", "text/html; charset=utf-8")

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        if not self._auth_ok():
            self._deny()
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query or "")
        try:
            if path in ("/", "/index.html"):
                accept = self.headers.get("Accept", "") or ""
                if "atom+xml" in accept or "opds" in accept.lower():
                    self._send(200, feed_root(), f"{OPDS_NAV_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    self._send(200, index_html(), "text/html; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/opensearch.xml":
                self._send(200, opensearch_xml(), "application/opensearchdescription+xml; charset=utf-8",
                           head_only=head_only)
                return

            m = re.match(r"^/opds/catalog/(.+)$", path)
            if m:
                cat = unquote(m.group(1))
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    body = feed_catalog(cat, page)
                    if body is None:
                        self._notfound("分类不存在")
                    else:
                        self._send(200, body, f"{OPDS_NAV_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    body = catalog_html(cat, page)
                    if body is None:
                        self._notfound("分类不存在")
                    else:
                        self._send(200, body, "text/html; charset=utf-8", head_only=head_only)
                return

            m = re.match(r"^/opds/book/(.+)$", path)
            if m:
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    body = feed_book(m.group(1), page)
                    if body is None:
                        self._notfound("作品不存在")
                    else:
                        self._send(200, body, f"{OPDS_ACQ_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    body = book_html(m.group(1), page)
                    if body is None:
                        self._notfound("作品不存在")
                    else:
                        self._send(200, body, "text/html; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/recent":
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    self._send(200, feed_recent(page), f"{OPDS_ACQ_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    self._send(200, recent_html(page), "text/html; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/search":
                q = qs.get("q", [""])[0]
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    self._send(200, feed_search(q, page), f"{OPDS_ACQ_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    self._send(200, search_html(q, page), "text/html; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/refresh":
                get_library(force=True)
                self._send(200, '{"ok":true}', "application/json; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/stats":
                lib = get_library(force=True)
                data = {c: {"books": len(b), "vols": sum(len(v) for v in b.values())} for c, b in lib.items()}
                self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8",
                           head_only=head_only)
                return

            m = re.match(r"^/cover/(.+)$", path)
            if m:
                self._serve_cover(m.group(1), head_only)
                return

            m = re.match(r"^/zip/(.+)$", path)
            if m:
                self._serve_zip(m.group(1), head_only)
                return

            m = re.match(r"^/dl/(.+)$", path)
            if m:
                self._serve_file(m.group(1), head_only)
                return

            self._notfound(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("处理请求出错 %s：%s", path, exc)
            try:
                self._send(500, f"<h1>500</h1><p>{html.escape(str(exc))}</p>", "text/html; charset=utf-8")
            except Exception:
                pass

    def _serve_cover(self, enc_rel, head_only=False):
        blob, mime = get_cover(enc_rel)
        if not blob:
            self._send(200, BLANK_PNG, "image/png",
                       {"Cache-Control": "public, max-age=86400"}, head_only=head_only)
            return
        self._send(200, blob, mime, {"Cache-Control": "public, max-age=604800"}, head_only=head_only)

    def _serve_zip(self, enc_rel, head_only=False):
        """把一部作品（或一个子目录组）的所有卷流式打包成 zip 下载。
        epub 本身已是压缩格式，用 ZIP_STORED 不二次压缩，速度 = 纯读盘。

        路径格式：
          /zip/分类                 -> 整类打包（每部作品一个子目录）
          /zip/分类/书名             -> 整书打包（保留「正篇/」「番外/」子目录）
          /zip/分类/书名/子目录      -> 整组打包（zip 根目录就是 epub）"""
        rel = _safe_relpath(enc_rel)
        if not rel:
            self._notfound("路径非法")
            return
        parts = rel.split("/")
        cat = parts[0]
        book = parts[1] if len(parts) > 1 else ""
        subdir = "/".join(parts[2:]) if len(parts) > 2 else ""
        lib = get_library()
        if book:
            vols = list(lib.get(cat, {}).get(book) or [])
            if subdir:
                inner = f"{cat}/{book}/"
                vols = [v for v in vols
                        if v["rel"].startswith(inner)
                        and (v["rel"][len(inner):] == subdir
                             or v["rel"][len(inner):].startswith(subdir + "/"))]
            label = f"{book}-{subdir.split('/')[-1]}" if subdir else book
        else:
            books = lib.get(cat, {})
            vols = [v for vols_ in books.values() for v in vols_]
            label = cat
        if not vols:
            self._notfound("没有可打包的卷")
            return

        # 流式发送：无法预知总长度，用 Connection: close 结尾。
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(label + '.zip')}")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if head_only:
            return
        try:
            sink = _ZipSink(self.wfile)
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as zf:
                for v in vols:
                    disk = resolve_under(LIGHT_NOVEL_DIR, v["rel"])
                    if not disk or not os.path.isfile(disk):
                        continue
                    # 动态剥前缀：cat/ ( + book/ ) ( + subdir/ )
                    arc = v["rel"]
                    if arc.startswith(cat + "/"):
                        arc = arc[len(cat) + 1:]
                    if book and arc.startswith(book + "/"):
                        arc = arc[len(book) + 1:]
                    if subdir and arc.startswith(subdir + "/"):
                        arc = arc[len(subdir) + 1:]
                    zf.write(disk, arcname=arc)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.warning("zip 打包中断 %s：%s", rel, exc)

    def _serve_file(self, enc_rel, head_only=False):
        path = resolve_under(LIGHT_NOVEL_DIR, enc_rel)
        if not path or not os.path.isfile(path):
            self._notfound("文件不存在")
            return
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", (rng or "").strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", EPUB_MIME)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(os.path.basename(path))}")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.warning("下载中断 %s：%s", path, exc)


# ---------------------------- 服务启动 ----------------------------
def make_server(port=PORT, bind=BIND):
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        return ThreadingHTTPServer((bind, port), OPDSHandler)
    except OSError as exc:
        win_in_use = getattr(errno, "WSAEADDRINUSE", None)
        if win_in_use is not None and exc.errno == win_in_use:
            raise RuntimeError(f"端口 {port} 已被占用，请换端口（--port）或先关闭占用它的程序。") from exc
        raise


def serve_forever(port=PORT, bind=BIND):
    httpd = make_server(port, bind)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def start_background(port=PORT, bind=BIND):
    """后台线程启动，返回 (httpd, thread)；供 lightnovel.sync 调用。"""
    httpd = make_server(port, bind)
    t = threading.Thread(target=httpd.serve_forever, name="opds-server", daemon=True)
    t.start()
    return httpd, t


# ---------------------------- 公网隧道 ----------------------------
def find_cloudflared():
    """按 PATH -> 环境变量 CLOUDFLARED_BIN -> 常见安装位置 -> 用户工具目录 的顺序查找。"""
    for name in ("cloudflared", "cloudflared.exe"):
        found = shutil.which(name)
        if found:
            return found
    env_bin = os.environ.get("CLOUDFLARED_BIN", "")
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, ".workbuddy", "bin", "cloudflared.exe"),
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        os.path.join(home, "cloudflared", "cloudflared.exe"),
        os.path.join(home, "scoop", "shims", "cloudflared.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def start_named_tunnel(port, name=NAMED_TUNNEL_NAME, timeout=60, on_connected=None):
    """拉起**固定域名**的 named tunnel -> (proc, public_url)。
    前提：已跑过 lightnovel.tunnel_setup 完成登录、建隧道、加 DNS 记录。
    on_connected: 首次连接成功时的回调（只会触发一次）。"""
    exe = find_cloudflared()
    if not exe:
        log.error("未找到 cloudflared，无法启动 named tunnel。")
        return None, None
    host = read_named_hostname()
    if not host:
        log.error("未在 %s 中找到 hostname，请先运行 lightnovel.tunnel_setup 完成配置。", CF_CONFIG)
        return None, None
    sync_named_port(port)
    try:
        proc = subprocess.Popen(
            [exe, "--config", CF_CONFIG, "tunnel", "run", name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        log.error("启动 named tunnel 失败：%s", exc)
        return None, None

    def reader():
        fired = False
        try:
            for line in proc.stdout:
                if re.search(r"(Registered tunnel connection|Connection .* registered)", line, re.I):
                    log.info("named tunnel 已连接：https://%s/", host)
                    if on_connected and not fired:
                        fired = True
                        try:
                            on_connected()
                        except Exception:
                            pass
        except (ValueError, OSError):
            pass

    threading.Thread(target=reader, daemon=True, name="cf-named-reader").start()
    time.sleep(2)  # 给隧道一点建立时间
    return proc, "https://" + host + "/"


def read_named_hostname(config_path=None):
    """从 cloudflared config.yml 读出 ingress 的 hostname（固定公网域名）。"""
    config_path = config_path or CF_CONFIG
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return None
    m = re.search(r"hostname:\s*([A-Za-z0-9._-]+)", txt)
    return m.group(1) if m else None


def sync_named_port(port, config_path=None):
    """确保 config.yml 里 ingress 的 service 端口与本次服务端口一致；返回是否改动过。"""
    config_path = config_path or CF_CONFIG
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return False
    new = re.sub(r"service:\s*http://127\.0\.0\.1:\d+", f"service: http://127.0.0.1:{port}", txt)
    if new == txt:
        return False
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new)
        log.info("已将 cloudflared 配置里的回源端口同步为 %d", port)
        return True
    except OSError:
        return False


def start_cloudflared(port, timeout=60):
    """拉起 cloudflared 快速隧道 -> (proc, public_url)；未安装返回 (None, None)。"""
    exe = find_cloudflared()
    if not exe:
        return None, None
    try:
        proc = subprocess.Popen(
            [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        log.error("启动 cloudflared 失败：%s", exc)
        return None, None
    holder = {"url": None}

    def reader():
        try:
            for line in proc.stdout:
                m = re.search(r"https://[A-Za-z0-9-]+\.trycloudflare\.com", line)
                if m and not holder["url"]:
                    holder["url"] = m.group(0)
                    log.info("Cloudflare 隧道就绪：%s", holder["url"])
        except (ValueError, OSError):
            pass

    threading.Thread(target=reader, daemon=True, name="cf-reader").start()
    deadline = time.time() + timeout
    while time.time() < deadline and not holder["url"]:
        time.sleep(0.5)
    return proc, holder["url"]


# ---------------------------- 二维码 ----------------------------
def print_qr(url):
    try:
        import segno
        segno.make(url).terminal(compact=True)
        return True
    except Exception:
        pass
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


EXTERNET_NOTE = """
本机网络现状（2026-09 实测）
----------------------------
  外网出口 IPv4：223.67.255.x（运营商大内网），本机实际是 192.168.31.x
  IPv6：只有 fe80:: 链路本地地址，没有公网 IPv6
  => 没有公网 IP，端口映射 / DDNS 直连这条路走不通，只能用穿透或组网。

外网访问方案（任选其一，手机用流量也能访问本书源）
------------------------------------------------
1) Cloudflare 固定域名隧道（named tunnel）—— 地址永久不变，需自备域名
   前提：有一个 DNS 托管在 Cloudflare 的域名（免费套餐即可）
   配置：双击 setup_named_tunnel.bat（会开浏览器让你登录 Cloudflare 授权，
        然后自动建隧道、写配置、加 CNAME 记录）
   启动：python lightnovel.opds.server --tunnel named   或双击 run_named_tunnel.bat
   结果：https://你填的域名/  永久固定，重启不变
   没有域名？换方案 2，或去注册一个（.top/.xyz 一年十几块）。

2) Tailscale / ZeroTier 组网 —— 无需公网 IP、无需域名，推荐
   电脑和手机都装上并加入同一私有网络，手机访问电脑的虚拟 IP：
       http://100.x.y.z:8080/
   优点：加密点对点、速度快、地址固定、免费。
   缺点：手机要装 App，且 App 需保持后台连接。

3) Cloudflare 临时隧道 —— 免域名，但每次重启地址都变
       python lightnovel.opds.server --tunnel cloudflared
   会拿到 https://xxxx.trycloudflare.com，重跑就换一个，仅适合临时用。

4) frp / 花生壳 / 樱花穿透 等国内穿透
   需要一台有公网 IP 的服务器（或买现成服务），把 127.0.0.1:8080 映射出去。

安全建议：暴露到公网时务必设置口令：
   set LN_OPDS_USER=你的用户名
   set LN_OPDS_PASS=你的口令
阅读器里把地址写成 https://用户名:口令@域名/ 即可通过 Basic 认证。
"""


# ---------------------------- CLI ----------------------------
def _hide_console():
    """隐藏本进程的控制台窗口（Windows）。无控制台（如 VBS 后台启动）时为无操作。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
            log.info("服务运行正常，控制台窗口已自动隐藏（停止服务请运行 stop_opds.bat）。")
    except Exception as exc:
        log.warning("隐藏控制台窗口失败：%s", exc)


def _ensure_logging():
    if not log.handlers:
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(os.path.join(LOG_DIR, "opds.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def run_service(port=PORT, bind=BIND, public_url="", tunnel=None, no_qr=False,
                hide_window=False):
    """打印订阅信息并以**阻塞**方式运行服务（供 CLI 和 lightnovel.sync 复用）。
    hide_window=True 时，named tunnel 连接成功后自动隐藏控制台窗口（服务继续后台运行）。"""
    os.makedirs(COVER_CACHE_DIR, exist_ok=True)
    lib = get_library(force=True)
    n_books = sum(len(b) for b in lib.values())
    n_vols = sum(1 for _ in all_vols(lib))
    log.info("书库扫描完成：%d 部作品 / %d 卷", n_books, n_vols)

    lan_url = f"http://{local_ip()}:{port}/"
    log.info("OPDS 服务启动：%s（bind %s）", lan_url, bind)
    if AUTH_USER or AUTH_PASS:
        log.info("已启用 Basic 认证（用户 %s）", AUTH_USER or "(空)")
    else:
        log.warning("当前为免密访问；若已暴露公网，请设置 LN_OPDS_USER / LN_OPDS_PASS。")

    print("\n" + "=" * 62)
    print(f"  OPDS 书源已启动   端口 {port}")
    print(f"  局域网订阅地址： {lan_url}")

    cf_proc = None
    pub = public_url.rstrip("/") + "/" if public_url else ""
    if tunnel == "named":
        def _on_tunnel_up():
            if hide_window:
                threading.Timer(3, _hide_console).start()   # 留 3 秒看清启动信息
        cf_proc, url = start_named_tunnel(port, on_connected=_on_tunnel_up)
        if url:
            pub = url
            print(f"  固定公网地址：   {pub}")
        else:
            print("  named tunnel 未就绪，请先运行 lightnovel.tunnel_setup 完成登录与配置。")
    elif tunnel == "cloudflared":
        cf_proc, url = start_cloudflared(port)
        if url:
            pub = url.rstrip("/") + "/"
            print(f"  公网订阅地址：   {pub}（临时域名，重启会变）")
        else:
            print("  cloudflared 未就绪（未安装或超时）。安装：winget install Cloudflare.cloudflared")
    elif pub:
        print(f"  公网订阅地址：   {pub}")

    subscribe = pub or lan_url
    if not no_qr:
        print("\n  手机扫码订阅（或手动把下方地址填进阅读器）：\n")
        if not print_qr(subscribe):
            print("  （未安装 segno/qrcode，跳过二维码，手动输入地址即可）")
    print(f"\n  书源地址： {subscribe}")
    print(f"  书库规模： {n_books} 部作品 / {n_vols} 卷")
    if hide_window and tunnel == "named":
        print("  停止服务： 运行 stop_opds.bat（隧道连通后本窗口自动隐藏）")
    else:
        print("  停止服务： Ctrl+C")
    print("=" * 62 + "\n")

    try:
        serve_forever(port, bind)
    except KeyboardInterrupt:
        log.info("收到中断信号，停止 OPDS 服务。")
    finally:
        if cf_proc:
            cf_proc.terminate()


def main():
    ap = argparse.ArgumentParser(description="Light-Novel OPDS 书源服务（手机端远程同步）")
    ap.add_argument("--port", type=int, default=PORT, help=f"监听端口（默认 {PORT}）")
    ap.add_argument("--bind", default=BIND, help="监听地址，默认 0.0.0.0；配隧道时可用 127.0.0.1")
    ap.add_argument("--tunnel", choices=["cloudflared", "named"],
                    help="公网隧道：cloudflared=临时域名；named=固定域名（需先跑 lightnovel.tunnel_setup）")
    ap.add_argument("--public-url", default="", help="已知的公网地址，仅用于打印订阅地址/二维码")
    ap.add_argument("--no-qr", action="store_true", help="不打印二维码")
    ap.add_argument("--hide-window", action="store_true",
                    help="named tunnel 连接成功后自动隐藏控制台窗口（配合 run_named_tunnel.bat；"
                         "停止服务用 stop_opds.bat）")
    ap.add_argument("--help-internet", action="store_true", help="打印外网接入方案说明后退出")
    args = ap.parse_args()

    _ensure_logging()

    if args.help_internet:
        print(EXTERNET_NOTE)
        return

    run_service(args.port, args.bind, args.public_url, args.tunnel, args.no_qr,
                hide_window=args.hide_window)


if __name__ == "__main__":
    main()
