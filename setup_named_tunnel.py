#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloudflare 固定域名隧道（named tunnel）配置向导
================================================
把 OPDS 书源绑定到一个**固定不变**的公网域名，例如 https://ln.你的域名.com/

前置条件（缺一不可）：
  1. 你有一个域名，且 DNS 已托管在 Cloudflare（免费套餐即可）
  2. 本机有桌面环境（向导会打开浏览器让你登录 Cloudflare 账号授权）

向导会依次完成：
  ① cloudflared tunnel login    —— 浏览器授权，生成 ~/.cloudflared/cert.pem
  ② cloudflared tunnel create   —— 创建隧道，生成凭证 json
  ③ 写入 config.yml             —— 域名 -> http://127.0.0.1:<端口> 的回源规则
  ④ cloudflared tunnel route dns —— 在 Cloudflare 自动添加 CNAME 记录

完成后启动服务：
    python opds_server.py --tunnel named
或直接双击 run_named_tunnel.bat

用法：
    python setup_named_tunnel.py                # 交互式引导
    python setup_named_tunnel.py --name ln-opds # 指定隧道名
"""

import os
import re
import sys
import json
import argparse
import subprocess
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import opds_server as O
    CF_CONFIG = Path(O.CF_CONFIG)
    DEFAULT_NAME = O.NAMED_TUNNEL_NAME
    DEFAULT_PORT = O.PORT
    find_cloudflared = O.find_cloudflared
except Exception:  # 独立运行时的兜底
    CF_CONFIG = Path.home() / ".cloudflared" / "config.yml"
    DEFAULT_NAME = "ln-opds"
    DEFAULT_PORT = int(os.environ.get("LN_OPDS_PORT", "8080"))

    def find_cloudflared():
        for n in ("cloudflared", "cloudflared.exe"):
            p = __import__("shutil").which(n)
            if p:
                return p
        cands = [Path.home() / ".workbuddy" / "bin" / "cloudflared.exe",
                 r"C:\Program Files (x86)\cloudflared\cloudflared.exe"]
        for c in cands:
            if Path(c).is_file():
                return str(c)
        return None

CF_DIR = CF_CONFIG.parent
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean(s):
    return ANSI.sub("", s or "").strip()


def run_cf(args, interactive=False, exe=None):
    exe = exe or find_cloudflared()
    if interactive:
        print(f"\n>>> cloudflared {' '.join(args)}\n")
        return subprocess.run([exe, *args])
    r = subprocess.run([exe, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    r.out = clean(r.stdout)
    r.err = clean(r.stderr)
    return r


def step_login(exe):
    cert = CF_DIR / "cert.pem"
    if cert.is_file():
        print(f"  已登录（{cert}），跳过。")
        return True
    print("  即将打开浏览器，请在网页里登录 Cloudflare 并选择要使用的域名。")
    input("  准备好后按回车继续…")
    r = run_cf(["tunnel", "login"], interactive=True, exe=exe)
    if not (CF_DIR / "cert.pem").is_file():
        print("  ✗ 未生成 cert.pem，登录可能失败或被取消。请重跑本向导。")
        return False
    print("  ✓ 登录成功")
    return True


def tunnel_id_of(name, exe):
    """从 tunnel list 里按名字找 UUID。"""
    r = run_cf(["tunnel", "list"], exe=exe)
    txt = (getattr(r, "out", "") or "") + (getattr(r, "stdout", "") or "")
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name:
            return parts[0]
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", txt)
    return m.group(1) if m and name in txt else None


def step_create(name, exe):
    r = run_cf(["tunnel", "create", name], exe=exe)
    out = (getattr(r, "out", "") or "") + (getattr(r, "err", "") or "")
    print("  " + (out.splitlines()[0] if out.splitlines() else "(无输出)"))
    m = re.search(r"id\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", out)
    if m:
        return m.group(1)
    tid = tunnel_id_of(name, exe)  # 已存在的情况
    if tid:
        print(f"  隧道已存在，复用 id {tid}")
        return tid
    print("  ✗ 无法创建或定位隧道，请检查上面的输出。")
    return None


def write_config(name, tid, hostname, port):
    CF_DIR.mkdir(parents=True, exist_ok=True)
    cred = CF_DIR / f"{tid}.json"
    cfg = (
        f"# 由 setup_named_tunnel.py 自动生成\n"
        f"tunnel: {name}\n"
        f"credentials-file: {cred.as_posix()}\n"
        f"\n"
        f"ingress:\n"
        f"  - hostname: {hostname}\n"
        f"    service: http://127.0.0.1:{port}\n"
        f"  - service: http_status:404\n"
    )
    CF_CONFIG.write_text(cfg, encoding="utf-8")
    print(f"  ✓ 已写入 {CF_CONFIG}")
    if not cred.is_file():
        print(f"  ⚠ 未找到凭证文件 {cred.name}，请确认 tunnel create 是否成功。")


def step_route_dns(name, hostname, exe):
    r = run_cf(["tunnel", "route", "dns", name, hostname], exe=exe)
    out = (getattr(r, "out", "") or "") + (getattr(r, "err", "") or "")
    for line in out.splitlines():
        if line.strip():
            print("  " + line)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="Cloudflare 固定域名隧道配置向导")
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"隧道名称（默认 {DEFAULT_NAME}）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"回源端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--hostname", default="", help="完整域名，如 ln.example.com（不填则交互询问）")
    args = ap.parse_args()

    exe = find_cloudflared()
    if not exe:
        print("✗ 未找到 cloudflared。请先安装，或把 cloudflared.exe 放到：")
        print("    " + str(Path.home() / ".workbuddy" / "bin" / "cloudflared.exe"))
        return 1
    print(f"使用 cloudflared：{exe}\n")

    print("【0/4】前置检查")
    print(f"  配置目录： {CF_DIR}")
    print(f"  回源地址： http://127.0.0.1:{args.port}")
    print("  提示：域名必须已在 Cloudflare 托管（DNS 由 Cloudflare 解析）。\n")

    print("【1/4】登录 Cloudflare")
    if not step_login(exe):
        return 1

    print("\n【2/4】创建隧道")
    tid = step_create(args.name, exe)
    if not tid:
        return 1
    print(f"  隧道 id： {tid}")

    print("\n【3/4】绑定域名")
    hostname = args.hostname.strip()
    if not hostname:
        print("  请填写一个**完整子域名**，例如：ln.example.com")
        print("  （必须是 Cloudflare 上已托管的域名；子域名可以随意起，脚本会自动创建 CNAME）")
        hostname = input("  域名：").strip()
    hostname = re.sub(r"^https?://", "", hostname).strip().rstrip("/")
    if not re.match(r"^[A-Za-z0-9._-]+\.[A-Za-z]{2,}$", hostname):
        print(f"  ✗ 域名格式看起来不对：{hostname}")
        return 1
    write_config(args.name, tid, hostname, args.port)

    print("\n【4/4】添加 DNS 记录")
    ok = step_route_dns(args.name, hostname, exe)
    if not ok:
        print("  ⚠ DNS 记录添加可能失败。若失败，请到 Cloudflare 后台手动添加：")
        print(f"    类型 CNAME  名称 {hostname.split('.')[0]}  目标 {tid}.cfargotunnel.com")

    print("\n" + "=" * 62)
    print("  配置完成！")
    print(f"  固定书源地址： https://{hostname}/")
    print(f"  隧道名称：     {args.name}")
    print(f"  回源端口：     {args.port}")
    print("")
    print("  启动服务（固定域名）：")
    print("     python opds_server.py --tunnel named")
    print("  或双击 run_named_tunnel.bat")
    print("")
    print("  手机阅读器里填：https://" + hostname + "/")
    print("  （DNS 首次生效可能需要几十秒到几分钟）")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
