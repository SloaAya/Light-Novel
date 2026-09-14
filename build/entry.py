#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyInstaller 打包入口。

为什么不直接拿 ``lightnovel/__main__.py`` 当入口：那个文件用的是相对导入
（``from .cli import main``），一旦被当成顶层脚本执行，``__package__`` 为空，
相对导入直接 ImportError。这里用绝对导入消掉这个坑。
"""

import sys

from lightnovel.cli import main

if __name__ == "__main__":
    sys.exit(main())
