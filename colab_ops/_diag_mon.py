#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：VM 上监控进程与其日志参数、本机（VM）日志文件清单。"""
import subprocess

print(subprocess.run(
    ["bash", "-lc",
     "echo '[monitor 进程]'; pgrep -af monitor_server.py; "
     "echo '[日志文件]'; ls -l /content/*.log 2>/dev/null; "
     "echo '[启动脚本]'; ls -l /content/_start_mon*.py /content/start_monitor.py 2>/dev/null; "
     "echo '[监控输出尾]'; tail -5 /content/monitor.out 2>/dev/null"],
    capture_output=True, text=True).stdout)