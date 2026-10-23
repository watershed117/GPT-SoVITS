# -*- coding: utf-8 -*-
"""batch 显存探针:合成数据跑一次完整 fwd+bwd,打印各 bs 的真实峰值显存。

用法(VM, 训练进程不在跑时):
  python3 probe_bs_mem.py 8 10 12 14

判读:峰值 < 20.0 GiB 视为安全(卡可用 22.03 GiB,留 2 GiB 余量)。
按 bs 升序测,遇到第一个 OOM 即停(避免 OOM 后的脏状态影响后续读数)。
"""
import sys

import torch

sys.path.insert(0, "/content/stage1_code")
from qwen_loader import load_tokenizer_model          # noqa: E402
from rev4_codec import Rev4Codec                      # noqa: E402

DEV = "cuda:0"
FRAME = 1920          # 12.5Hz → 24k 每帧样本数
FRAMES = 48           # 与训练一致
SEM_VOCAB = 1024
SPK_DIM = 192

sizes = [int(a) for a in sys.argv[1:] if a.isdigit()]   # colab exec 会混入自己的参数,只认数字
if not sizes:
    sizes = [8, 10, 12, 14]

Cls = load_tokenizer_model()
tok = Cls.from_pretrained("Qwen/Qwen3-TTS-Tokenizer-12Hz").eval().to(DEV)
torch.manual_seed(0)
model = Rev4Codec(tok, sem_vocab=SEM_VOCAB, freeze_encoder=True).to(DEV)
trainable = [p for p in model.parameters() if p.requires_grad]
opt = torch.optim.AdamW(trainable, lr=3e-4)
print("[probe] 模型就绪 | 可训 %.1fM" % (sum(p.numel() for p in trainable) / 1e6), flush=True)

for bs in sizes:
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    wav = torch.randn(bs, FRAMES * FRAME, device=DEV)
    sem = torch.randint(0, SEM_VOCAB, (bs, FRAMES), device=DEV)
    spk = torch.randn(bs, SPK_DIM, device=DEV)
    try:
        with torch.autocast("cuda", dtype=torch.bfloat16):
            out = model(wav, sem, spk, FRAMES)
            # 与训练同构的损失面:所有可反传输出都参与(sum of mean-square)
            loss = sum(v.float().pow(2).mean() for v in out.values()
                       if torch.is_tensor(v) and v.requires_grad)
        loss.backward()
        opt.step()
        opt.zero_grad(set_to_none=True)
        peak = torch.cuda.max_memory_allocated() / 2**30
        verdict = "安全" if peak < 20.0 else "危险(<2GiB余量)" if peak < 21.0 else "不可用"
        print("[probe] bs=%2d  peak %.2f GiB  %s" % (bs, peak, verdict), flush=True)
    except torch.OutOfMemoryError as e:
        msg = str(e).split("Tried to allocate")[-1][:60]
        print("[probe] bs=%2d  OOM %s" % (bs, msg), flush=True)
        print("[probe] 停止(后续更大 bs 不再测)", flush=True)
        break
    finally:
        del wav, sem, spk
        torch.cuda.empty_cache()
print("[probe] done", flush=True)