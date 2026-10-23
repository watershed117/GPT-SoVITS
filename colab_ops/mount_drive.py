#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 Colab 内核里执行官方 Drive 挂载（配合 docs/colab_ops_sop.md §3.1 三步配方）。

前置：已用 official_mount_propagate.py 完成凭据投递（否则本脚本会一直等 request_auth）。
用法：
  colab exec -s <session> --timeout 300 -f colab_ops/mount_drive.py

注意 --timeout：colab exec 默认只有 30s，而凭据刚就绪时 drive.mount 仍可能阻塞数十秒到 180s。
"""
import os
import subprocess

from google.colab import drive

MOUNT = os.environ.get("DRIVE_MOUNT", "/content/drive")

try:
    drive.mount(MOUNT, timeout_ms=180000)
    print("MOUNT_CALL_RETURNED")
except Exception as e:  # noqa: BLE001
    print("MOUNT_CALL_FAIL:", type(e).__name__, str(e)[:300])

print("MyDrive exists =", os.path.exists(os.path.join(MOUNT, "MyDrive")))
print(subprocess.run(
    ["bash", "-lc",
     "grep fuse.drive /proc/mounts || echo 'NOT MOUNTED'; "
     "ls -1 %s/MyDrive 2>/dev/null | head -8" % MOUNT],
    capture_output=True, text=True).stdout)
