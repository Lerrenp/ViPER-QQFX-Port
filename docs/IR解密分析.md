# QQ 音乐 `.enc` 脉冲响应（IR）解密分析

> 纯静态分析：仅用 pefile + capstone 读取 DLL 字节，**未运行、未加载任何 DLL/EXE**。
> 所有反汇编结论均附文件名 + VA + 指令摘录。

## 0. 结论

**能完全解密。** `.enc` 是「裸 WAV 明文 ⊕ 密钥流」的 XOR 流密码；密钥流由
`libSuperSound2.dll` 中一个二次取模公式查一张 256 字节常量表生成，按 0x80000 字节分块。
公开仓库 `audioeffect-qm/qmae/decrypt.py` 只截取了公式在 `i<0x8000` 时的「128 字节循环」
近似，故长文件后段损坏；本文给出完整算法后，本地及参考仓库全部浮点 IR 均 0 损坏。
并已据此从 CDN 补齐全部缺失 `.enc`，产出 **14 个 kernel（全部通过校验，0 失败）**，
使 **12 个预设启用 `convolver`**（见 §7）。

## 1. 明文/容器格式

- `.enc` **不是** QMAE 容器（无 `QMAE` 魔数）。XOR 不改变长度，解密后即是裸 WAV：
  本地唯一文件 `742156792c2f7c863b9d5cec1cd0622546a1f877.enc` 解密后为
  `RIFF/WAVE`，`fmt = (IEEE_FLOAT, 4ch, 44100Hz, blockAlign 16, 32-bit)`，
  含一个 `junk(52B)` 块，`data` 负载从文件偏移 **0x68** 开始。
- 头部 `RIFF`+size+`WAVE`+`fmt `+... 与「文件偏移 0x0 处开始逐字节 XOR」一致
  （即头部也参与加密），故密钥流第 0 字节 = `0x91^'R' = 0xC3`。

## 2. 加密代码定位

| 项 | 值 |
|---|---|
| DLL | `QQMusic去exe版/libSuperSound2.dll`（x86, ImageBase 0x10000000, 543624 B） |
| 导出函数 | `SUPERSOUND2::decrypt_file(char const*, char const*)` @ **VA 0x10052400** |
| 分块大小 | **0x80000**（512 KiB） |
| 核心异或例程 | **VA 0x1001fb70** |
| 密钥表 | **VA 0x1005f788**（256 字节常量） |

### 2.1 `decrypt_file` @ 0x10052400（分块读/写）

```
10052550  push 0x80000              ; 块大小 = 0x80000
10052587  push 0x80000              ; fread(buf, 1, 0x80000, fp)
1005258f  call dword ptr [0x1005f1c8]   ; fread -> eax = 本块字节数(edi)
1005259e  nop
100525a0  push edi                  ; arg3 = size
100525a1  push esi                  ; arg2 = 本块缓冲区
100525a2  push ecx                  ; arg1
100525a3  call 0x1001fb70           ; decrypt_block(buf, size)：就地 XOR
```

即：`for each 0x80000-byte block: decrypt_block(block, len)`；块内偏移 `i` 每块从 0 重新计数。

### 2.2 `decrypt_block` @ 0x1001fb70（核心逐字节 XOR）

寄存器：`esi` = 块内偏移 `i`，`edi` = 本块长度，`ebx` = 缓冲区。

```
1001fb82  8bc6        mov  eax, esi              ; eax = i
1001fb84  85f6        test esi, esi
1001fb86  7904        jns  0x1001fb8c            ; i>=0（本场景恒真）
1001fb88  33c0        xor  eax, eax              ; i<0 -> 0
1001fb8a  eb25        jmp  0x1001fbb1
1001fb8c  81feff7f0000 cmp  esi, 0x7fff
1001fb92  7e1d        jle  0x1001fbb1            ; i<=0x7fff 时 q = i
1001fb94  b803000180  mov  eax, 0x80010003       ; 有符号除法魔法数（除以 0x7fff, 移 14）
1001fb99  f7ee        imul esi                   ; edx:eax = magic * i
1001fb9b  03d6        add  edx, esi
1001fb9d  c1fa0e      sar  edx, 0xe
1001fba0  8bc2        mov  eax, edx
1001fba2  c1e81f      shr  eax, 0x1f
1001fba5  03c2        add  eax, edx
1001fba7  69c8ff7f0000 imul ecx, eax, 0x7fff
1001fbad  8bc6        mov  eax, esi
1001fbaf  2bc1        sub  eax, ecx              ; q = i - (i/0x7fff)*0x7fff = i % 0x7fff
1001fbb1  0fafc0      imul eax, eax              ; q*q
1001fbb4  051b3c0100  add  eax, 0x13c1b          ; + 0x13c1b
1001fbb9  25ff000080  and  eax, 0x800000ff       ; 取低 8 位（bit31 恒为 0）
1001fbbe  7907        jns  0x1001fbc7            ; 负数分支不触发
1001fbc0  48          dec  eax
1001fbc1  0d00ffffff  or   eax, 0xffffff00
1001fbc6  40          inc  eax                   ; 符号扩展字节（死代码）
1001fbc7  8a8888f70510 mov  cl, byte ptr [eax + 0x1005f788]   ; KEY256[index]
1001fbcd  300c1e      xor  byte ptr [esi + ebx], cl            ; buf[i] ^= KEY256[index]
1001fbd0  46          inc  esi
1001fbd1  3bf7        cmp  esi, edi
1001fbd3  7cad        jl   0x1001fb82
```

**算法公式**（`i` 为块内 0 基偏移）：

```
q     = i              , i <= 0x7fff
      = i mod 0x7fff   , i >  0x7fff
index = (q*q + 0x13c1b) & 0xFF
plain[i] = enc[i] XOR KEY256[index]
```

说明：`q <= 0x7ffe` 时 `q²+0x13c1b ≤ 0x3FFF3F1F < 2³¹`，`and 0x800000ff` 的 bit31 永不置位，
故实际索引就是低 8 位；`jns` 后的符号修正分支为死代码。

## 3. 密钥表 KEY256（VA 0x1005f788，256 字节）

```
77 48 32 73 de f2 c0 c8 95 ec 30 b2 51 c3 e1 a0 9e e6 9d cf fa 7f 14 d1
ce b8 dc c3 4a 67 93 d6 28 c2 91 70 ca 8d a2 a4 f0 08 61 90 7e 6f a2 e0
eb ae 3e b6 67 c7 92 f4 91 b5 f6 6c 5e 84 40 f7 f3 1b 02 7f d5 ab 41 89
28 f4 25 cc 52 11 ad 43 68 a6 41 8b 84 b5 ff 2c 92 4a 26 d8 47 6a 7c 95
61 cc e6 cb bb 3f 47 58 89 75 c3 75 a1 d9 af cc 08 73 17 dc aa 9a a2 16
41 d8 a2 06 c6 8b fc 66 34 9f cf 18 23 a0 0a 74 e7 2b 27 70 92 e9 af 37
e6 8c a7 bc 62 65 9c c2 08 c9 88 b3 f3 43 ac 74 2c 0f d4 af a1 c3 01 64
95 4e 48 9f f4 35 78 95 7a 39 d6 6a a0 6d 40 e8 4f a8 ef 11 1d f3 1b 3f
3f 07 dd 6f 5b 19 30 19 fb ef 0e 37 f0 0e cd 16 49 fe 53 47 13 1a bd a4
f1 40 19 60 0e ed 68 09 06 5f 4d cf 3d 1a fe 20 77 e4 d9 da f9 a4 2b 76
1c 71 db 00 bc fd 0c 6c a5 47 f7 f6 00 79 4a 11
```

（也已内嵌于 `tools/decrypt_ir.py` 的 `KEY256`。）

## 4. 为什么公开的「128 字节表」会在 0x8000 之后损坏

`index = (q²+0x13c1b) & 0xFF`，而 `(i+128)² ≡ i² (mod 256)`，因此在 `i ∈ [0,0x7fff]`
区间内 `index` 只依赖 `i mod 128` —— 前 ~32 KiB 的密钥流恰好表现为「128 字节表循环」。
公开仓库 `audioeffect-qm` 的 128 字节硬编码表正是 `KEY256[(i²+0x13c1b)&0xFF]` 在
`i = 0..127` 的取值（本地已验证：按本算法生成的前 128 字节密钥流与该表逐字节相同）。

- `i > 0x7fff` 后改用 `q = i mod 0x7fff`，相位漂移（`0x7fff mod 256 = 255 ≡ −1`，故每跨越
  0x7fff 字节相位约 +1）——这就是原作者看到「0x8000 之后 44% 采样 NaN/|x|>4」的根因。
- 另一维：`i` 每 0x80000 字节分块重置；公开算法缺此重置。

## 5. 验证数据（本地唯一 `.enc`，对应 `504-现场律动`）

输入：`QQMusic去exe版/resae/irs/742156792c2f7c863b9d5cec1cd0622546a1f877.enc`（1,570,744 B）
输出：`kernels/742156792c2f7c863b9d5cec1cd0622546a1f877.wav`（1,570,744 B）
（注：现经 `tools/build_kernels.py` 构建为双速率可读名 `kernels/441/现场律动_441.wav`、
`kernels/48k/现场律动_48k.wav`，见 §7.1 与 `kernels/kernel_index.json`。）

| 判据 | 结果 |
|---|---|
| (a) 前 0x8000 字节与旧「128B 表循环」输出逐字节一致 | **True** |
| (b) float 样本总数 / 非有限值 / `\|x\|>8` / `max\|x\|` | 392660 / **0** / **0** / **0.999999881** |
| (c) WAV 声明 `data` 长度 vs 实际负载 | 1,570,640 B == 1,570,640 B（一致） |
| (c) 格式与时长 | 4ch / 32-bit float / 44100 Hz，2.226 s |
| (c) RMS 包络（每 0.1 s，前 4 段 / 尾 3 段） | `0.010931, 0.001082, 0.000604, 0.000357 … 5e-07, 3e-07, 2e-07`（单调衰减） |
| 输出 md5 / sha1 | `08005422fd94ac9a65a1c85771bf3a84` / `de1c732b0b36cb1ab303316d5ab835426c9ee49f` |

参考仓库 `audioeffect-qm` 全部 19 个 `.enc` 用本算法解密后，全部浮点 IR
（001/007/008/009/010/012-2/013/015/017/504，以及同内容的 0045）**0 个非有限值/0 个 `|x|>8`**；
其余为 16-bit/24-bit PCM（004/011/012-1/014/018）。公开算法（128B 表循环）对其中
001/007/008/010/015/017/504 均产生数万级损坏样本——对比即证完整算法正确。

## 6. 解密器

```bash
python tools/decrypt_ir.py -i <input.enc> -o <output.wav>
```

`tools/decrypt_ir.py` 内嵌 KEY256 与完整公式（含 0x80000 分块、`i mod 0x7fff`），
仅用标准库，无第三方依赖。解密后运行 `python tools/batch_convert.py`，
引用该 IR 的预设会自动启用 `convolver.kernelFile`。

## 7. 其余缺失 `.enc` 的补齐路径

本地语料库仅含 1 个 `.enc`。语料中 **12 个 id2 预设**（共 **11 个不同 hash**）引用的 `.enc`
实体不在本地。这些文件的下载直链已存在于语料元数据 `aep/recommendbase.json` 的
`effectIRLinks[]`（字段 `irFileName` / `irLink`），CDN 为腾讯 `dldir1.qq.com`。按需下载即可：

| irFileName | irLink |
|---|---|
| 2f823376fa7e63ef5a9b70f7443c19793811bb0e.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/2f823376fa7e63ef5a9b70f7443c19793811bb0e.enc |
| 36af30f920f0e3f00232ff24b1b7847b2c006198.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/36af30f920f0e3f00232ff24b1b7847b2c006198.enc |
| 43e714f5c19994239908b83b3c494225079b7605.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/43e714f5c19994239908b83b3c494225079b7605.enc |
| 4d11b1cd25bd08e131e79b2b1736b71c6f18828b.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/4d11b1cd25bd08e131e79b2b1736b71c6f18828b.enc |
| 6f7ed6744ad0f47c6bf73d001b65c1531b47a388.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/6f7ed6744ad0f47c6bf73d001b65c1531b47a388.enc |
| 8da782aafdc5174e9b8e838c57e2a31d2db9f6a3.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/8da782aafdc5174e9b8e838c57e2a31d2db9f6a3.enc |
| b7c9156c947c4af96e2edde9abf58b3a1a29e932.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/b7c9156c947c4af96e2edde9abf58b3a1a29e932.enc |
| c2e6f77802083d99658e46126b64fd0eeb2a12f2.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/c2e6f77802083d99658e46126b64fd0eeb2a12f2.enc |
| d8d31861ef720e7ebe5d30fad4ba2dea68bbee41.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/d8d31861ef720e7ebe5d30fad4ba2dea68bbee41.enc |
| f11a9f4cf5aacc0ee807a14427ac63cd65841767.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/f11a9f4cf5aacc0ee807a14427ac63cd65841767.enc |
| fae6b7a7ebcd6c4bfda2ff6807f0a5ca71048be0.enc | https://dldir1.qq.com/music/clntupate/ss2/irs/fae6b7a7ebcd6c4bfda2ff6807f0a5ca71048be0.enc |

下载后同样用：

```bash
python tools/decrypt_ir.py -i <hash>.enc -o kernels/<hash>.wav
python tools/batch_convert.py
```

### 7.1 补齐结果（已完成：14/14 下载并解密，0 失败）

14 个 `.enc` 直链全部可下载（无 404）。每个解密后按硬判据校验，全部通过并写入 `kernels/`
（现为 `kernels/441/` 与 `kernels/48k/` 双速率、可读命名，hash↔名字对照见 `kernels/kernel_index.json`）：

| hash（.wav） | 调用方 | 格式 | 时长 | 校验 |
|---|---|---|---|---|
| `8da782aa…b9f6a3` | id2 → 001-差分环绕 | float32 / 2ch / 44.1k | 0.406 s | OK（peak 1.12，含尾部 `SyLp` 块） |
| `2f823376…811bb0` | id2 → 007-录音棚环绕 | float32 / 2ch / 44.1k | 0.186 s | OK |
| `36af30f9…c006198` | id2 → 008-小编制 | float32 / 2ch / 44.1k | 0.371 s | OK |
| `43e714f5…9b7605` | id2 → 009-民谣 | float32 / 2ch / 44.1k | 0.001 s | OK |
| `4d11b1cd…18828b` | id2 → 010-流动低音 | float32 / 2ch / 44.1k | 0.371 s | OK |
| `6f7ed674…47a388` | id2 → 011-现场环绕 | pcm16 / 2ch / 44.1k | 0.743 s | OK |
| `d8d31861…bbee41` | id2 → 012-复合低音、013-超高保真 | float32 / 2ch / 44.1k | 0.012 s | OK |
| `b7c9156c…a29e932` | （012 另一 IR，未被 convolver 选中） | pcm32 / 2ch / 44.1k | 0.023 s | OK |
| `f11a9f4c…841767` | id2 → 015-清澈旋律 | float32 / 2ch / 44.1k | 0.371 s | OK |
| `c2e6f778…a12f2` | id2 → 017-震撼低音 | float32 / 2ch / 44.1k | 0.093 s | OK |
| `fae6b7a7…48be0` | id2 → 018-极重低音 | pcm24 / 2ch / 48k | 0.044 s | OK |
| `74215679…6a1f877` | id2 → 504-现场律动 | float32 / 4ch / 44.1k | 2.226 s | OK（见 §5） |
| `679a81d9…a622fa` | id7 `Sampler`（004/014，非卷积） | pcm16 / 2ch / 44.1k | 2.439 s | OK |
| `de36ef18…fc5f7` | id7 `Sampler`（014，非卷积） | pcm16 / 2ch / 44.1k | 5.647 s | OK |

校验口径：解密输出为合法 RIFF/WAVE 且 `data` 长度可解析不越界；float32 全部有限、`|x|≤8`；
无 RMS 满幅段（分段 >0.9）。所有文件均满足，**0 失败**，故全部产出 kernel、无跳过。

> 说明：`采样素材_黑胶_留声机`（`679a…`）/`采样素材_黑胶_留声机_2`（`de36ef…`）是 id7
> `Sampler`（采样器）的音乐素材，不是卷积 IR，但同属 `.enc` 加密封装，解密校验通过后一并保留；
> `b7c9156c…`（`复合低音_备选`）是 012 的第二条 IR，V4A 单级 convolver 下**后者覆盖前者**，
> 012 实际生效的是 `d8d31861…`（`复合低音_超高保真`），`b7c9156c…` 仅作备选。
> `.enc` 原文件仅存系统临时目录，未纳入仓库。

### 7.2 最终结果

- kernel 总数：**14**（全部校验通过，0 失败）。
- `convolver` 启用预设数：**12**（001/007/008/009/010/011/012/013/015/017/018/504）。
- 批量转码：`presets` **36** 个，`validate_preset.py` 全量 **37/37 pass**（36 生成 + 1 手工），
  `manifest.json` 校验 `pass 36 / fail 0`。
  （注：为当时快照；当前为 `presets` **35** 个、校验 35/35 pass、`manifest` pass 35 / fail 0 / skipped 16。）

> 注：元数据中另有 4 个 `.irs` 链接（`dlied5sdk.myapp.com/...t_sound_recommendEffectBase/`），
> 是另一类未加密 IR 资产，不在 `.enc` 范畴。

## 8. 版权

`.enc`/IR 内容版权归腾讯（QQ 音乐）所有，本文与解密器仅用于**个人学习 / 互操作性研究**，
请勿商用或再分发。
