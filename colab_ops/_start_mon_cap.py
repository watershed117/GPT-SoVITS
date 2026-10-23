#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把监控页切到 res_cap 冒烟的日志 /content/cap05.log（复用 start_monitor.py）。"""
import os
import runpy

os.environ["MON_LOG"] = "/content/cap05.log"
os.environ["MON_PORT"] = "8000"
runpy.run_path("/content/start_monitor.py", run_name="__main__")