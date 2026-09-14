#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPDS 服务层：HTTP Handler、服务启动、cloudflared 隧道、二维码、命令行入口。"""

import argparse
import base64
import errno
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
from .finished import normalize_key, toggle_finished
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
        log.info("已启用管理员口令：网页在 /opds/login 登录（签名 cookie，%d 天）；"
                 "阅读器可用 https://用户:口令@域名/ 直接带 Basic 凭据。", session.TTL // 86400)
        log.info("未登录/访客：可正常浏览、搜索、下载与订阅，只是看不到「已读完」入口。")
    else:
        log.warning("当前没有设置管理员口令：谁都不能登录，只有本机(127.0.0.1)来源算管理员。"
                    "要管理「已读完」请设置 LN_OPDS_USER / LN_OPDS_PASS 后重启；"
                    "书库浏览/下载对所有来源开放。")

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
