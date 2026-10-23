# -*- coding: utf-8 -*-
"""Kaggle 数据集多线程分段下载（段内续传 + 重试 + 签名过期自换 + 总长校验）。

为什么需要：`kaggle datasets download` 是单流，19.4GB 的 wav 数据集要 ~11min。
本工具取签名直链后按 N 段并行 Range 拉取；段内断点续传；403（签名过期）自动换新直链。

用法（VM 侧）：
  python3 parallel_kaggle_dl.py <owner/slug> <dst_zip> [threads]
环境：
  ~/.kaggle/credentials.json（或 KAGGLE_CONFIG_DIR 指定的目录）——OAuth 格式，
  认证走 KaggleApi.authenticate()，取直链用 `Authorization: Bearer <token>`。
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

RETRIES = 60        # 每段最大尝试次数（含断点续传，失败即退避重试）
CHUNK_BYTES = 1 << 18


def signed_url(dataset):
    """向 Kaggle API 取 302 后的签名直链（不跟随重定向，直接读 Location）。"""
    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    api.authenticate()
    tok = api.config_values["token"]
    url = "https://www.kaggle.com/api/v1/datasets/download/" + dataset.lstrip("/")
    r = requests.get(url, headers={"Authorization": "Bearer " + tok},
                     allow_redirects=False, stream=True, timeout=60)
    r.close()
    loc = r.headers.get("Location", "")
    if r.status_code not in (301, 302, 303, 307, 308) or not loc:
        raise RuntimeError("取下载直链失败: HTTP %s" % r.status_code)
    return loc


def total_size(url):
    """用 0-0 Range 探总长（Content-Range: bytes 0-0/<total>）。"""
    with requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=60) as r:
        cr = r.headers.get("Content-Range", "")
        return int(cr.split("/")[-1]) if "/" in cr else 0


def fetch_chunk(dst, lo, hi, tag, urlbox):
    """把 [lo, hi]（含）续传进 dst；403 视为签名过期，换新直链后继续。"""
    want = hi - lo + 1
    for _ in range(RETRIES):
        have = os.path.getsize(dst) if os.path.exists(dst) else 0
        if have >= want:
            return tag, have
        try:
            with requests.get(urlbox["url"],
                              headers={"Range": "bytes=%d-%d" % (lo + have, hi)},
                              stream=True, timeout=90) as r:
                if r.status_code == 403:
                    urlbox["url"] = signed_url(urlbox["dataset"])
                    raise RuntimeError("签名过期,已刷新直链")
                r.raise_for_status()
                with open(dst, "ab") as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_BYTES):
                        f.write(chunk)
        except Exception as e:  # noqa: BLE001  # 网络类错误统一退避重试
            print("  [%s] %s @ %d" % (tag, type(e).__name__, have), flush=True)
            time.sleep(5)
    return tag, -1


def main():
    if len(sys.argv) < 3:
        print("用法: parallel_kaggle_dl.py <owner/slug> <dst_zip> [threads]")
        return 2
    dataset, dst = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)

    urlbox = {"dataset": dataset, "url": signed_url(dataset)}
    total = total_size(urlbox["url"])
    print("[dl] %s → %s | %d 字节 | %d 线程" % (dataset, dst, total, n), flush=True)

    seg = total // n + 1
    jobs = []
    for i in range(n):
        lo = i * seg
        hi = min(lo + seg - 1, total - 1)
        if lo > hi:
            break
        jobs.append((lo, hi, dst + ".chunk%02d" % i))

    t0 = time.time()
    with ThreadPoolExecutor(len(jobs)) as ex:
        futs = [ex.submit(fetch_chunk, p, lo, hi, "c%02d" % i, urlbox)
                for i, (lo, hi, p) in enumerate(jobs)]
        for k, fut in enumerate(futs, 1):
            tag, got = fut.result()
            print("  段完成 %s: %d B (%d/%d)" % (tag, got, k, len(jobs)), flush=True)

    if not all(os.path.getsize(p) == hi - lo + 1 for lo, hi, p in jobs):
        print("[dl] 段不齐,失败（保留 chunk 可重跑续传）", flush=True)
        return 1

    tmp = dst + ".part"
    with open(tmp, "wb") as out:
        for _, _, p in jobs:
            with open(p, "rb") as f:
                while True:
                    b = f.read(1 << 22)
                    if not b:
                        break
                    out.write(b)
            os.remove(p)          # 边拼边删:峰值占用从 2× 降到 ~1.1×
    got = os.path.getsize(tmp)
    if got != total:
        print("[dl] 拼接后总长不符 %d != %d" % (got, total), flush=True)
        os.remove(tmp)
        return 1
    os.replace(tmp, dst)
    el = time.time() - t0
    print("[dl] OK %d B | %.0fs | %.1f MB/s" % (got, el, got / el / 1e6), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())