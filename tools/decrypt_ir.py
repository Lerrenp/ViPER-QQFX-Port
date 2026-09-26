#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解密 QQ 音乐 `.enc` 脉冲响应（IR）文件，输出裸 WAV。

算法来源（纯静态逆向，未运行任何 DLL）
=====================================
DLL: `QQMusic去exe版\\libSuperSound2.dll` (x86, ImageBase 0x10000000)

解码入口 `SUPERSOUND2::decrypt_file(char const* in, char const* out)`
导出地址 VA 0x10052400。它把输入按 **0x80000 (512 KiB) 块**读入，
对每块调用核心异或例程，再把结果写出：

    0x10052550  push 0x80000            ; 块大小 = 0x80000
    0x10052587  push 0x80000            ; fread(buf, 1, 0x80000, fp)
    0x1005258f  call [0x1005f1c8]       ; fread
    0x100525a3  call 0x1001fb70         ; decrypt_block(buf, size)

核心例程 `0x1001fb70`（arg2 = 就地异或的缓冲区 ebx，arg3 = 本块字节数 edi）
逐字节执行（esi = 本块内偏移 i，从 0 起）：

    0x1001fb82  mov  eax, esi
    0x1001fb86  jns  0x1001fb8c
    0x1001fb88  xor  eax, eax                 ; i<0 -> 0（本场景 i>=0，不触发）
    ...
    0x1001fb8c  cmp  esi, 0x7fff
    0x1001fb92  jle  0x1001fbb1               ; i<=0x7fff 时 q=i
    0x1001fb94  mov  eax, 0x80010003          ; 有符号除法魔法数
    0x1001fb99  imul esi                      ; edx:eax = magic*i
    0x1001fb9b  add  edx, esi
    0x1001fb9d  sar  edx, 0xe                 ; edx = i / 0x7fff
    0x1001fba0  mov  eax, edx
    0x1001fba2  shr  eax, 0x1f
    0x1001fba5  add  eax, edx
    0x1001fba7  imul ecx, eax, 0x7fff
    0x1001fbad  mov  eax, esi
    0x1001fbaf  sub  eax, ecx                 ; q = i - (i/0x7fff)*0x7fff = i % 0x7fff
    0x1001fbb1  imul eax, eax                 ; q*q
    0x1001fbb4  add  eax, 0x13c1b             ; + 0x13c1b
    0x1001fbb9  and  eax, 0x800000ff          ; 保留低 8 位（bit31 恒为 0，见下）
    0x1001fbbe  jns  0x1001fbc7               ; 负数时的“符号扩展字节”修正，本数据不触发
    0x1001fbc7  mov  cl, byte ptr [eax + 0x1005f788]   ; 256 字节密钥表
    0x1001fbcd  xor  byte ptr [esi + ebx], cl ; out[i] ^= key[index]
    0x1001fbd0  inc  esi
    0x1001fbd1  cmp  esi, edi
    0x1001fbd3  jl   0x1001fb82

即，对块内偏移 i：

    q        = i                if i <= 0x7fff
             = i % 0x7fff       otherwise          (32 位有符号截断取余)
    index    = (q*q + 0x13c1b) & 0xFF
    plain[i] = enc[i] XOR KEY256[index]

密钥表 KEY256 为 libSuperSound2.dll VA 0x1005f788 处的 256 字节常量。

几个关键点：
* 由于 (i+128)² ≡ i² (mod 256)，在 i ∈ [0, 0x7fff] 区间内 index 只依赖
  i mod 128 —— 所以前 ~32 KiB 的密钥流正好是“128 字节表循环”，这与公开仓库
  `audioeffect-qm/qmae/decrypt.py` 观察到的现象一致；其 128 字节硬编码表正是
  KEY256[(i²+0x13c1b)&0xFF] 在 i=0..127 的取值。
* i 超过 0x7fff 后改按 i mod 0x7fff 取 q，密钥流相位随之漂移 —— 这正是该仓库
  长文件“0x8000 之后损坏”的根因（它只是把 128 字节表无限循环，缺少 mod 0x7fff 的
  二次取模与 0x80000 分块重置）。
* 分块大小 0x80000：i 每块从 0 重新计数。
"""

from __future__ import annotations

import argparse
import os
import sys

# libSuperSound2.dll @ VA 0x1005f788，共 256 字节
KEY256 = bytes.fromhex(
    "77483273def2c0c895ec30b251c3e1a0"
    "9ee69dcffa7f14d1ceb8dcc34a6793d6"
    "28c29170ca8da2a4f00861907e6fa2e0"
    "ebae3eb667c792f491b5f66c5e8440f7"
    "f31b027fd5ab418928f425cc5211ad43"
    "68a6418b84b5ff2c924a26d8476a7c95"
    "61cce6cbbb3f47588975c375a1d9afcc"
    "087317dcaa9aa21641d8a206c68bfc66"
    "349fcf1823a00a74e72b277092e9af37"
    "e68ca7bc62659cc208c988b3f343ac74"
    "2c0fd4afa1c30164954e489ff4357895"
    "7a39d66aa06d40e84fa8ef111df31b3f"
    "3f07dd6f5b193019fbef0e37f00ecd16"
    "49fe5347131abda4f14019600eed6809"
    "065f4dcf3d1afe2077e4d9daf9a42b76"
    "1c71db00bcfd0c6ca547f7f600794a11"
)

BLOCK = 0x80000  # 0x10052550 push 0x80000
MOD = 0x7FFF     # 0x1001fb8c cmp esi, 0x7fff
ADD = 0x13C1B    # 0x1001fbb4 add eax, 0x13c1b


def _q(i: int) -> int:
    """块内偏移 i -> 取模后的 q（对应 0x1001fb82~0x1001fbaf）。"""
    if i < 0:
        return 0
    if i <= MOD:
        return i
    return i % MOD


def _key_index(i: int) -> int:
    """块内偏移 i -> KEY256 下标（对应 0x1001fbb1~0x1001fbc7）。"""
    q = _q(i)
    v = (q * q + ADD) & 0x800000FF
    # q <= 0x7ffe 时 q²+0x13c1b < 2³¹，bit31 不会置位，v 即低 8 位。
    # 若极端情况 bit31 置位，x86 的“符号扩展”分支会把索引变成负地址；
    # 这里按代码语义取低 8 位即可（本算法在真实数据中不触发）。
    return v & 0xFF


# 预计算一个块内的密钥流，加速
_BLOCK_KS = bytes(KEY256[_key_index(i)] for i in range(BLOCK))


def keystream(n: int) -> bytes:
    """生成长度 n 的完整密钥流（按 0x80000 分块重置）。"""
    out = bytearray()
    full, rem = divmod(n, BLOCK)
    out += _BLOCK_KS * full
    out += _BLOCK_KS[:rem]
    return bytes(out)


def decrypt(data: bytes) -> bytes:
    """就地语义的纯函数版本：返回解密后的字节。"""
    ks = keystream(len(data))
    return bytes(a ^ b for a, b in zip(data, ks))


def decrypt_fast(data: bytes) -> bytes:
    """使用 int XOR 加速（无需第三方依赖）。"""
    n = len(data)
    full, rem = divmod(n, BLOCK)
    out = bytearray()
    i = 0
    blk = int.from_bytes(_BLOCK_KS, "big")
    for _ in range(full):
        chunk = int.from_bytes(data[i:i + BLOCK], "big") ^ blk
        out += chunk.to_bytes(BLOCK, "big")
        i += BLOCK
    if rem:
        chunk = int.from_bytes(data[i:i + rem], "big") ^ (blk >> (8 * (BLOCK - rem)))
        out += chunk.to_bytes(rem, "big")
    return bytes(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="解密 QQ 音乐 .enc 脉冲响应（IR）")
    ap.add_argument("--input", "-i", required=True, help="输入 .enc 路径")
    ap.add_argument("--output", "-o", default="", help="输出 WAV 路径（默认同目录同名 .wav）")
    args = ap.parse_args(argv)

    src = os.path.abspath(args.input)
    if not os.path.exists(src):
        print("输入不存在: %s" % src, file=sys.stderr)
        return 2
    dst = args.output or os.path.splitext(src)[0] + ".wav"
    dst = os.path.abspath(dst)

    with open(src, "rb") as f:
        data = f.read()
    plain = decrypt_fast(data)

    with open(dst, "wb") as f:
        f.write(plain)

    kind = "WAV" if plain[:4] == b"RIFF" else "raw"
    print("%s -> %s  (%d bytes, %s)" % (src, dst, len(plain), kind))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
