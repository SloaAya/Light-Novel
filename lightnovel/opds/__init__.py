#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPDS 1.2 书源服务。

把 ``D:\\Light-Novel\\轻小说`` 下的书库发布成标准 OPDS 目录，手机阅读器
（静读天下 / Lithium / KyBook / Moon+ Reader 等）订阅后即可浏览、检索并
下载 epub。

分层：
    library  路径工具 + 目录索引 + epub 封面提取 + 元数据 + zip 流式打包（纯数据）
    finished 「已读完」清单的读写（纯数据，管理员专有状态）
    updates  「新增卷」提示：书库快照对比 + 待读提示（纯数据）
    feeds    Atom feed（导航型 / 获取型）与 HTML 视图（纯渲染）
    server   HTTP Handler、服务启动、cloudflared 隧道、二维码、命令行入口

依赖方向严格单向：``library / finished / updates → feeds → server``，不存在反向引用。
"""

from . import library, finished, updates, feeds, server  # noqa: F401

__all__ = ["library", "finished", "updates", "feeds", "server"]
