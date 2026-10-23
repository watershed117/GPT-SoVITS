#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方 Drive 挂载的「凭据投递」步骤（不改 colab-cli 源码）。

背景
----
Colab 官方 `drive.mount()` 的凭据链路是：
    drive.mount() → DriveFS 二进制 → 向 runtime 的凭据服务要 token
                  → 凭据服务向「前端」发 request_auth(dfs_ephemeral) 消息
                  → 前端（浏览器）完成 Google 授权并把 token 投递给该 endpoint
纯 SSH / CLI 会话没有前端，于是 colab-cli 自己扮演前端：调用
    /tun/m/credentials-propagation/<endpoint>   (authtype=dfs_ephemeral)
但它的实现里有一段 **`open("/dev/tty").readline()` 等用户按回车**——
Windows 上 `/dev/tty` 不存在，这一步必然抛异常，授权就永远配不上对
（同意链接里的 state.token 与随后的投递 POST 必须是**同一次运行的同一个 token**，
所以「点一次、下次再跑」不成立）。

本脚本做同一件事，只把「等回车」换成**轮询 API**：
  1) GET  拿 token
  2) POST dryrun=true  → 若 success=false，打印同意链接，开始轮询
  3) 用户在浏览器点「允许」
  4) 轮询到 dryrun=true 返回 success 后，POST dryrun=false 完成投递
之后 `colab drivemount`（或 drive.mount）不再需要 /dev/tty，可全自动挂载。

用法：
  python official_mount_propagate.py <endpoint> [轮询秒数]
endpoint 取自 `colab url -s <session>` 里的 /tun/m/<endpoint>。
"""
import json
import sys
import time

from colab_cli.auth import AuthProvider, get_credentials

DOMAIN = "https://colab.research.google.com"


def jload(resp):
    """Colab 的 JSON 响应前面有 XSSI 前缀 `)]}'`，取最后一行。"""
    return json.loads(resp.text.split("\n", 1)[-1])


def main():
    endpoint = sys.argv[1] if len(sys.argv) > 1 else ""
    poll_secs = int(sys.argv[2]) if len(sys.argv) > 2 else 900
    if not endpoint:
        print("用法: official_mount_propagate.py <endpoint> [轮询秒数]")
        return 2

    url = "%s/tun/m/credentials-propagation/%s" % (DOMAIN, endpoint)
    base = {"authuser": "0", "authtype": "dfs_ephemeral", "version": "2",
            "propagate": "true", "record": "false"}
    files = {"file_id": (None, "empty.ipynb")}

    creds = get_credentials(None, provider=AuthProvider.OAUTH2)

    r = creds.request("GET", url, params=dict(base, dryrun="true"))
    token = jload(r).get("token")
    if not token:
        print("STEP1_FAIL:", r.status_code, r.text[:300])
        return 3
    print("STEP1 token ok (%s...)" % token[:14], flush=True)
    headers = {"x-goog-colab-token": token}

    r = creds.request("POST", url, params=dict(base, dryrun="true"),
                      headers=headers, files=files)
    d = jload(r)
    print("STEP2 dryrun success =", d.get("success"), flush=True)

    if not d.get("success"):
        uri = d.get("unauthorized_redirect_uri", "")
        print("\n===== 需要你授权（用浏览器里已登录的 Google 账号点「允许」）=====", flush=True)
        print(uri, flush=True)
        print("==========================================================", flush=True)
        print("[poll] 等待授权完成（最多 %ds，每 5s 用 dryrun=false 真投递一次）..." % poll_secs,
              flush=True)
        t0 = time.time()
        got = False
        last_body = ""
        while time.time() - t0 < poll_secs:
            time.sleep(5)
            try:
                # 关键：CLI 原版在用户按回车后直接发 dryrun=false（真实投递）。
                # 用 dryrun=true 探可能永远返回 success=false，所以这里直接真投递。
                rr = creds.request("POST", url, params=dict(base, dryrun="false"),
                                   headers=headers, files=files)
                dd = jload(rr)
                last_body = json.dumps(dd)[:200]
            except Exception as e:  # noqa: BLE001
                print("[poll] 探测异常（继续）:", repr(e)[:120], flush=True)
                continue
            if dd.get("success"):
                print("[poll] 投递成功，用时 %ds" % int(time.time() - t0), flush=True)
                got = True
                break
            if int(time.time() - t0) % 30 < 6:
                print("[poll] %ds 未成功 | %s" % (int(time.time() - t0), last_body), flush=True)
        if not got:
            print("TIMEOUT: 未检测到授权，链接可能已过期，重跑本脚本拿新链接", flush=True)
            print("最后响应:", last_body, flush=True)
            return 4
        print("PROPAGATION_DONE", flush=True)
        return 0

    r = creds.request("POST", url, params=dict(base, dryrun="false"),
                      headers=headers, files=files)
    d = jload(r)
    print("STEP3 投递 success =", d.get("success"), "| status", r.status_code, flush=True)
    print("PROPAGATION_DONE" if d.get("success") else "PROPAGATION_FAILED", flush=True)
    return 0 if d.get("success") else 5


if __name__ == "__main__":
    sys.exit(main())
