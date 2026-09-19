#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPDS 服务层：HTTP Handler、服务启动、cloudflared 隧道、二维码、命令行入口。"""

import argparse
import base64
import errno
import hashlib
import hmac
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
    AUTH_GUEST_PASS,
    AUTH_GUEST_USER,
    AUTH_PASS,
    AUTH_USER,
    BIND,
    BLANK_PNG,
    CF_CONFIG,
    COVER_CACHE_DIR,
    COVER_MAX_W,
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
    parse_range,
    resolve_under,
    zip_plan,
    zip_size,
)
from .finished import normalize_key, toggle_finished
from . import moon
from . import session
from . import updates as upd
from .feeds import (
    _accept_wants_xml,
    book_html,
    catalog_html,
    feed_book,
    feed_catalog,
    feed_finished,
    feed_recent,
    feed_root,
    feed_search,
    index_html,
    login_html,
    opensearch_xml,
    read_html,
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

    # -------------------------------------------------------- 身份判定
    # ⚠ 这里是全服务**唯一**的角色判定入口，也是唯一能做权限结论的地方：
    #   只看请求本身（Authorization 头 + 来源地址），客户端传来的任何「我是管理员」
    #   字段（query / cookie / hidden input）一律不参与判定。
    def _basic_creds(self):
        """解析 Basic 认证头 → (user, pass)；没有/格式不对 → None。"""
        hdr = self.headers.get("Authorization", "") or ""
        if not hdr.startswith("Basic "):
            return None
        try:
            raw = base64.b64decode(hdr[6:]).decode("utf-8")
        except Exception:
            return None
        user, _, pw = raw.partition(":")
        return user, pw

    @staticmethod
    def _same(secret, given):
        """定长比较，避免用 ``==`` 比口令时的时序侧信道。"""
        try:
            return hmac.compare_digest(str(secret).encode("utf-8"),
                                       str(given).encode("utf-8"))
        except Exception:
            return False

    def _is_loopback(self):
        host = self.client_address[0] if self.client_address else ""
        if not host:
            return False
        return host in ("::1", "localhost") or host.startswith("127.")

    def _cookie(self, name):
        """从 ``Cookie:`` 头里取一个值（没有 → 空串）。"""
        return session.parse_cookie(self.headers.get("Cookie", "")).get(name, "")

    def _session_ok(self):
        """网页登录态：签名 cookie 有效即管理员。"""
        return session.verify(self._cookie(session.COOKIE_NAME))

    def _client_ip(self):
        """节流用的来源标识：隧道下取 Cloudflare 的 ``CF-Connecting-IP``，否则取 socket 对端。"""
        ip = (self.headers.get("CF-Connecting-IP", "") or "").strip()
        if not ip:
            ip = (self.headers.get("X-Forwarded-For", "") or "").split(",")[0].strip()
        if not ip:
            ip = self.client_address[0] if self.client_address else "?"
        return ip

    def _role(self):
        """判定本次请求的角色 → ``"admin"`` / ``"guest"`` / ``None``（拒绝）。

        判定规则（三条，按顺序短路）：

          1. **登录 cookie 有效** → ``admin``（网页登录页拿到的那个签名 cookie）；
          2. **Basic 凭据命中** → 管理员口令=``admin``，访客口令
             （LN_OPDS_GUEST_USER/PASS）=``guest``；
          3. **完全没配任何口令 + 回环地址** → ``admin``
             （本机开机就能管，零配置的安全下限）；
          4. 其余一律 ``guest``。

        ⚠ 第 2 条**不匹配时不会拒绝**，而是落到第 4 条当访客 —— 这是刻意的：
        浏览器会把上次输过的 Basic 凭据长期缓存并自动附上，一旦这里回 401，
        用户每次打开书库都会被系统认证框拦一下（口令早就改过了），
        「匿名也能正常访问」就成了空话。口令对不对由登录页明确告诉你（401 + 文案），
        Basic 只管「带了正确凭据就升权」，不负责「带了错凭据就赶人」。

        第 4 条保证**未登录也能正常浏览与下载**：全站只有「已读完」这类管理功能
        需要管理员身份，书库本身是给访客看的。
        """
        if self._session_ok():
            return "admin"
        creds = self._basic_creds()
        if creds:
            user, pw = creds
            if (AUTH_USER or AUTH_PASS) \
                    and self._same(user, AUTH_USER) and self._same(pw, AUTH_PASS):
                return "admin"
            if (AUTH_GUEST_USER or AUTH_GUEST_PASS) \
                    and self._same(user, AUTH_GUEST_USER) and self._same(pw, AUTH_GUEST_PASS):
                return "guest"
        if not (AUTH_USER or AUTH_PASS) and self._is_loopback():
            return "admin"
        return "guest"

    # -------------------------------------------------------- 登录 cookie
    def _is_https(self):
        """是不是走 HTTPS 进来的（隧道会带 ``X-Forwarded-Proto``）。"""
        proto = (self.headers.get("X-Forwarded-Proto", "") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _cookie_header(self, token, max_age=session.TTL):
        """拼 ``Set-Cookie``：``HttpOnly`` 防脚本读取，``SameSite=Lax`` 挡跨站携带。

        ``Secure`` 只在 HTTPS 时加 —— 加了之后浏览器只在 HTTPS 上回传这个 cookie，
        本机 ``http://127.0.0.1`` 会变成「怎么登都不是管理员」。两种入口各自登录即可，
        安全优先。
        """
        parts = ["%s=%s" % (session.COOKIE_NAME, token or ""),
                 "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=%d" % max_age]
        if self._is_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Light-Novel OPDS", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _forbidden(self, msg="仅管理员可访问"):
        """已认证但权限不够（访客访问管理员功能）→ 403，而不是 401。

        区别很重要：401 会让浏览器再弹一次登录框（用户以为口令错了），403 才是
        「你的身份没问题，但没有这个权限」。
        """
        self._send(403, f"<h1>403</h1><p>{html.escape(msg)}</p>", "text/html; charset=utf-8")

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
        role = self._role()
        if role is None:
            self._deny()
            return
        is_admin = role == "admin"          # 页面渲染期的唯一开关，向下逐层传递
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query or "")
        try:
            if path in ("/", "/index.html"):
                accept = self.headers.get("Accept", "") or ""
                if "atom+xml" in accept or "opds" in accept.lower():
                    self._send(200, feed_root(is_admin), f"{OPDS_NAV_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    self._send(200, index_html(is_admin), "text/html; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/opensearch.xml":
                self._send(200, opensearch_xml(), "application/opensearchdescription+xml; charset=utf-8",
                           head_only=head_only)
                return

            if path == "/opds/login":
                # 登录页：访客能打开（这正是它的用途），已经是管理员的直接送回去。
                back = self._local_back(qs.get("back", ["/"])[0], "/")
                if is_admin:
                    self._redirect(back)
                    return
                wait = session.login_throttle.retry_after(self._client_ip())
                err = "尝试次数过多，请 %d 秒后再试。" % wait if wait else ""
                self._send(200, login_html(err, back, wait), "text/html; charset=utf-8",
                           head_only=head_only)
                return

            if path == "/opds/read":
                # 管理员专属页面：访客到此直接 403（不返回内容，也不返回「入口」）。
                if not is_admin:
                    self._forbidden("「已读完」仅管理员可查看。"
                                    "配置 LN_OPDS_USER / LN_OPDS_PASS 后用它登录即可。")
                    return
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    self._send(200, feed_finished(page), f"{OPDS_NAV_TYPE}; charset=utf-8",
                               head_only=head_only)
                else:
                    self._send(200, read_html(page), "text/html; charset=utf-8", head_only=head_only)
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
                    body = catalog_html(cat, page, is_admin)
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
                    body = book_html(m.group(1), page, is_admin)
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
                    self._send(200, recent_html(page, is_admin), "text/html; charset=utf-8",
                               head_only=head_only)
                return

            if path == "/opds/search":
                q = qs.get("q", [""])[0]
                page = int(qs.get("page", ["1"])[0] or 1)
                if _accept_wants_xml(self.headers.get("Accept", "")):
                    self._send(200, feed_search(q, page), f"{OPDS_ACQ_TYPE}; charset=utf-8", head_only=head_only)
                else:
                    self._send(200, search_html(q, page, is_admin), "text/html; charset=utf-8",
                               head_only=head_only)
                return

            if path == "/opds/refresh":
                get_library(force=True)
                moon.wake()          # 顺带让后台线程立刻重读一次阅读进度（不阻塞本请求）
                self._send(200, '{"ok":true}', "application/json; charset=utf-8", head_only=head_only)
                return

            if path == "/opds/stats":
                lib = get_library(force=True)
                data = {c: {"books": len(b), "vols": sum(len(v) for v in b.values())} for c, b in lib.items()}
                # 阅读器进度是「后台刷 + 本地缓存」，把它的健康状况一并暴露出来：
                # unmatched 一旦变大就说明书库被改过名、关联断了（详见 .autosync/opds.log）。
                data["_moon"] = moon.snapshot()
                self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8",
                           head_only=head_only)
                return

            m = re.match(r"^/cover/(.+)$", path)
            if m:
                # 默认压到 COVER_MAX_W（移动端关键）；?w=N 指定宽度；?full=1 取原图
                mw = COVER_MAX_W
                if qs.get("full"):
                    mw = None
                elif qs.get("w"):
                    try:
                        mw = int(qs["w"][0]) or COVER_MAX_W
                    except (ValueError, TypeError):
                        mw = COVER_MAX_W
                self._serve_cover(m.group(1), head_only, mw)
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

    # -------------------------------------------------------- 写操作
    def _form(self):
        """解析 ``application/x-www-form-urlencoded`` 表单体（限长，防内存放大）。"""
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 8 * 1024:
            return {}
        try:
            raw = self.rfile.read(n).decode("utf-8", "replace")
        except (OSError, ValueError):
            return {}
        return parse_qs(raw, keep_blank_values=True)

    def _wants_json(self):
        """前端用 ``fetch`` 提交时打了 ``X-Requested-With: fetch``，就回 JSON 而不是 303。

        为什么认这个头而不是 ``Accept``：跨站 ``fetch`` 带自定义请求头会先触发 CORS 预检，
        我们不应答 OPTIONS —— 预检不通过，跨站脚本根本发不出这个请求，等于白捡一层 CSRF 兜底。
        不带这个头的（原生表单提交、curl、旧浏览器）一律走原来的 303，功能一条不少。
        """
        return (self.headers.get("X-Requested-With", "") or "").strip().lower() == "fetch"

    def _send_json(self, obj, code=200):
        """给 fetch 用的最小 JSON 响应（不缓存，避免拿到上一次的标记结果）。"""
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _same_origin(self):
        """只受理同站表单（CSRF 兜底）。

        浏览器会把已缓存的 Basic 凭据自动带上，所以「第三方页面里放个自动提交的表单」
        也能打到这里。判据：``Origin`` 的主机 == ``Host``（无 Origin 时看 ``Referer``）。
        两个头都没有（curl / 脚本）→ 放行：那不是浏览器场景，谈不上 CSRF。
        """
        host = (self.headers.get("Host", "") or "").lower()
        if not host:
            return True
        for name in ("Origin", "Referer"):
            val = self.headers.get(name)
            if not val:
                continue
            return urlparse(val).netloc.lower() == host
        return True

    def _local_back(self, back, fallback="/"):
        """回跳地址只认本站绝对路径，堵掉开放重定向（``//evil.com`` / ``http://…``）。"""
        back = (back or "").strip()
        if not back.startswith("/") or back.startswith("//") or "\\" in back or "/.." in back:
            return fallback
        return back

    def _redirect(self, location, extra=None):
        """303：刷新后是 GET，页面状态由服务端重绘，天然正确（不用前端记账）。"""
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -------------------------------------------------------- 登录 / 退出
    def _do_login(self):
        """管理员登录。三道关：同源 → 节流 → 口令比对。

        口令不对回 **401 + 登录页**（带 ``WWW-Authenticate`` 才会触发系统认证框，
        这里刻意不发，所以浏览器老老实实渲染我们的错误文案）。
        """
        if not self._same_origin():
            self._send(403, "<h1>403</h1><p>跨站请求被拒绝</p>", "text/html; charset=utf-8")
            return
        form = self._form()
        back = self._local_back(form.get("back", ["/"])[0], "/")
        ip = self._client_ip()
        wait = session.login_throttle.retry_after(ip)
        if wait:
            self._send(429, login_html("尝试次数过多，请 %d 秒后再试。" % wait, back, wait),
                       "text/html; charset=utf-8")
            return
        if not (AUTH_USER or AUTH_PASS):
            self._send(503, login_html("服务端没有设置管理员口令（LN_OPDS_USER / LN_OPDS_PASS），"
                                       "无法登录；不登录也可以正常浏览。", back),
                       "text/html; charset=utf-8")
            return
        user = (form.get("user", [""])[0] or "").strip()
        pw = form.get("pass", [""])[0] or ""
        if self._same(user, AUTH_USER) and self._same(pw, AUTH_PASS):
            session.login_throttle.ok(ip)
            log.info("管理员登录成功（%s）", ip)
            self._redirect(back, {"Set-Cookie": self._cookie_header(session.issue())})
            return
        n = session.login_throttle.fail(ip)
        log.warning("管理员登录失败（%s 第 %d 次）", ip, n)
        self._send(401, login_html("用户名或口令不对。", back), "text/html; charset=utf-8")

    def _logout_back(self, back):
        """退出后的落点：管理员专属页面在退出后必然 403，回首页，别把人丢进错误页。"""
        for prefix in ("/opds/read",):
            if back == prefix or back.startswith(prefix + "?") or back.startswith(prefix + "/"):
                return "/"
        return back

    def _do_logout(self):
        """退出登录：把 cookie 置空并设 ``Max-Age=0``（浏览器立刻删掉）。

        用 POST 而不是 GET —— GET 退出会被「页面里塞个 <img src=...>」这类
        跨站花招触发成「莫名掉线」。
        """
        if not self._same_origin():
            self._send(403, "<h1>403</h1><p>跨站请求被拒绝</p>", "text/html; charset=utf-8")
            return
        back = self._local_back(self._form().get("back", ["/"])[0], "/")
        log.info("管理员退出登录（%s）", self._client_ip())
        self._redirect(self._logout_back(back), {"Set-Cookie": self._cookie_header("", 0)})

    def do_POST(self):
        role = self._role()
        if role is None:
            self._deny()                      # 没通过身份判定：401
            return
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/opds/login":
                self._do_login()
                return
            if path == "/opds/logout":
                self._do_logout()
                return
            if path == "/opds/read/toggle":
                # 三道关：① 已认证（上面） ② 必须是管理员 ③ 必须是同源表单
                if role != "admin":
                    self._forbidden("只有管理员可以修改「已读完」清单。")
                    return
                if not self._same_origin():
                    self._send(403, "<h1>403</h1><p>跨站请求被拒绝</p>",
                               "text/html; charset=utf-8")
                    return
                form = self._form()
                key = normalize_key(form.get("key", [""])[0])
                if not key:
                    self._send(400, "<h1>400</h1><p>作品参数不合法</p>",
                               "text/html; charset=utf-8")
                    return
                on = toggle_finished(key)
                log.info("「已读完」标记 %s → %s", key, "已读完" if on else "未读完")
                if self._wants_json():
                    # 原地生效：前端拿到最终状态自己回写 DOM，不跳转、不丢滚动位置。
                    # 回的是「服务端算出来的状态」而不是「前端猜的」，两边不会漂。
                    self._send_json({"ok": True, "key": key, "finished": on})
                    return
                self._redirect(self._local_back(form.get("back", ["/"])[0], "/opds/read"))
                return
            if path == "/opds/updates/clear":
                # 与「已读完」同一套三道关：已认证 + 管理员 + 同源。
                if role != "admin":
                    self._forbidden("只有管理员可以清除新增卷提示。")
                    return
                if not self._same_origin():
                    self._send(403, "<h1>403</h1><p>跨站请求被拒绝</p>",
                               "text/html; charset=utf-8")
                    return
                form = self._form()
                raw = form.get("key", [""])[0].strip()
                if raw and not normalize_key(raw):
                    self._send(400, "<h1>400</h1><p>作品参数不合法</p>",
                               "text/html; charset=utf-8")
                    return
                n = upd.clear(normalize_key(raw) if raw else None)
                log.info("清除新增卷提示：%s（%d 部）", raw or "全部", n)
                if self._wants_json():
                    self._send_json({"ok": True, "cleared": n, "key": raw})
                    return
                self._redirect(self._local_back(form.get("back", ["/"])[0], "/"))
                return
            self._notfound(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("处理 POST 出错 %s：%s", path, exc)
            try:
                self._send(500, f"<h1>500</h1><p>{html.escape(str(exc))}</p>", "text/html; charset=utf-8")
            except Exception:
                pass

    def _serve_cover(self, enc_rel, head_only=False, max_w=None):
        blob, mime = get_cover(enc_rel, max_w)
        if not blob:
            # 取不到封面时回一张 1×1 透明 PNG，而不是 404：<img> 报错会在控制台刷屏，
            # 卡片也会塌成 alt 文本。
            # **但绝不能被缓存**：原先写的是 public/max-age=86400，于是一次偶发的
            # 取不到（服务正在重启、epub 被同步或杀毒临时占用）就会让浏览器把白图
            # 钉住整整一天 —— 服务早就恢复了，页面却一直是灰块。失败响应不是结果。
            self._send(200, BLANK_PNG, "image/png",
                       {"Cache-Control": "no-store"}, head_only=head_only)
            return
        cache = {"Cache-Control": "public, max-age=604800",
                 "ETag": '"%s"' % hashlib.md5(blob).hexdigest()[:20]}
        # 协商缓存：封面没变就回 304，省掉整张图的传输
        if self.headers.get("If-None-Match") == cache["ETag"]:
            self._send(304, b"", mime, cache, head_only=head_only)
            return
        self._send(200, blob, mime, cache, head_only=head_only)

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

        pairs = zip_plan(vols, cat, book, subdir)
        if not pairs:
            self._notfound("没有可打包的卷")
            return
        # 先预演一遍拿到精确长度，再带着 Content-Length 发响应头。
        # 下载端靠它才能显示「已下 4.4 MB / 共 130 MB」—— 没有它只能画一根不知道
        # 尽头的进度条、写着「继续下载中…」（实测的症状）。预演与实际打包共用
        # 同一个 zip_plan、同一套 zipfile 参数，长度必然一致。
        total = zip_size(pairs)
        # 断点续传。zip 的字节是**确定的**：同一批文件 + 同样的 mtime → 同样的字节，
        # 顺序也由 zip_plan 固定，所以按偏移重发是安全的（两段拼起来必然等于整包，
        # 有回归断言盯着这一点）。
        rng = parse_range(self.headers.get("Range"), total)
        if rng and rng[0] >= total:                      # 越界
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % total)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = rng if rng else (0, total - 1)
        body_len = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(body_len))
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(label + '.zip')}")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if head_only:
            return
        try:
            # skip/limit 让 sink 只写出 [start, end] 这一段。zip 仍**从头生成**、
            # 前半段丢弃 —— 省下的是网络流量（断点续传要省的正是它），磁盘读取省不掉。
            sink = _ZipSink(self.wfile, skip=start, limit=body_len)
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as zf:
                for disk, arc in pairs:
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


def tunnel_protocol():
    """cloudflared 连接边缘用的协议。

    默认 **http2**（TCP 7844），而不是 cloudflared 自己的 ``auto``。原因：``auto``
    先试 QUIC（UDP 7844），而部分宽带线路会把这条 UDP 流量丢掉 —— 症状是日志里
    反复 ``Failed to dial a quic connection: timeout: no recent network activity``，
    且它只在 QUIC 上重试、**不会自己退回 http2**，于是隧道始终零活动连接（公网
    530/502），而进程一直活着、看起来一切正常。实测同一台机器上 ``--protocol http2``
    几秒内就注册满连接；cloudflared 自己的 precheck 在这种网络下也会给出
    ``suggested_protocol=http2``。

    需要别的值时用环境变量 ``LN_TUNNEL_PROTOCOL`` 覆盖（auto / quic / http2）。
    """
    return (os.environ.get("LN_TUNNEL_PROTOCOL") or "").strip() or "http2"


def tunnel_metrics_addr():
    """cloudflared 的 metrics 监听地址 —— 守护靠它区分「进程活着」和「连接活着」。

    cloudflared 在 ``--metrics`` 指定的端口上暴露 ``/ready``：**有活动连接返回
    200，零连接返回 503**。这正是 QUIC 被丢包那类故障的判别点 —— 进程一直在、
    却一条连接都注册不上，公网 530。端口固定（而不是让它随机选）守护才找得到；
    要换端口用环境变量 ``LN_TUNNEL_METRICS_PORT``。
    """
    port = (os.environ.get("LN_TUNNEL_METRICS_PORT") or "").strip() or "20241"
    return "127.0.0.1:" + port


def tunnel_metrics_args():
    """``--metrics`` 参数 —— **必须挂在 ``tunnel`` 这一层**。

    实测（2026-09-18）：``tunnel run --metrics 127.0.0.1:20241 <name>`` 会被 cloudflared
    拒绝 —— 它打印一段 usage 然后**以退出码 0 安静退出**，于是隧道永远起不来，日志里
    只剩几行 flag 说明。写成 ``tunnel --metrics 127.0.0.1:20241 run ...`` 才生效。
    坑在于 ``tunnel run --help`` 里照样会列出 ``--metrics``（继承的 flag 都会列出来），
    只看 help 会以为它属于 ``run``。
    """
    return ["--metrics", tunnel_metrics_addr()]


def tunnel_protocol_args():
    """``--protocol`` 参数 —— 必须挂在 ``run`` 这一层（``tunnel --help`` 里没有它）。"""
    return ["--protocol", tunnel_protocol()]


def named_tunnel_argv(exe, name, config=None):
    """named tunnel 的完整命令行。

    单独抽成函数，是为了让测试能断言**参数顺序** —— 上面那个把 ``--metrics`` 放错
    层级的坑，靠"函数返回个列表"这种断言是测不出来的。
    """
    return ([exe, "--config", config or CF_CONFIG, "tunnel"] + tunnel_metrics_args()
            + ["run"] + tunnel_protocol_args() + [name])


def quick_tunnel_argv(exe, port):
    """快速隧道（trycloudflare）的完整命令行。

    这里**不能**加 ``--protocol``：快速隧道走 ``tunnel --url`` 这条路径，而 ``--protocol``
    只在 ``run`` 子命令下有效（``tunnel --help`` 里没有它，硬加会被拒。
    """
    return ([exe, "tunnel"] + tunnel_metrics_args()
            + ["--url", "http://127.0.0.1:%d" % int(port), "--no-autoupdate"])


def start_named_tunnel(port, name=None, timeout=60, on_connected=None):
    """拉起**固定域名**的 named tunnel -> (proc, public_url)。
    前提：已跑过 lightnovel.tunnel_setup 完成登录、建隧道、加 DNS 记录。
    on_connected: 首次连接成功时的回调（只会触发一次）。

    ``name`` 留空时**以 config.yml 里的 ``tunnel:`` 为准**（见
    :func:`read_named_tunnel_name`），只有配置里读不到才退回 ``NAMED_TUNNEL_NAME``。
    """
    exe = find_cloudflared()
    if not exe:
        log.error("未找到 cloudflared，无法启动 named tunnel。")
        return None, None
    host = read_named_hostname()
    if not host:
        log.error("未在 %s 中找到 hostname，请先运行 lightnovel.tunnel_setup 完成配置。", CF_CONFIG)
        return None, None
    tname = name or read_named_tunnel_name() or NAMED_TUNNEL_NAME
    sync_named_port(port)
    try:
        proc = subprocess.Popen(
            named_tunnel_argv(exe, tname),
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
                elif re.search(r"\b(ERR|WRN|FTL)\b|failed|error|cannot|no such",
                               line, re.I):
                    # cloudflared 的失败原因**必须**落到日志里：以前这里只认「已连接」
                    # 那一行，别的输出全扔了 —— 于是隧道起不来时日志上一片安静，只能
                    # 从浏览器那边看 Cloudflare Error 1033，无从排查。
                    log.warning("cloudflared: %s", line.strip())
        except (ValueError, OSError):
            pass

    threading.Thread(target=reader, daemon=True, name="cf-named-reader").start()
    time.sleep(2)  # 给隧道一点建立时间
    if proc.poll() is not None:
        # 两秒内就退了：多半是名字 / 凭证 / 配置不对。别假装成功（之前这里无条件
        # 返回 URL，调用方会打印「公网地址已就绪」，而实际域名侧是 Error 1033）。
        log.error("named tunnel 启动后立刻退出（退出码 %s，隧道名 %s）——公网地址暂时不可用。"
                  "常见原因：config.yml 里的 tunnel 名与 %s 下的凭证文件对不上。",
                  proc.returncode, tname, os.path.dirname(CF_CONFIG))
        return None, None
    return proc, "https://" + host + "/"


def read_named_tunnel_name(config_path=None):
    """从 cloudflared config.yml 读出 ``tunnel:`` 那一行的名字；读不到返回 ``None``。

    为什么不能只用常量默认值：隧道是可以重建的（换名字 = 换凭证文件）。
    名字对不上时 cloudflared 会去找 ``<name>.json``、找不到就秒退，域名侧表现为
    Cloudflare **Error 1033**（域名挂在隧道上，但没有任何在线连接）。以配置为准最稳。
    """
    config_path = config_path or CF_CONFIG
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return None
    m = re.search(r"^tunnel:\s*([^\s#]+)", txt, re.M)
    return m.group(1).strip().strip("\"'") if m else None


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
            quick_tunnel_argv(exe, port),
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
   配置：python -m lightnovel tunnel-setup（会开浏览器让你登录 Cloudflare 授权，
        然后自动建隧道、写配置、加 CNAME 记录）
   启动：双击 launchers\\launch_online.bat（服务 + 隧道 + 守护进程一起，关窗口不掉线）
        或 python -m lightnovel opds --tunnel named（前台运行，关窗口即停）
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

访问身份：书库本身对所有人开放（浏览 / 搜索 / 下载 / OPDS 订阅都不需要口令）；
只有「已读完」这类管理功能要管理员身份。两条登入口令的路径：
  * 网页：浏览器打开 /opds/login 用用户名 + 口令登录（签名 cookie，30 天有效）；
  * 阅读器 / 脚本：把地址写成 https://用户名:口令@域名/ 直接带 Basic 凭据。
设置口令（暴露公网时强烈建议）：
   set LN_OPDS_USER=你的用户名
   set LN_OPDS_PASS=你的口令
未设置任何口令时：本机(127.0.0.1)来源视为管理员，其余来源是访客。
"""


# ---------------------------- CLI ----------------------------
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


def run_service(port=PORT, bind=BIND, public_url="", tunnel=None, no_qr=False):
    """打印订阅信息并以**阻塞**方式运行服务（供 CLI 和 lightnovel.sync 复用）。

    前台阻塞运行 = 关掉这个窗口就等于停服务；要后台常驻＋断线自愈请走
    ``lightnovel.launcher``（launchers\\launch_online.bat）。
    """
    os.makedirs(COVER_CACHE_DIR, exist_ok=True)
    lib = get_library(force=True)
    n_books = sum(len(b) for b in lib.values())
    n_vols = sum(1 for _ in all_vols(lib))
    log.info("书库扫描完成：%d 部作品 / %d 卷", n_books, n_vols)

    lan_url = f"http://{local_ip()}:{port}/"
    log.info("OPDS 服务启动：%s（bind %s）", lan_url, bind)
    if AUTH_USER or AUTH_PASS:
        log.info("已启用管理员口令：网页在 /opds/login 登录（签名 cookie，%d 天）；"
                 "阅读器可用 https://用户:口令@域名/ 直接带 Basic 凭据。", session.TTL // 86400)
        log.info("未登录/访客：可正常浏览、搜索、下载与订阅，只是看不到「已读完」入口。")
    else:
        log.warning("当前没有设置管理员口令：谁都不能登录，只有本机(127.0.0.1)来源算管理员。"
                    "要管理「已读完」请设置 LN_OPDS_USER / LN_OPDS_PASS 后重启；"
                    "书库浏览/下载对所有来源开放。")

    # 阅读进度：起一个后台线程按 TTL 重读手机阅读器的位置数据。
    # 那个目录在网盘挂载盘上（单文件读约 55 ms），**请求路径上绝不能现读** ——
    # 线程首轮没跑完之前进度角标不渲染（宁可不显示，也不显示成 0%）。
    if moon.ensure_worker():
        snap = moon.snapshot()
        log.info("阅读进度：后台刷新已启动（来源 %s，每 %d 秒一次，单卷读完阈值 %.0f%%）；"
                 "首轮结果就绪前页面不显示进度角标。",
                 snap["root"], snap["ttl"], snap["done_percent"])
    elif not moon.enabled():
        log.info("阅读进度：已关闭（LN_MOON_PROGRESS=0），页面不显示进度角标。")

    print("\n" + "=" * 62)
    print(f"  OPDS 书源已启动   端口 {port}")
    print(f"  局域网订阅地址： {lan_url}")

    cf_proc = None
    pub = public_url.rstrip("/") + "/" if public_url else ""
    if tunnel == "named":
        cf_proc, url = start_named_tunnel(port)
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
    print("  停止服务： Ctrl+C（这是前台运行）")
    print("  想要后台常驻＋断线自愈： launchers\\launch_online.bat（停止用 stop_opds.bat）")
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
    ap.add_argument("--help-internet", action="store_true", help="打印外网接入方案说明后退出")
    args = ap.parse_args()

    _ensure_logging()

    if args.help_internet:
        print(EXTERNET_NOTE)
        return

    run_service(args.port, args.bind, args.public_url, args.tunnel, args.no_qr)


if __name__ == "__main__":
    main()
