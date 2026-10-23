#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经 colab exec 重启监控页,目标改为延长 B 臂的日志 /content/longB.log。
复用 start_monitor.py（内含 pkill 旧实例 + detached 启动）。"""
import os
import runpy

os.environ["MON_LOG"] = "/content/longB.log"
os.environ["MON_PORT"] = "8000"
runpy.run_path("/content/start_monitor.py", run_name="__main__")