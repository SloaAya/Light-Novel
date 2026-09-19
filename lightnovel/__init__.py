#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Light-Novel 个人书库工具包。

模块总览：
    paths         全局配置与路径（唯一真源）
    opds.*        OPDS 1.2 书源服务 —— library（数据层）/ feeds（表示层）/ server（服务层）
    sync.*        F 盘镜像与目录监控 —— catalog（目录/书单工具）/ mirror（镜像）/ monitor（监控+主流程）
    tunnel_setup  Cloudflare 固定域名隧道配置向导
    ui            Tkinter 图形控制面板
    cli           统一命令行入口（``python -m lightnovel``）
"""

__version__ = "2.0.0"
__all__ = ["paths"]
