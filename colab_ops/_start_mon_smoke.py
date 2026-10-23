#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经 colab exec 启动监控页,监控 smoke.log(双臂冒烟日志)。
复用 start_monitor.py 逻辑,只把 MON_LOG 指向 /content/smoke.log。"""
import os
import runpy

os.environ["MON_LOG"] = "/content/smoke.log"
os.environ["MON_PORT"] = "8000"
runpy.run_path("/content/start_monitor.py", run_name="__main__")