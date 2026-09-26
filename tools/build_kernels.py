#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_kernels.py — 端到端卷积核构建流水线（可复现）

从 aep\\*.aep 收集全部 id2(StudioIr) 的 IR 引用，获取 .enc（本地缓存或 CDN 下载），
用 decrypt_ir 解密，按 libSuperSound2.dll 加载器(0x10048c60)的语义做后处理，
归一成 V4A convolver 可用的 float32/2ch/44.1kHz WAV 写入 kernels\\<hash>.wav。

处理步骤与 DLL 证据（详见 docs/SS2引擎分析.md）：
  1. 解密   : SUPERSOUND2::decrypt_file @0x10052400（先试明文打开，失败才解密，
              见 0x10048d2a/0x10048d66）。明文即裸 RIFF/WAVE 流。
  2. Trim   : 尾部阈值裁剪。threshold = 10^(Trim/20)（0x10048dc1..dd1，pow(10,Trim/20)）；
              仅当 Trim ∈ (-96, -30) dB（0x10048e77 comiss -96 / 0x10048e8b comiss -30）
              且帧数 > 1024（0x10048e8f cmp 0x400）时，从末帧向前找最后一个
              max|x| > threshold 的帧（0x10048ea0..edc，4 声道取 fabs 最大值）截尾。
              只裁尾不裁头。
  3. Fade   : 末尾线性淡出。n = Fade/1000 × 目标采样率（0x10049110..140），
              clamp 到裁剪后帧数，末 n 个样点乘 k/n 斜坡（1→0）。
              仅在 Trim 分支内生效（Trim 关闭时 Fade 不生效）。
  4. 重采样 : 文件采样率 ≠ 目标采样率时重采样（0x10048ee5 比较，SoundTouch，
              失败走线性插值兜底）。目标率分配时默认 0xAC44=44100（0x10049484），
              可被效果/会话率覆盖——此处统一取 44100（V4A DSP 常量同为 44100，
              见 ViPERDSP viper/utils/constants.h）。
  5. Trim/Fade 缺省值：效果对象成员 +0x820/+0x824 在初始化时显式清零
              （0x10049b47 / 0x10049b3d `mov dword ptr [edi+0x820], 0`），
              即 .aep 未携带该参数时 Trim=Fade=0 → 均不在生效区间。
              本语料实测：仅 018/504 显式携带 Trim=-100（≤-96，不生效），
              其余无此参数 —— 全部 IR 的 Trim/Fade 均不激活，使用全长。
  6. 通道归一: 4ch true-stereo [LL,LR,RL,RR] → 取对角 L=ch0, R=ch3
              （逐通道分析：ch0/ch3 有 t=0 直通尖峰、互相关 0.996；
              V4A app utils/WavDecoder.kt:93-95 拒绝 >2ch，DSP Convolver.cpp
              仅支持 1/2 路）。1/2ch 保持不变。
  7. 位深   : 统一转 float32（V4A WavDecoder.kt:96-110 接受 PCM16/24/32 与
              float32；统一 f32 便于 DSP 端处理）。

用法：
  python tools/build_kernels.py                # 全量构建（缓存/下载 .enc 到系统临时目录）
  python tools/build_kernels.py --hash <sha1>  # 只构建指定 kernel
"""
import argparse
import json
import math
import os
import struct
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import decrypt_ir  # noqa: E402
from aep_parser import parse  # noqa: E402

AEPS = os.path.join(ROOT, "aep")
MANIFEST_JSON = os.path.join(AEPS, "recommendbase.json")
KERNELS = os.path.join(ROOT, "kernels")
ENC_CACHE = os.path.join(tempfile.gettempdir(), "qqfx_enc")
CDN = "https://dldir1.qq.com/music/clntupate/ss2/irs/"
TARGET_RATE = 44100
TRIM_LO, TRIM_HI = -96.0, -30.0   # Trim 生效区间 (dB)，SS2 0x10065ecc/0x10065ec8
TRIM_MIN_FRAMES = 1024            # 0x10048e8f cmp 0x400
RESAMPLER_TAPS = 16               # 加窗 sinc 抽头数（近似 SoundTouch 质量）


# ---------- .enc 获取 ----------
def enc_path(sha1):
    """返回 .enc 路径；缓存无则从 CDN 下载（文件名即 sha1）。"""
    p = os.path.join(ENC_CACHE, sha1 + ".enc")
    if os.path.exists(p):
        return p, False
    os.makedirs(ENC_CACHE, exist_ok=True)
    url = CDN + sha1 + ".enc"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    with open(p, "wb") as f:
        f.write(data)
    return p, True


# ---------- WAV 读/写（容忍 pcm16/24/32、float32、EXTENSIBLE） ----------
def read_wav(data):
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE", "not a RIFF/WAVE"
    pos, fmt, chunks = 12, None, {}
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        else:
            chunks[cid] = body
        pos += 8 + size + (size & 1)
    tag, ch, rate = struct.unpack("<HHI", fmt[:8])
    if tag == 0xFFFE and len(fmt) >= 26:          # WAVE_FORMAT_EXTENSIBLE
        tag = struct.unpack("<H", fmt[24:26])[0]
    bits = struct.unpack("<H", fmt[14:16])[0]
    raw = chunks[b"data"]
    if tag == 3 and bits == 32:
        x = struct.unpack("<%df" % (len(raw) // 4), raw)
    elif tag == 1 and bits == 16:
        n = len(raw) // 2
        x = [v / 32768.0 for v in struct.unpack("<%dh" % n, raw)]
    elif tag == 1 and bits == 24:
        x = []
        for i in range(0, len(raw) - 2, 3):
            v = raw[i] | raw[i + 1] << 8 | raw[i + 2] << 16
            x.append((v - (v >> 23 << 24)) / 8388608.0)   # 符号位扩展
    elif tag == 1 and bits == 32:
        n = len(raw) // 4
        x = [v / 2147483648.0 for v in struct.unpack("<%di" % n, raw)]
    else:
        raise ValueError("unsupported wav fmt tag=%s bits=%s" % (tag, bits))
    frames = len(x) // ch
    # 去交错：返回声道数组列表（chans[c] = 第 c 声道的平坦样本序列）
    return [x[c::ch] for c in range(ch)], rate


def write_wav_f32(path, chans, rate):
    n = len(chans[0])
    raw = b"".join(struct.pack("<%df" % len(chans), *row) for row in zip(*chans))
    hdr = b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVEfmt " + \
        struct.pack("<IHHIIHH", 16, 3, len(chans), rate, rate * 4 * len(chans),
                    4 * len(chans), 32) + b"data" + struct.pack("<I", len(raw))
    with open(path, "wb") as f:
        f.write(hdr + raw)


# ---------- Trim / Fade（SS2 0x10048c60 加载器语义） ----------
def apply_trim_fade(chans, rate, trim_db, fade_ms):
    """返回 (处理后 chans, 动作描述)；语义见文件头注释 2/3。"""
    if trim_db is None:
        return chans, "Trim/Fade 未携带（缺省 0，不生效）"
    if not (TRIM_LO < trim_db < TRIM_HI):
        return chans, "Trim=%g 不在 (%g,%g) 区间，不裁剪（Fade 联动不生效）" % (
            trim_db, TRIM_LO, TRIM_HI)
    frames = len(chans[0])
    if frames <= TRIM_MIN_FRAMES:
        return chans, "帧数 %d ≤ %d，不裁剪" % (frames, TRIM_MIN_FRAMES)
    thr = 10.0 ** (trim_db / 20.0)
    last = 0
    for i in range(frames - 1, -1, -1):                     # 从末帧向前
        if any(abs(c[i]) > thr for c in chans):
            last = i
            break
    keep = last + 1
    chans = [c[:keep] for c in chans]
    act = "Trim=%g dB → 阈值 %g，%d→%d 帧" % (trim_db, thr, frames, keep)
    if fade_ms:                                             # Fade 仅在 Trim 分支内
        n = min(int(fade_ms / 1000.0 * rate), keep)
        for c in chans:
            for k in range(n):
                c[keep - n + k] *= k / float(n)             # 线性斜坡 →0
        act += "；Fade=%g ms → 末 %d 样点淡出" % (fade_ms, n)
    return chans, act


# ---------- 重采样（加窗 sinc，近似 SS2 的 SoundTouch 路径） ----------
def resample(chans, rate_in, rate_out):
    if rate_in == rate_out:
        return chans
    import numpy as np
    out_n = int(math.floor(len(chans[0]) * rate_out / rate_in))
    K = RESAMPLER_TAPS
    outs = []
    for c in chans:
        src = np.asarray(c, dtype=np.float64)
        idx = np.arange(out_n) * (rate_in / float(rate_out))
        base = np.floor(idx).astype(np.int64)
        frac = idx - base
        out = np.zeros(out_n)
        for t in range(-K // 2 + 1, K // 2 + 1):
            j = np.clip(base + t, 0, len(src) - 1)
            x = (idx + t) - j                                   # 到采样点的距离
            w = np.sinc(x) * np.sinc(x / (K // 2 + 1.0))        # 加窗 sinc
            out += src[j] * w
        outs.append(out.tolist())
    return outs


def to_2ch(chans):
    """4ch true-stereo [LL,LR,RL,RR] 取对角；1/2ch 原样。"""
    if len(chans) == 4:
        return [chans[0], chans[3]], "4ch → 2ch（对角 L=ch0, R=ch3）"
    if len(chans) in (1, 2):
        return chans, None
    raise ValueError("unexpected channel count %d" % len(chans))


# ---------- 主流程 ----------
def build_one(sha1, trim_db, fade_ms, log):
    os.makedirs(KERNELS, exist_ok=True)
    enc, downloaded = enc_path(sha1)
    raw = open(enc, "rb").read()
    wav = decrypt_ir.decrypt_fast(raw)
    chans, rate = read_wav(wav)
    src_frames, src_ch, src_rate = len(chans[0]), len(chans), rate
    chans, act_tf = apply_trim_fade(chans, rate, trim_db, fade_ms)
    chans, act_ch = to_2ch(chans)
    chans = resample(chans, rate, TARGET_RATE)
    out = os.path.join(KERNELS, sha1 + ".wav")
    write_wav_f32(out, chans, TARGET_RATE)
    log.append({
        "hash": sha1, "downloaded": downloaded,
        "src": "%dch/%dHz/%d帧(%.3fs)" % (src_ch, src_rate, src_frames,
                                          src_frames / float(src_rate)),
        "trim_fade": act_tf, "channels": act_ch or "1/2ch 原样",
        "out": "%d帧(%.3fs) f32/2ch/%dHz" % (len(chans[0]), len(chans[0]) / float(TARGET_RATE), TARGET_RATE),
    })
    return out


def main():
    ap = argparse.ArgumentParser(description="构建 QQ 音效卷积核（解密+SS2 后处理+V4A 归一）")
    ap.add_argument("--hash", help="只构建指定 sha1 的 kernel")
    args = ap.parse_args()

    links = {}
    if os.path.exists(MANIFEST_JSON):
        meta = json.load(open(MANIFEST_JSON, encoding="utf-8"))
        for it in (meta if isinstance(meta, list) else meta.get("effects", meta.get("list", []))):
            if isinstance(it, dict) and it.get("effectIRLinks"):
                links.update(it["effectIRLinks"])

    refs = {}   # sha1 -> (trim, fade)（多文件引用同一 hash 时取首个非缺省值）
    for f in sorted(os.listdir(AEPS)):
        if not f.endswith(".aep"):
            continue
        try:
            d = parse(os.path.join(AEPS, f))
        except Exception:
            continue
        for n in d["nodes"]:
            if n["id"] != 2:
                continue
            ir = os.path.basename(str(n["params"].get("IR File", ""))).rsplit(".", 1)[0]
            if not ir:
                continue
            tr, fd = n["params"].get("Trim"), n["params"].get("Fade")
            cur = refs.setdefault(ir, [None, 0.0])
            if tr is not None and cur[0] is None:
                cur[0], cur[1] = float(tr), float(fd or 0.0)
    if args.hash:
        refs = {args.hash: refs.get(args.hash, [None, 0.0])}

    log = []
    for sha1, (tr, fd) in sorted(refs.items()):
        try:
            build_one(sha1, tr, fd, log)
            mark = "[OK]"
        except Exception as e:
            mark = "[FAIL]"
            log.append({"hash": sha1, "error": str(e)})
        print("%s %s  trim=%s fade=%s" % (mark, sha1[:12], tr, fd))
    for e in log:
        if "error" in e:
            print("    !! %s: %s" % (e["hash"][:12], e["error"]))
        else:
            print("    %s | %s | %s | %s → %s" % (
                e["hash"][:12], e["src"], e["trim_fade"], e["channels"], e["out"]))
    ok = sum(1 for e in log if "error" not in e)
    print("[i] built %d/%d kernels → %s" % (ok, len(refs), KERNELS))
    return 0 if ok == len(refs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
