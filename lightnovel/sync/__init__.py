#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F 盘网盘镜像 + 目录监控。

分层：
    catalog   日志、分类目录与书单（README）维护、目录树扫描工具
    mirror    F 盘镜像：清单、差异比对（查）、删除、增改删执行、状态看板
    monitor   目录快照/变更检测、单实例锁、监控循环、状态查看、命令行入口

依赖方向严格单向：``catalog → mirror → monitor``。

2026-09-19 起同步只保留「镜像到 F 盘」这一条路：书库本体已从 Git 仓库移除
（改由网盘镜像承担备份），原来的 ``gitops`` 模块随之删除 —— 其中非 Git 的
扫描与书单工具改名到 ``catalog``，F 盘镜像与监控照旧复用。
"""

from . import catalog, mirror, monitor  # noqa: F401

__all__ = ["catalog", "mirror", "monitor"]
