#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 同步 + F 盘网盘镜像 + 目录监控。

分层：
    gitops   Git 封装、仓库初始化、分块推送重试、分类目录与 README 维护、扫描工具
    mirror   F 盘镜像：清单、差异比对（查）、删除、增改删执行、状态看板
    monitor  种子复制、目录快照/变更检测、单实例锁、监控循环、状态查看、命令行入口

依赖方向严格单向：``gitops → mirror → monitor``。
"""

from . import gitops, mirror, monitor  # noqa: F401

__all__ = ["gitops", "mirror", "monitor"]
