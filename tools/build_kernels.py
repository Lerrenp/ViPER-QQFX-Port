#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_kernels.py — 端到端卷积核/采样素材构建流水线（可复现，双速率 + 可读命名）

从 aep\\*.aep 收集 id2(StudioIr, "IR File") 与 id7(Sampler, "Audio File") 的音频引用，
获取 .enc（本地缓存或 CDN 下载），用 decrypt_ir 解密，按 libSuperSound2.dll 加载器
(0x10048c60) 的语义做后处理，输出两套采样率版本：

    kernels/441/<名>_441.wav   (44100 Hz)
    kernels/48k/<名>_48k.wav   (48000 Hz)

<名> 从语料自动推导：卷积核用引用它的效果名（多效果共享则 "_" 连接；同效果多个
id2 时被覆盖的前者加 "_备选"）；id7 采样素材加 "采样素材_" 前缀。hash↔名字对照
写入 kernels/kernel_index.json（convert_aep.py 由此把预设的 kernelFile 指向可读名）。

处理步骤与 DLL 证据（详见 docs/SS2引擎分析.md）：
  1. 解密   : SUPERSOUND2::decrypt_file @0x10052400（先试明文打开，失败才解密，
              见 0x10048d2a/0x10048d66）。明文即裸 RIFF/WAVE 流。
  2. Trim   : 尾部阈值裁剪。threshold = 10^(Trim/20)（0x10048dc1..dd1）；
              仅当 Trim ∈ (-96, -30) dB（0x10048e77/0x10048e8b）且帧数 > 1024
              （0x10048e8f）时，从末帧向前找最后一个 max|x| > threshold 的帧截尾。
  3. Fade   : 末尾线性淡出。n = Fade/1000 × 采样率（0x10049110..140），
              仅在 Trim 分支内生效。
  4. Trim/Fade 缺省值：效果对象成员 +0x820/+0x824 初始化显式清零
              （0x10049b47 / 0x10049b3d）→ .aep 未携带时 Trim=Fade=0，不生效。
              本语料实测全部不激活（仅 018/504 显式 Trim=-100，亦在区间外）。
  5. 重采样 : 原生采样率即 .enc 内 WAV 头声明率（11/12 为 44100，018 为 48000，
              帧数多为 2 的幂，系按 44.1k 制作）。双版本各自从原生率重采样
              （加窗 sinc，近似 SS2 的 SoundTouch 路径 0x10048fc3），原生匹配的
              版本直接透传不经重采样。
  6. 通道归一: 4ch true-stereo [LL,LR,RL,RR] → 取对角 L=ch0, R=ch3
              （V4A app utils/WavDecoder.kt:93-95 拒绝 >2ch；DSP Convolver.cpp
              仅支持 1/2 路）。位深统一 float32。

用法：
  python tools/build_kernels.py                # 全量构建两套版本
  python tools/build_kernels.py --hash <sha1>  # 只构建指定 kernel
"""
import argparse
import json
import math
import os
import re
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
INDEX_JSON = os.path.join(KERNELS, "kernel_index.json")
ENC_CACHE = os.path.join(tempfile.gettempdir(), "qqfx_enc")
CDN = "https://dldir1.qq.com/music/clntupate/ss2/irs/"
RATES = {"441": 44100, "48k": 48000}
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
    # 去交错：返回声道数组列表（chans[c] = 第 c 声道的平坦样本序列）
    return [list(x[c::ch]) for c in range(ch)], rate


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


# ---------- 语料引用收集与命名 ----------
def _clean(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(name)).strip("_") or "未命名"


def collect_refs():
    """遍历语料，返回 (refs, naming)。

    refs:  sha1 -> {"trim": float|None, "fade": float, "roles": set}
    naming: sha1 -> base 名（可读命名，见文件头说明）
    """
    order = []                       # 语料文件顺序（用于共享名连接与冲突消解）
    id2_use = {}                     # sha1 -> [(effect, active?), ...]
    id7_use = {}                     # sha1 -> [effect, ...]
    for f in sorted(os.listdir(AEPS)):
        if not f.endswith(".aep"):
            continue
        try:
            d = parse(os.path.join(AEPS, f))
        except Exception:
            continue
        effect = _clean(d.get("name") or os.path.basename(f)[:-4])
        order.append(effect)
        id2_nodes = [n for n in d["nodes"] if n["id"] == 2]
        for i, n in enumerate(id2_nodes):
            ir = os.path.basename(str(n["params"].get("IR File", ""))).rsplit(".", 1)[0]
            if not ir:
                continue
            # 同效果多个 id2：后者覆盖前者（V4A convolver 单级），前者为"备选"
            id2_use.setdefault(ir, []).append((effect, i == len(id2_nodes) - 1))
        for n in d["nodes"]:
            if n["id"] == 7:
                au = os.path.basename(str(n["params"].get("Audio File", ""))).rsplit(".", 1)[0]
                if au:
                    id7_use.setdefault(au, []).append(effect)

    refs, naming = {}, {}
    for ir, uses in id2_use.items():
        refs[ir] = {"trim": None, "fade": 0.0, "roles": ["convolver"]}
        names = [effect if active else effect + "_备选" for effect, active in uses]
        naming[ir] = "_".join(dict.fromkeys(names))    # 去重保序
    for au, effects in id7_use.items():
        refs[au] = {"trim": None, "fade": 0.0, "roles": ["sampler"]}
        naming[au] = "采样素材_" + "_".join(dict.fromkeys(effects))
    return refs, naming


def _trim_fade_from_corpus(refs):
    """从 .aep 参数补 Trim/Fade（refs 只带 role；这里按 hash 重扫取值）。"""
    out = {}
    for f in sorted(os.listdir(AEPS)):
        if not f.endswith(".aep"):
            continue
        try:
            d = parse(os.path.join(AEPS, f))
        except Exception:
            continue
        for n in d["nodes"]:
            for key, dst in ((2, "ir"), (7, "au")):
                if n["id"] != key:
                    continue
                pname = "IR File" if key == 2 else "Audio File"
                sha = os.path.basename(str(n["params"].get(pname, ""))).rsplit(".", 1)[0]
                if not sha:
                    continue
                tr, fd = n["params"].get("Trim"), n["params"].get("Fade", 0.0)
                cur = out.setdefault(sha, [None, 0.0])
                if tr is not None and cur[0] is None:
                    cur[0], cur[1] = float(tr), float(fd)
    return out


# ---------- 主流程 ----------
def build_one(sha1, trim_db, fade_ms, base_name, log):
    for sub in RATES:
        os.makedirs(os.path.join(KERNELS, sub), exist_ok=True)
    enc, downloaded = enc_path(sha1)
    raw = open(enc, "rb").read()
    wav = decrypt_ir.decrypt_fast(raw)
    chans, native_rate = read_wav(wav)
    src_desc = "%dch/%dHz/%d帧(%.3fs)" % (len(chans), native_rate, len(chans[0]),
                                          len(chans[0]) / float(native_rate))
    chans, act_tf = apply_trim_fade(chans, native_rate, trim_db, fade_ms)
    chans, act_ch = to_2ch(chans)
    files = {}
    for tag, rate in RATES.items():
        out_chans = resample(chans, native_rate, rate)   # 原生匹配时透传
        out = os.path.join(KERNELS, tag, "%s_%s.wav" % (base_name, tag))
        write_wav_f32(out, out_chans, rate)
        files[tag] = "%s_%s.wav" % (base_name, tag)
    log.append({
        "hash": sha1, "name": base_name, "src": src_desc,
        "trim_fade": act_tf, "channels": act_ch or "1/2ch 原样",
        "out": {t: "%.3fs" % (len(chans[0]) * (RATES[t] / native_rate if RATES[t] != native_rate else 1) / RATES[t]) if RATES[t] != native_rate else "%.3fs" % (len(chans[0]) / float(native_rate)) for t in RATES},
        "downloaded": downloaded,
    })
    return files


def main():
    ap = argparse.ArgumentParser(description="构建 QQ 音效卷积核/采样素材（解密+SS2 后处理+双速率+可读命名）")
    ap.add_argument("--hash", help="只构建指定 sha1")
    args = ap.parse_args()

    refs, naming = collect_refs()
    tf = _trim_fade_from_corpus(refs)
    if args.hash:
        refs = {args.hash: refs.get(args.hash, {"roles": ["convolver"]})}
        naming.setdefault(args.hash, args.hash)

    log, index = [], {}
    for sha1 in sorted(refs):
        base = naming.get(sha1, sha1)
        tr, fd = tf.get(sha1, [None, 0.0])
        try:
            files = build_one(sha1, tr, fd, base, log)
            print("[OK] %-14s %s  trim=%s fade=%s" % (sha1[:12], base, tr, fd))
            index[sha1] = {"name": base, "files": files,
                           "roles": refs[sha1]["roles"]}
        except Exception as e:
            print("[FAIL] %-14s %s: %s" % (sha1[:12], base, e))
            index[sha1] = {"name": base, "error": str(e),
                           "roles": refs[sha1]["roles"]}
    for e in log:
        print("    %-14s | %s | %s | %s → %s" % (
            e["hash"][:12], e["src"], e["trim_fade"], e["channels"],
            " / ".join("%s:%s" % (t, s) for t, s in e["out"].items())))
    os.makedirs(KERNELS, exist_ok=True)
    with open(INDEX_JSON, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("[i] index → %s" % INDEX_JSON)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
