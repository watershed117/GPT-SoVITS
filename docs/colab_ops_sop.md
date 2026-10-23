# Colab 运维规范（SSH 会话 · 端口转发 · Drive 挂载）

> 2026-10-06 定稿；**2026-10-24 泛化清理**：删除旧项目（stage1）的一次性会话脚本与专属训练流程，只保留与项目无关的通用操作规范。
> 适用场景：训练/实验跑在 Colab（付费 GPU 或免费 CPU），本地用 `colab-cli` 驱动。
> 配套工具：仓库根目录 [`colab_ops/`](../colab_ops/)（通用：rclone 接入 ×2、DriveFS 挂载 ×2、SSH 隧道 ×1、Kaggle 多线程下载 ×1、**v6 训练监控页 ×1**：`monitor_v6.py`，解析 `_deliverytransfer/v6/train.py` 的 `log.jsonl`，本地/VM 通用）。
> 旧项目的训练监控页（`monitor_server.py`，解析 stage1 文本日志）已删，需要时从 git 历史（0845b1e）取。

## 0. 三条原则（先记这三条，其余都是推论）

1. **会话 = 一个活的 Jupyter 内核**；`/content` 在会话回收时**全部丢失**。产物必须落到会话之外：
   Google Drive、Kaggle 数据集、或本地。
2. **纯 SSH 会话没有浏览器前端**。凡依赖前端的官方能力（`drive.mount()` 的凭据投递、
   `eval_js`/`kernel.proxyPort()`）都必须换实现——本文件给的就是替代方案。
3. **一个 runtime 只允许一条 SSH 连接**。多个端口转发必须挂在**同一条** `ssh -L ... -L ...` 上，
   第二条连接会报 HTTP 429 `Already-active SSH session`。

---

## 1. 会话生命周期（怎么开机器）

| 命令 | 作用 | 备注 |
|---|---|---|
| `colab new -s <name> [--gpu L4\|T4\|A100]` | 开新会话（省略 `-s` 会得到随机名，后续难引用） | CPU 会话最省额度，链路验证够用 |
| `colab sessions` | 列出服务端在跑的 runtime | 本地状态被清时用它对账 |
| `colab status -s <name>` | 硬件 / IDLE-BUSY / 上次执行 | |
| `colab url -s <name>` | 打印「把网页挂到**这个已存在会话**」的 URL | 里面含 `/tun/m/<endpoint>`，**Drive 投递要用这个 endpoint** |
| `colab stop -s <name>` | 停会话释放额度 | **收工必做**；闲置 VM 会烧计算单元 |
| `colab exec -s <name>` / `colab console -s <name>` | 走内核执行 / 走 tmux bash 执行 | **内核被阻塞时用 `console`**（它不排队） |

- 本地状态在 `~/.config/colab-cli/`（`sessions.json` / `token.json` / `colab.log`）。
- **官方只支持 Linux/macOS**（CLI 自带 README 明写）；本项目在 Windows 上跑，是因为本地已打补丁
  （把 Windows 桥的 select-on-pipe 改成阻塞 `os.read`）。升级 CLI 后需重新确认该补丁。
- **无主 runtime 会被平台回收**：靠 SSH 连接或浏览器标签保活；长训练进程自带超时自杀（--max-hours 类）。

---

## 2. SSH 与端口转发

`colab_ssh --proxy-mode` 就是**标准 OpenSSH ProxyCommand**，所以 `-L` 转发由 ssh 客户端自己实现，
不依赖 Colab 前端（实测可用）。

### 2.1 ssh_config 模板

> 占位符：`<repo>` = 本仓库绝对路径；`<你的用户目录>`；`<SESSION>` = 会话名。known_hosts 建议指到工作区内目录，避免写 `~/.ssh`。

```sshconfig
Host colabmon
  User root
  HostName colab-runtime
  IdentityFile <你的用户目录>/.ssh/id_ed25519
  StrictHostKeyChecking no
  UserKnownHostsFile <repo>/.colab_known_hosts
  LogLevel ERROR
  ServerAliveInterval 30
  ProxyCommand <venv 的 python> -m colab_cli.cli ssh --proxy-mode -s <SESSION>
```

> 用 `-m colab_cli.cli` 调 venv 里的 python，避免 PATH 上找不到 `colab`。
> [`colab_ops/tunnel.ps1`](../colab_ops/tunnel.ps1) 自动生成同款配置并起隧道（`-Background` 走 `Start-Job`）。

### 2.1.1 交互式登录：**不要用 `colab ssh`**（Windows 必炸，实测）

`colab ssh`（非 proxy-mode）内部用 `shlex.join` 生成 ProxyCommand，得到的是
**POSIX 风格的单引号 + 反斜杠路径**，Windows 的 OpenSSH 用 `posix_spawnp` 拉不起来：

```
CreateProcessW failed error:2
posix_spawnp: No such file or directory
```

**绕法**：直接用系统 ssh + 上面的 ssh_config（路径**必须正斜杠、不加引号**）：

```cmd
ssh -F <config 路径> colabmon                      :: 交互 shell
ssh -F <config 路径> colabmon "nvidia-smi -L"      :: 跑单条命令
```

隧道与登录共用一条连接即可。注意每次重开会话都会换 host key，遇到
`REMOTE HOST IDENTIFICATION HAS CHANGED` 先
`ssh-keygen -R colab-runtime -f <known_hosts 路径>`。

### 2.2 单连接多转发（推荐姿势）

```powershell
ssh -F <config> -o ExitOnForwardFailure=yes -N `
    -L 8010:127.0.0.1:8000 `      # 任务 A 的 web 页
    -L 53682:127.0.0.1:53682 `    # rclone 授权回调（用 rclone 路线时才需要）
    colabmon
```

**校验**：`curl -s -o NUL -w "%{http_code}" http://127.0.0.1:8010/health` 应为 200。

---

## 3. Drive 接入（怎么挂载谷歌云盘；两条路线，都实测跑通）

### 3.1 官方 DriveFS（推荐：路径与 notebook 完全一致）

**机制**（读 `google/colab/drive.py` + `colab_cli/commands/automation.py` 得出）：

```
drive.mount('/content/drive')
  → DriveFS 二进制 /opt/google/drive/drive        ← 注意：二进制名叫 drive，不叫 drivefs
  → 向 runtime 凭据服务要 token
     （--metadata_server_auth_uri=<TBE_EPHEM_CREDS_ADDR>/computeMetadata/v1，实测 172.28.0.1:8009）
  → 凭据服务向「前端」发 request_auth(dfs_ephemeral) 消息
  → 前端完成 Google 授权并把 token 投递给「该 endpoint」
```

纯 SSH 没有前端 ⇒ 必须有人扮演前端。`colab drivemount` 想做这件事，但**在 Windows 上永远失败**：

| 致命细节 | 说明 |
|---|---|
| **token 必须配对** | 同意链接里的 `state.token` 与随后的投递 POST 必须是**同一次运行的同一个 token**。每次重跑都会生成新 token ⇒「点一次、下次再跑」不成立。 |
| **真实投递是 `dryrun=false`** | CLI 先看 `dryrun=true` 的返回值，而该值**即使授权已完成也一直是 false**（实测）⇒ CLI 必然进入「打印链接 + `open("/dev/tty").readline()`」分支；Windows 无 `/dev/tty` ⇒ 异常 ⇒ 流程死。**所以 `colab drivemount` 的失败与"有没有授权"无关。** |

**配方（三步）**：

```powershell
$colab = '<venv 目录>\colab.exe'
$py    = '<venv 目录>\python.exe'

# ① 取 endpoint（形如 m-s-kkb-xxxx）
& $colab url -s <SESSION>            # 输出里的 /tun/m/<endpoint>

# ② 投递凭据：脚本打印同意链接并轮询；浏览器点「允许」后通常 5s 内完成
& $py colab_ops\official_mount_propagate.py <endpoint> 900
#    期望： STEP2 dryrun success = False → （授权）→ [poll] 投递成功 → PROPAGATION_DONE

# ③ 内核里挂载（凭据就绪后 request_auth 不再需要前端回复；会阻塞数十秒~180s）
#    ⚠ colab exec 默认超时只有 30s，必须显式加长，否则报 TimeoutError（内核其实还在跑）
& $colab exec -s <SESSION> --timeout 300 -f colab_ops\mount_drive.py
```

**顺序不可颠倒**：先投递再挂载。凭据没就绪时 `drive.mount()` 会一直等 `request_auth`。

**验证（关键一步）**：用 `colab console`（tmux bash，**独立于内核**）查：

```bash
grep fuse.drive /proc/mounts          # drive /content/drive fuse.drive rw,...
ls /content/drive/MyDrive             # 应看到 Drive 顶层
echo hi > /content/drive/MyDrive/_t.txt && cat /content/drive/MyDrive/_t.txt && rm -f /content/drive/MyDrive/_t.txt
```

这一步同时证明「**非内核进程也看得见挂载**」——即 SSH 里跑的任务进程可以直接写产物。

**生命周期**：

- DriveFS 进程由 `setsid` 起、末尾 `disown -a` ⇒ **内核重启、SSH 断开都不会掉**（实测）。
- 凭据类型是 `dfs_ephemeral`（**临时**）⇒ 会话回收或过期后需重挂；同意已授权过，第 ② 步通常秒过。
- 正常卸载走 `--push_changes_and_quit` 刷盘；**异常回收时未刷的写会丢** ⇒ 重要产物落盘后**读回校验**。

### 3.2 rclone（备选：大文件搬运、与 Colab 内部解耦）

```powershell
# 本机一次性授权（浏览器点一次），产出 rclone.conf + rclone.conf.b64
powershell -ExecutionPolicy Bypass -File colab_ops\mint_token.ps1
# 会话内：装 + 配 + 挂 + 传
bash /content/colab_drive.sh install        # rclone 静态二进制 + fuse3
bash /content/colab_drive.sh config "<base64>"   # 或直接 colab upload rclone.conf 到 /root/.config/rclone/
bash /content/colab_drive.sh mount          # 挂 /content/gdrive，并建软链 /content/drive/MyDrive → /content/gdrive
bash /content/colab_drive.sh push /content/out/ckpt.pt backup/ckpt.pt
bash /content/colab_drive.sh pull data/bundle.zip /content
```

- **大文件走 `push`/`pull` 而不是 FUSE 直写**：`rclone copyto` 原子改名 + 断点续传 + **可 md5 校验**
  （实测 1MB 上传→下载→md5 一致）。
- **风险**：默认用的是 **rclone 共享 client_id，Google 正在退役它**（挂载时已见 `rateLimitExceeded`）。
  长期方案：自己 GCP 项目启用 Drive API + 建「桌面应用」OAuth 客户端，用
  `mint_token.ps1 -ClientId ... -ClientSecret ...` 重签。
- 若账号开了 Advanced Protection，自建未验证客户端会被 `policy_enforced` 拒 ⇒ 只能走官方 DriveFS 或 Kaggle 数据集。

### 3.3 选择

| | 官方 DriveFS | rclone |
|---|---|---|
| 路径 | `/content/drive/MyDrive/...`（notebook 同款） | `/content/gdrive` + 兼容软链 |
| 授权 | 一次性同意（token 需配对，可脚本化） | 一次性 refresh_token，与浏览器无关 |
| 凭据寿命 | `dfs_ephemeral`，临时 | 长期 refresh_token |
| 大文件 | FUSE 直写（历史实测 ~30–60s / 1.5GB） | `push` 原子 + md5 可校验 |
| 主要风险 | 依赖 Colab 内部凭据服务按 endpoint 投递 | 共享 client_id 退役 |

**建议**：日常通路用官方 DriveFS（路径兼容、体验同 notebook）；**产物落盘仍"写 + 读回校验"**，
需要强保证时叠加 rclone push 或 Kaggle 数据集做第二副本。

---

## 4. 端口暴露与常驻进程

- **Colab 官方 `kernel.proxyPort()` / `eval_js` 在纯 SSH 下会永久阻塞**（要前端执行 JS，实测卡死内核，
  只能 `restart-kernel` 收场）⇒ 暴露端口**只能用 SSH 隧道**（§2.2）。
- **常驻进程用 `setsid nohup ... < /dev/null` 起** ⇒ **重启内核后仍存活**（实测）；
  日志写文件、`tail -f` 跟。
- 若做资源监控曲线：**采样必须后台线程高频采（1s）**，不能请求到达时现采——页面刷新间隔远大于
  快变信号周期时，稀疏瞬时读数会**混叠**（如 GPU 利用率随 step 相位在 58~100 间跳，不是真的掉）。

---

## 5. 环境坑清单（全部实测踩过）

| 现象 | 根因 | 处置 |
|---|---|---|
| `.ps1` 报莫名语法错 / 中文吞掉下一行 | PowerShell **5.1** 按 GBK 读无 BOM 的 UTF-8 脚本 | **`.ps1` 必须带 UTF-8 BOM**（改完脚本记得补） |
| 脚本无故中断、退出码 1 | `$ErrorActionPreference='Stop'` 时**原生命令往 stderr 写一行就抛终止错误**（rclone 启动必打 NOTICE） | 改 `Continue` + 显式判定；rclone 加 `--log-level ERROR`；末尾显式 `exit 0` |
| `colab.exe` 报 `UnicodeEncodeError: 'gbk' codec can't encode ...` | Python 用 cp936 写 stdout，VM 返回的 UTF-8 含替换字符 | 先设 `$env:PYTHONIOENCODING='utf-8'`，并 `[Console]::OutputEncoding=[Text.Encoding]::UTF8` |
| `Start-Process : Access is denied` | 受限 shell/沙箱拒绝带重定向的进程创建 | 用**原生重定向** `& cmd ... *> file`；需要后台就交给作业管理器 |
| 下载 rclone 失败 / 403 | `downloads.rclone.org` 在国内常被挡 | 从 GitHub release 取；rclone 是 Go 程序，**只认 `HTTP(S)_PROXY`**（不读 WinINET），需显式注入系统代理 |
| `rclone mount` 报 `fusermount3: executable not found` | Colab 镜像只带 fuse2 的 `fusermount` | `apt-get install -y fuse3`（`colab_drive.sh install` 已自动做） |
| `rclone mount` 步骤挂住十几分钟不返回 | `--daemon` 继承了调用方 stdout/stderr，调用方用管道捕获就永远等不到 EOF | 调用处加 `</dev/null >>"$LOG" 2>&1`，并给单步加超时 |
| 脚本"静默"退出、看不到真因 | `set -e` 下 `[ ... ] && cmd` 条件为假即整行非零 → 直接退出 | 一律写 `if ...; then ...; fi` |
| 第二条 ssh 报 429 | 一个 runtime 只允许一条 SSH 连接 | 多转发合并到同一条 `ssh -L ... -L ...` |
| rclone 授权进程莫名自杀 | 探了回调根路径 `http://127.0.0.1:53682/`（无 `code` 即判失败） | 验活只探 `/auth?state=...`，返回 307 即正常 |
| `colab drivemount` 打印链接后必失败 | 见 §3.1 两个致命细节（Windows 无 `/dev/tty`） | 用 `official_mount_propagate.py` 投递，再 `drive.mount()` |
| 内核 cell 卡住、后续 `colab exec` 全部排队 | 内核在等前端消息（`request_auth` / `eval_js`） | 用 `colab console` 绕过内核排查；必要时 `colab restart-kernel` |
| `colab exec` 报 `TimeoutError: Timeout waiting for reply` | **默认超时只有 30s**，而挂载/认证类 cell 动辄几十秒到 3 分钟 | 加 `--timeout 300`（超时后内核仍在跑，别重复执行） |
| `colab ssh`（交互式）报 `CreateProcessW failed error:2` / `posix_spawnp: No such file or directory` | 内部 `shlex.join` 生成 POSIX 单引号+反斜杠的 ProxyCommand，Windows OpenSSH 拉不起来 | **别用 `colab ssh`**，走 §2.1.1：手写 ssh_config + 系统 ssh |
| `colab exec -f <脚本>` 报 `UnicodeDecodeError: 'gbk' codec can't decode ...` | CLI 用系统默认编码（中文 Windows = GBK）`open()` 读 UTF-8 脚本 | 先 `set PYTHONUTF8=1`（cmd）/ `$env:PYTHONUTF8='1'`（PowerShell），让 `open()` 走 UTF-8 |
| 交互式 SSH 挂着时，另一条 `ssh`/`scp` 报 429 | 一个 runtime 只允许**一条** SSH 连接 | 上传/取文件走内核通道（`colab upload` / `colab download`），它们**不占** SSH 位 |
| Kaggle 数据集单流下载慢 | `kaggle datasets download` 是单流 | 用 `colab_ops/parallel_kaggle_dl.py <owner/slug> <dst> [线程]`：签名直链 + Range 分段 + 段内续传 + 403 自动换链；898MB 实测 **132→233 MB/s**（8 线程） |
| `pkill -f <脚本名>` 后"命令无输出、但目标进程还活着" | `pkill -f` 也匹配**执行它的那个 shell**（其 cmdline 含该字符串）→ shell 先自杀，后续命令不执行；而真正的 python 进程 cmdline 里没有脚本名，反而漏网 | 别用 `pkill -f`。改用 `pgrep -af <关键词>` 取 PID → `os.kill(pid, SIGTERM)`，并**排除自身 pid**；关键词选目标 python 进程独有、脚本名不含的串 |

---

## 6. 标准流程骨架（照抄改项目参数即可）

```powershell
$colab = '<venv 目录>\colab.exe'
$py    = '<venv 目录>\python.exe'

# 1) 开会话
& $colab new -s <SESSION> --gpu L4
# 2) 挂 Drive：取 endpoint → 投递（点一次同意）→ 内核挂载
$ep = [regex]::Match((& $colab url -s <SESSION> | Select-Object -Last 1), '/tun/m/([^#\s&]+)').Groups[1].Value
& $py colab_ops\official_mount_propagate.py $ep 900
& $colab exec -s <SESSION> --timeout 300 -f colab_ops\mount_drive.py     # drive.mount('/content/drive')
# 3) 数据/代码就位（Drive 直读太慢——先拷到 /content 本地盘）
#    大文件用 parallel_kaggle_dl.py 或 colab_drive.sh pull；rsync 到 /content 后再跑
# 4) 起任务：交互 ssh（§2.1.1）或 console；常驻进程 setsid nohup（§4）；
#    产物/日志路径放 /content/drive/MyDrive/...（启动前 grep fuse.drive /proc/mounts 硬检查），
#    重要 ckpt 写后读回校验
# 5) 收工
& $colab stop -s <SESSION>
```

---

## 7. 验收清单（每步的期望输出）

| 步骤 | 期望 |
|---|---|
| `colab sessions` | 只列出你在用的会话 |
| `official_mount_propagate.py` | `PROPAGATION_DONE` |
| `drive.mount()` | 打印 `Mounted at /content/drive` |
| `grep fuse.drive /proc/mounts` | `drive /content/drive fuse.drive rw,...` |
| `ls /content/drive/MyDrive` | Drive 顶层目录 |
| 写/读/删测试 | 内容回读一致 |
| 隧道 `curl 127.0.0.1:<port>/health` | `200` |
| rclone（如用） | `config-check` 出配额；`push` 后 `pull` 回 md5 一致 |
