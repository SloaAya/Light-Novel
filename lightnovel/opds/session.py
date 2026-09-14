#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理员会话：**签名 cookie**，供网页登录用 —— 与 Basic 口令是两条独立入口。

为什么网页侧不用 Basic
----------------------
阅读器（Moon+ / 静读天下）能在订阅地址里写 ``https://用户:口令@域名/``，浏览器不行：
浏览器只会弹一个由系统绘制的认证框 —— 用户既看不出「现在登没登进去」，也没法
「退出登录」（只能重启浏览器）。所以网页侧改成标准的「登录页 → 签名 cookie」；
Basic 保留给阅读器与脚本（URL 里带凭据），两条路都在 :meth:`OPDSHandler._role`
里汇合成同一个角色结论。

设计要点
--------
* **无状态**：cookie 值就是 ``到期时间戳.HMAC 签名``，服务端不存会话表。
  重启进程、换端口都不掉线；状态文件丢了也只是所有人重新登一次。
* **密钥落盘**（``.autosync/session.key``，32 字节随机数）：留在本机、不进仓库
  （``.autosync/`` 已 gitignore）。密钥文件读不出来/格式不对 → 生成新的覆盖，
  最坏结果是「所有人被登出」，绝不会出现「用一个可猜的密钥继续跑」。
* **进程内缓存密钥**：万一目录不可写（配置成只读），至少本次运行是自洽的 ——
  否则每个请求都重新生成一把新钥匙，用户会看到「刚登录就掉线」这种鬼现象。
* **失败节流**：登录接口是全服务唯一能猜口令的地方，按来源计数，连错就冷一会儿。

与 ``finished.py`` / ``updates.py`` 同一套约定：状态不进仓库、写入原子
（临时文件 + fsync + :func:`os.replace`）、读取永不抛、全程持锁。
"""

import hashlib
import hmac
import logging
import os
import secrets
import tempfile
import threading
import time

from ..paths import SESSION_KEY_FILE

log = logging.getLogger("sync")

# cookie 名刻意不带 opds/ln-opds 之类会被隧道/WAF 关注的字样，就是个普通短名
COOKIE_NAME = "ln_adm"
# 有效期 30 天：这是个人书库，不是网银，减少重复登录比缩短窗口更划算
TTL = 30 * 24 * 3600
_KEY_BYTES = 32

_LOCK = threading.RLock()
_CACHE = {"key": b""}


# ---------------------------- 签名密钥 ----------------------------
def _new_key():
    """生成并原子落盘一把新密钥；写不进去也不抛（返回内存里的那把）。"""
    key = secrets.token_bytes(_KEY_BYTES)
    try:
        directory = os.path.dirname(SESSION_KEY_FILE)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(key.hex().encode("ascii"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SESSION_KEY_FILE)
            tmp = None
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        log.info("已生成新的会话签名密钥：%s", SESSION_KEY_FILE)
    except OSError as exc:
        log.warning("会话密钥写入失败（本次运行仍可登录，重启后会换钥匙）：%s", exc)
    return key


def _read_key():
    """读密钥；hex 文本或裸字节都认，坏到不能用就换一把新的。"""
    try:
        with open(SESSION_KEY_FILE, "rb") as f:
            raw = f.read().strip()
    except OSError:
        raw = b""
    if len(raw) >= _KEY_BYTES:
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return raw[:_KEY_BYTES]
        if len(text) == _KEY_BYTES * 2 and all(c in "0123456789abcdefABCDEF" for c in text):
            return bytes.fromhex(text)
        return raw[:_KEY_BYTES]
    return _new_key()


def key():
    """当前签名密钥（首次调用会读盘或生成）。"""
    with _LOCK:
        if not _CACHE["key"]:
            _CACHE["key"] = _read_key()
        return _CACHE["key"]


def forget_key():
    """丢掉内存缓存，下次调用重新读盘 —— 换过密钥文件（或测试改了路径）后调它。"""
    with _LOCK:
        _CACHE["key"] = b""


# ---------------------------- 令牌 ----------------------------
def _sign(exp_text):
    return hmac.new(key(), b"v1:" + exp_text.encode("ascii"), hashlib.sha256).hexdigest()


def issue(ttl=TTL, now=None):
    """签发一个 cookie 值：``到期时间戳.HMAC``。"""
    exp = int((time.time() if now is None else now) + ttl)
    return "%d.%s" % (exp, _sign("%d" % exp))


def verify(token, now=None):
    """校验 cookie 值：签名不对 / 过期 / 格式乱 → ``False``（不抛）。"""
    if not token or not isinstance(token, str):
        return False
    exp_text, sep, sig = token.partition(".")
    if not sep or not exp_text or not sig:
        return False
    # 先比签名再看到期：避免「伪造的过期令牌」和「真令牌」走两套分支
    if not hmac.compare_digest(sig, _sign(exp_text)):
        return False
    try:
        exp = int(exp_text)
    except ValueError:
        return False
    return exp > (time.time() if now is None else now)


def parse_cookie(header):
    """把 ``Cookie:`` 头切成 ``{名: 值}``（同名只取第一个）。任何输入都不抛。"""
    out = {}
    for part in (header or "").split(";"):
        name, sep, val = part.partition("=")
        name = name.strip()
        if sep and name and name not in out:
            out[name] = val.strip()
    return out


# ---------------------------- 登录失败节流 ----------------------------
class Throttle:
    """按来源计数的失败节流：``limit`` 次失败 → 冷却 ``lock_for`` 秒。

    只放在内存里，重启即清零 —— 目的是挡住「对着登录框猛试口令」，不是做长期封禁；
    个人服务里长期封禁的维护成本远高于收益（把自己锁在外面更麻烦）。
    """

    def __init__(self, limit=8, window=600, lock_for=900):
        self.limit = limit
        self.window = window
        self.lock_for = lock_for
        self._m = {}
        self._lk = threading.Lock()

    def retry_after(self, ip, now=None):
        """还要等多少秒（0 = 现在可以试）。"""
        now = time.time() if now is None else now
        with self._lk:
            rec = self._m.get(ip)
            if not rec:
                return 0
            if rec["until"] > now:
                return int(rec["until"] - now) + 1
            if now - rec["first"] > self.window:
                del self._m[ip]
            return 0

    def fail(self, ip, now=None):
        """记一次失败，返回这轮累计的失败次数。"""
        now = time.time() if now is None else now
        with self._lk:
            rec = self._m.get(ip)
            if not rec or now - rec["first"] > self.window:
                rec = {"first": now, "n": 0, "until": 0.0}
            rec["n"] += 1
            if rec["n"] >= self.limit:
                rec["until"] = now + self.lock_for
                rec["n"] = 0
                rec["first"] = now
            self._m[ip] = rec
            return rec["n"]

    def ok(self, ip):
        with self._lk:
            self._m.pop(ip, None)

    def clear(self):
        with self._lk:
            self._m.clear()


login_throttle = Throttle()
