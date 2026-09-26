# QQ 音乐 SS2 音效引擎（libSuperSound2.dll）静态分析

> 纯静态分析：仅用 objdump / strings / pefile + capstone 读取 PE 字节与反汇编，**未运行/未加载任何 DLL**。
> 目标：`D:\AI-Agent\ZCode\workspace\QQMusic去exe版\libSuperSound2.dll`（x86, 543,624 B, ImageBase 0x10000000, 落盘时间 2023-08-30）。
> 所有结论均附「VA + 反汇编摘录 / 导出表条目」；不确定处显式标注。
> 术语约定：VA = ImageBase + RVA；文中 VA 均为加载假定基址 0x10000000 下的绝对虚拟地址。

---

## 0. 结论速览

1. **导出面**：`libSuperSound2.dll` 共 **94 个导出**，几乎全是 `SUPERSOUND2::` 命名空间。分三类：
   - **引擎 API**：`supersound_init/uninit`、`supersound_create_effect`、`supersound_processf_input/output`、`supersound_set_samplerate` 等（40+ 个 `supersound_*`）；
   - **WAV 读写类 `WaveFile`**：`SetFilePathUTF8` / `ReadFrames` / `WriteFrames` / `GetSampleRate` / `TimeToFrames` 等（30+ 个）；
   - **工具函数**：`decrypt_file`、`dB2scale`/`scale2dB`、`norm_center`/`denorm_center`、`df2i`/`i2df`、`lock_modulators` 等。
   - **导入面**：**只导入 `IPPWrapper.dll` 的 8 个 FFT 原语**（`CreateFFTInst/InitFFT/FFT/IFFT/InitFFTC/FFTC/IFFTC/DestroyFFTInst`）+ CRT。→ **SS2 自身不做 FFT，卷积的 FFT 全部委托给 IPPWrapper（Intel IPP 封装）**。
2. **IR 生命周期**：`StudioIrEffect`（id2）在 IR 加载时**确实会做后处理**，且**全部在 libSuperSound2.dll 内部**：
   - 打开 `irs\*.enc` 失败时**内部调用 `decrypt_file`** 解密到临时 `*-dec` 文件再读；
   - **Trim = 尾部裁剪阈值（dB，扫描从尾部向前找最后一个超过阈值的帧）**；
   - **Fade = 末尾线性淡出（时长，单位 ms）**；
   - **重采样到效果的目标采样率**（有 SoundTouch 重采样器 + 线性插值兜底）；
   - **无峰值归一化、无加窗、无去直流、无二次哈希/校验**。
3. **卷积实现**：`SuperSoundFastConvolution`（单通道）+ `SuperSoundStereoConvolution`（内部持 4 个 FastConv，最多 4 通道），经典 **均匀分段 overlap-save 频域卷积**，FFT 长度 = 分段长 + overlap，分段数为 `ceil(IR长度/分段长)`。
4. **角色**：`libSuperSound2.dll` 就是 SS2 音效引擎本体；`qmcpcom.dll` / `qmcpcomx64.dll` / `QQMusic*.dll` 是**同一源码树（SuperSoundSDK-2020\supersound2lib）的另一份构建**（静态链接了同一套引擎，含 KEY256），代码里带 [SS2L] 日志与 76 插件注册表；**在本安装目录里没有任何模块通过名字导入 libSuperSound2.dll**，只有 `QQMusic_Protocol.dll` 内嵌 UTF-16 字符串 `libSuperSound2.dll` + `LoadLibrary*`/`GetProcAddress`（运行时加载）。
5. **对转换保真度**：我们直接把解密出的 `kernels\*.wav` 交给 V4A convolver 全长卷积，**缺少 QQ 引擎的 Trim（尾部裁剪）、Fade（末尾淡出）、按目标采样率重采样**三步。对带 Trim/Fade 的 IR（如 012）以及非 44.1kHz 的 IR（如 018 的 48k）会有尾部混响长度/亮度的差异；对已 44.1kHz、未启用 Trim/Fade 的 IR 差异可忽略。

---

## 1. 导出/导入面与引擎角色

### 1.1 导入表（决定能力边界）

`objdump -p` / pefile 显示 libSuperSound2.dll 只依赖：

| DLL | 关键导入 |
|---|---|
| **IPPWrapper.dll** | `?CreateFFTInst@IPPWrapper@@YAPAXXZ`、`?InitFFT@…`、`?FFT@…`、`?IFFT@…`、`?InitFFTC@…`、`?FFTC@…`、`?IFFTC@…`、`?DestroyFFTInst@…`（**仅这 8 个**） |
| KERNEL32 | MultiByteToWideChar、QueryPerformanceCounter、IsProcessorFeaturePresent… |
| MSVCP140 / VCRUNTIME140 / api-ms-win-crt-* | C++ 运行库、math（log10/pow/tan/atan…）、stdio（`_wfopen/fread/fwrite`）、locale、time、filesystem（remove） |

- 所有 FFT 调用点（IAT slot → 反汇编引用）：
  ```
  IAT 0x1005f000 DestroyFFTInst -> refs 10020985,10020a8c,10033c98
  IAT 0x1005f004 CreateFFTInst  -> refs 10020a95,10033d6f
  IAT 0x1005f008 FFT            -> refs 10020bda,10020ef6,10020f55
  IAT 0x1005f00c InitFFT        -> refs 10020b4b
  IAT 0x1005f010 IFFT           -> refs 10020c08
  IAT 0x1005f014 InitFFTC       -> refs 10033d81
  IAT 0x1005f018 IFFTC          -> refs 10034d2e,10034e89
  IAT 0x1005f01c FFTC           -> refs 1003457c,100346f0,10034b35
  ```
  → 频域卷积实现在 0x10020xxx（实数 FFT 路径）与 0x10033xxx–0x10034xxx（复数 FFT 路径）。

### 1.2 导出表（名字 + RVA，节选；完整 94 项）

> RVA = VA − 0x10000000。

**A. 引擎 API（`supersound_*`）**

| 名字 | VA |
|---|---|
| `supersound_init(SUPERSOUND_STREAM_READER const*)` | 0x1004e2d0 |
| `supersound_uninit` | 0x1004efb0 |
| `supersound_create_effect(SUPERSOUND_EFFECT_TYPE, AEffect**)` | 0x1004e580 |
| `supersound_get_existing_effect` | 0x1004e640 |
| `supersound_destroy_effect` | 0x1004e700 |
| `supersound_get_params / set_params` | 0x1004e720 / 0x1004e750 |
| `supersound_processf_input / processf_output`（float） | 0x1004e950 / 0x1004e9a0 |
| `supersound_process_input / process_output`（int16） | 0x1004e9e0 / 0x1004ea30 |
| `supersound_set_samplerate / get_samplerate` | 0x1004e870 / 0x1004e8b0 |
| `supersound_set_resource_root / get_res_paths` | 0x1004f520 / 0x1004f5b0 |
| `supersound_stream2params / params2stream` | 0x1004f340 / 0x1004f0e0 |
| `supersound_psctrl_*`（播放速度控制器，含 `set_samplerate/channels/multiple/seek/input/output`） | 0x1004ea80–0x1004ed90 |
| `supersound_set_modulator` / `set_script_error_handler` | 0x1004ede0 / 0x1004ef30 |
| `supersound_get_first_proc_len` / `get_in_chns` / `get_out_chns` | 0x1004e780 / 0x1004e7f0 / 0x1004e830 |

**B. `SUPERSOUND2::WaveFile`（WAV 读写）**

| 名字 | VA |
|---|---|
| `WaveFile::WaveFile()` / 拷贝构造 / 析构 | 0x10056b30 / 0x100133d0 / 0x10056b90 |
| `WaveFile::SetFilePathUTF8(char const*,bool,bool)` | 0x10056c20 |
| `WaveFile::SetFilePathW(wchar_t const*,bool,bool)` | 0x10057090 |
| `WaveFile::OnSetFilePath(bool,bool)`（protected） | 0x10056d20 |
| `WaveFile::SetupDone()` | 0x10057110 |
| `WaveFile::ReadFrames(...)`（8 种重载：float*/double*/float**/int16/int32/void*…） | 0x100573d0–0x10057f00 |
| `WaveFile::WriteFrames(...)`（8 种重载） | 0x100582d0–0x100588a0 |
| `WaveFile::GetSampleRate / GetChannels / GetSampleFormat / GetTotalFrames / GetDuration / GetSampleSize / GetChannelMask` | 0x10013a50 / 0x1000f420 / 0x10058af0 / 0x10058ae0 / 0x10058ad0 / 0x10043900 / 0x100573c0 |
| `WaveFile::SetSampleRate / SetChannels / SetSampleFormat / SetChannelMask / Seek / TimeToFrames / SetProfile` | … |

**C. 工具函数**

| 名字 | VA | 说明 |
|---|---|---|
| **`SUPERSOUND2::decrypt_file(char const*, char const*)`** | **0x10052400** | .enc 解密（=前序已破解） |
| `SUPERSOUND2::dB2scale / scale2dB` | 0x10052390 / 0x100523c0 | 缩放↔dB（scale2dB=`log10(x)*20`，见 `mulsd [0x10065c48]=20.0`） |
| `SUPERSOUND2::norm_center / denorm_center` | 0x10052310 / 0x10052290 | 参数值↔归一化（把物理量映射到 [0,1] 中心） |
| `SUPERSOUND2::df2i / i2df` | 0x10052650 / 0x10052670 | double-float↔int 辅助 |
| `SUPERSOUND2::lock_modulators / unlock_modulators / get/set_modulator_user` | 0x10052fc0 / 0x10052d00 / … | SS2L 脚本调制器 |
| `SUPERSOUND2::xplatform_pathname` | 0x100530d0 | 路径分隔符归一化 |
| `_set_xlog_handler / _set_xlog_level` | 0x10058b60 / 0x10058b70 | 日志钩子 |

### 1.3 谁在用 libSuperSound2？——角色判定

- **目录内没有任何模块在导入表里引用 `libSuperSound2.dll`**（`objdump -p` 全目录扫描无结果）。
- 但 **KEY256 常量表（`77 48 32 73 DE F2 C0 C8 …`）出现在多个模块**：

  | 模块 | 含 KEY256 | PDB 路径 |
  |---|---|---|
  | libSuperSound2.dll | ✅ | `D:\SuperSound_proj\trunk\supersound2lib\pdbs\Win32\Release_Dll\libSuperSound2.pdb` |
  | **qmcpcom.dll** | ✅ | `E:\yuanqingrui_work\git_work\SuperSoundSDK-2020\supersound2lib\pdbs\Win32\Release\qmcpcom.pdb` |
  | QQMusic.dll | ✅ | `E:\QQMusicPC\pdbRelease\GFQQMusic.pdb` |
  | QQMediaPlayer.dll / QQMusicCommon.dll | ✅ | — |

  → 说明 **SS2 引擎（含解密、卷积、效果）被静态编进了 qmcpcom / QQMusic\* 等模块**；qmcpcom 就是「SuperSoundSDK」里的宿主组件（其源码目录即 `supersound2lib`），并带 76 个效果注册表（RTTI，见 `docs/effect_registry.json`，取自 qmcpcomx64.dll）。
- `QQMusic_Protocol.dll` 含 UTF-16 字符串 **`libSuperSound2.dll`** 且导入 `LoadLibraryW/LoadLibraryExW/GetProcAddress` → **它是运行时按名字加载 libSuperSound2.dll 的模块**（同时还带 `supersound_earprint_*` 字符串，但该符号不在本 DLL 导出表中，可能是更早/更晚版本遗留，标注不确定）。
- `QQMusic.dll` 含 UTF-16 `SuperSound2SelectId` 与 RTTI `SuperSoundCtrlBase`/`SuperSoundWnd`（UI 侧控件）。

**判定**：`libSuperSound2.dll` = SS2 引擎共享库；本次安装中实际发声的引擎可能来自 qmcpcom/QQMusic 的静态副本，但两者**同源、行为一致**，因此对 libSuperSound2.dll 的分析结论对整体链路成立。

---

## 2. IR 加载后的处理（重点）

### 2.1 定位：`StudioIrEffect`（id2 StudioIr）

从 `docs/effect_registry.json`：`id 2 StudioIrEffect@STUDIO_IR@SUPERSOUND2`。在 libSuperSound2 中通过 RTTI 反查：

- TypeDescriptor（`.?AVStudioIrEffect@STUDIO_IR@SUPERSOUND2@@`）@ 0x10079d70；
- Complete Object Locator @ 0x1006895c / 0x100689a4；
- **vtable = 0x100650e4**；
- 构造函数 **0x100495d0**（`0x10049613  mov [esi],0x100650e4` 写 vptr；对象尺寸 0x3b0，见 0x10049840/0x10049774）。

### 2.2 IR 参数定义：IR File / Trim / Fade 都在 StudioIr 构造里注册

构造 0x100495d0 用两个注册辅助函数登记本地化名字：
- `0x10017320(this, locale, key)`：登记**分组/类别名**；
- `0x10017430(this, key, {locale,display}…)`：登记**参数名**（变参为本地化对）。

反汇编摘录（0x100495d0）：

```
1004964c  push 0x10065078   ; "コンボリューション"        (ja)
10049651  push 0x10060290   ; "ja"
...
1004965d  push 0x10065030   ; "Convolution"               (en)  ← 分组名
10049662  push 0x100602a0   ; "en"
10049669  call 0x10017320
...
1004967a  push 0x1006000b   ; ""
1004967f  push 0x100602a0   ; "en"
10049684  push 0x1006503c   ; "コンボリューション・ファイル"(ja)  ← IR File 的日文显示名
10049689  push 0x10060290   ; "ja"
1004968e  push 0x100650a8   ; "脉冲文件"                   (zh)
10049693  push 0x1006027c   ; "zh-HK"
10049698  push 0x100650a8   ; "脉冲文件"
1004969d  push 0x10060274   ; "zh-TW"
100496a2  push 0x100650b8   ; "脉冲文件"
100496a7  push 0x10060234   ; "zh"
100496ac  push 0x10065020   ; "IR File"
100496b1  push esi
100496b2  call 0x10017430
...
100496d5  push 0x10065028   ; "Trim"
100496da  push esi
100496db  call 0x10017430   ; Trim: zh"裁剪" / ja"トリム" / en""
...
10049701  push 0x10065008   ; "Fade"
10049706  push esi
10049707  call 0x10017430   ; Fade: zh"淡出" / ja"フェード" / en""
```

字符串实际内容（`.rdata`）：

| VA | 内容 |
|---|---|
| 0x10065020 | `IR File` |
| 0x10065028 | `Trim`（zh 裁剪 @0x100650a0，ja トリム @0x10065094） |
| 0x10065008 | `Fade`（zh 淡出 @0x100650d8，ja フェード @0x100650c8） |
| 0x10065030 | `Convolution`（分组名） |
| 0x1006503c | 日文「コンボリューション・ファイル」= Convolution File |

构造里还写死两个字段：`[esi+0x34]=3`、`[esi+0x3c]=2`（0x10049617 / 0x1004961e，语义未定，标注不确定）。

**结论**：`Trim`/`Fade` **属于 StudioIr（卷积）效果的参数**，语义：
- **Trim = 尾部裁剪阈值，单位 dB**；
- **Fade = 淡出时长，单位 ms**。

### 2.3 IR File 变更 → 重载流程（确认 Trim/Fade 由 SS2 在加载时执行）

StudioIr 的重载方法 **0x100498b0**（vtable 槽，无直接调用者）：

```
100498e0  mov eax,[ebx]                 ; this=vtable
100498e2  call [eax+0xdc]               ; 取资源根/环境
100498e8  push 0x120 / new              ; 建文件列表管理器(0x120, vtable 0x10065544)
...
1004996e  call [eax+0x1c]               ; 取(浮点)目标采样率
10049979  cvttss2si eax,[ebp-0x440]     ; → int
100499a7  mov [ebp-0x43c],eax           ; profile[0]=目标采样率
...
10049a02  mov eax,[ebx]; call [eax+0x60] ; 解析/拼接 IR 文件路径
10049a92.. memmove(profile+0xc, 路径字符串)
...
10049ab0  push 0x10065028   ; "Trim"      ← 读 Trim
10049ab5  call [eax+0x38]   ; GetParam(name)->float
10049abb  fstp [ebp-0x30]   ; profile+0x40c = Trim
10049abe  push 0x10065008   ; "Fade"      ← 读 Fade
10049ac6  call [eax+0x38]   ; GetParam(name)->float
10049ace  fstp [ebp-0x2c]   ; profile+0x410 = Fade
...
10049b71  call 0x100493f0   ; 用 profile 建立音频对象(0x82c)
```

profile 结构（0x414 字节，起始 `[ebp-0x43c]`）：`+0`=目标采样率、`+0xc`=文件路径、`+0x40c`=Trim、`+0x410`=Fade。

`0x100493f0` 把 profile 拷进 0x82c 对象（`rep movsd` 0x105 dword）并建立内部音频对象，随后：

```
1004951b  movss xmm0,[ebx+0x820]   ; Trim  (profile+0x40c)
10049523  movss xmm1,[ebx+0x824]   ; Fade  (profile+0x410)
...
1004954c  movss [esi+4],xmm0        ; 内部音频对象 +4 = Trim
10049551  movss [esi+8],xmm1        ; 内部音频对象 +8 = Fade
...
1004956b  call 0x10048c60           ; ★ 真正的加载器(this=内部对象, 路径)
```

### 2.4 加载器 0x10048c60（解密→读帧→Trim→Fade→重采样）

这是 IR/WAV 的**总加载器**。关键片段：

**(a) 打开文件；失败则内部调用 decrypt_file**

```
10048c8c  push 0x38 / new            ; 建 WaveFile 对象(vtable 0x10060018)
10048d0e  push 0x10061630            ; fmt = "%s%s"
10048d14  call 0x10013980            ; sprintf(path, "%s%s", root, file)
10048d2a  call 0x10056c20            ; WaveFile::SetFilePathUTF8(path)   ← 先按明文打开
10048d35  test bl,bl ; jne ok        ; 若成功→ok
10048d44  push 0x10061638            ; fmt = "%s%s-dec"
10048d51  call 0x10013980            ; sprintf(decPath, "%s%s-dec", root, file)
10048d66  call 0x10052400            ; ★ SUPERSOUND2::decrypt_file(path, decPath)
10048d79  call 0x10056c20            ; 再用 SetFilePathUTF8(decPath) 打开
```

→ **SS2 在内部完成 .enc 解密**（先试明文，失败即解密到 `文件名-dec` 临时文件）。三处 `decrypt_file` 内部调用：0x10047148（AudioFile/Sampler 加载器 0x10046f90）、0x100482a8（另一加载器）、0x10048d66（IR 加载器）。

**(b) Trim —— 尾部阈值裁剪**

```
10048dab  movss xmm1,[esi+4]         ; Trim
10048dc1  movsd xmm0,[0x10065c18]    ; = 10.0
10048dc9  divsd xmm1,[0x10065c48]    ; Trim / 20.0
10048dd1  call 0x1005ac76           ; _libm_sse2_pow_precise(10, Trim/20)
10048ded  movss [esp+0x34],xmm0      ; threshold = 10^(Trim/20)   ← 线性幅度阈值
...
10048e72  movss xmm1,[esi+4]         ; Trim
10048e77  comiss xmm1,[0x10065ecc]   ; -96.0
10048e7e  jbe skip                   ; if Trim <= -96 → 不裁剪
10048e80  movss xmm0,[0x10065ec8]    ; -30.0
10048e8b  jbe skip                   ; if -30 <= Trim → 不裁剪
10048e8f  cmp ebx,0x400 ; jbe skip   ; 帧数 <=1024 不裁剪
10048ea0 loop: dec ebx               ; 从尾帧向前
10048eb0   对 4 个声道:
10048eb4     movss xmm0,[ch][ebx]
10048eb9     call 0x10021ea0         ; fabs
10048ebe     maxss xmm0,xmm1         ; 取 4 声道最大幅度
10048ecb   comiss threshold,max ; jbe found
10048ed0   mov edx,ebx              ; 该帧视为静音，继续向前
```

即：`Trim∈(-96,-30) dB` 时，从末帧向前找到**最后一个 max|x| > 10^(Trim/20) 的帧**，作为 IR 有效长度（尾部低于阈值的静音/混响尾巴被截掉）。**注意：只裁尾，不裁头；阈值是相对满幅的绝对 dB。**

**(c) Fade —— 末尾线性淡出（ms）**

```
10049110  movss xmm2,[esi+8]         ; Fade
10049117  divss xmm2,[0x10065db0]    ; / 1000.0     → 秒
1004911f  mov eax,[esi+0xc]          ; 目标采样率
1004913c  mulss xmm2,xmm0            ; ×采样率      → 淡出样点数
10049140  cvttss2si eax,xmm2
10049144  cmp eax,edx ; cmovbe ecx,eax  ; n = min(淡出点数, 裁剪后帧数)
1004915d loop (ecx 从 n-1 递减到 0):
10049167    xmm1 = (float)ecx / n     ; 线性斜坡 1→0
1004916e/7b/90/9e  × 4 个声道末尾对应样点
```

即：在裁剪后的 IR 末尾施加**线性淡出，时长 = Fade 毫秒**（首样 ≈1，末样 =0）。**Fade 与 Trim 联动**：只有当 `Trim∈(-96,-30)` 才进入裁剪分支，淡出代码（0x10049110）也只在裁剪分支内被跳入；Trim 关闭时 Fade 不生效。

**(d) 重采样到目标采样率**

```
10048ee5  mov eax,[esi+0xc]          ; 目标采样率
10048ee8  mov ecx,[edi+0x14]         ; 文件实际采样率(WaveFile)
10048eed  je skip                    ; 相等则跳过
10048ef3.. 计算 新帧数 = 旧帧数 × 目标率 / 文件率  (FP, 0x1005ac8e=floor)
10048f7a  malloc 新缓冲
10048fc3  new 0x28 重采样器(vtable 0x100646c4) → Init + Process
10048ffd    call [edx+4] / [eax+8]   ; SoundTouch 重采样(见 §4)
10049023  若重采样器失败 → 走线性插值兜底(0x10049060..0x100490c3)
```

→ **IR 会被重采样到「效果目标采样率」**。该目标率存在内部对象 `+0xc`：分配时先写死 `0xAC44 = 44100`（`10049484  mov dword ptr [esi+0xc],0xac44`），随后由 `0x100494e1 call 0x10048990` 用 profile[0]（来自效果 `vtable+0x1c` 返回的浮点采样率）覆盖（`100489a2 mov [edi+0xc],eax`）。**机制确定；具体目标值 = 会话/效果采样率，默认常量 44100。**

**(e) 无归一化、无加窗、无去直流**

- 加载器内唯一的幅度运算就是 Trim 的「阈值比较」（不是缩放）与 Fade 的线性乘；**没有对整段 IR 求峰值再除以峰值**（未见 max-over-buffer → divss 归一化模式）。
- 没有 Hann/Hamming/Blackman 等窗函数（strings 全量搜索无 `hann/hamming/blackman/window` 函数名，唯一 `window_bits` 属另一效果）。
- 没有去直流/DC 估计。
- 卷积核直接送 FFT（FFT 卷积本身保幅，不再缩放）。

**(f) 接到卷积引擎**

```
10048990: new(0x18) → [ecx]=0x10061528   ; ★ SuperSoundStereoConvolution  vtable
          [edi+0x14]=StereoConv ; [edi+0xc]=采样率
          … 4 个声道缓冲 [edi+0x1c],[+0x11c],[+0x21c],[+0x31c]
```

### 2.5 卷积实现与 FFT 分段尺寸

`SuperSoundStereoConvolution`（vtable 0x10061528）内部是 4 个 `SuperSoundFastConvolution`：

- StereoConv 析构 0x10021090：遍历 `[ebx+4..+0x10]` 4 个子对象；
- StereoConv 加通道 0x10021180：
  ```
  10021180  eax=[ebp+0x10]               ; 通道号
  10021191  push 0x34 / new(0x34)        ; 新建 FastConv
  1002119e  mov [eax],0x1006150c         ; ★ FastConv vtable
  100211f8  call [eax+8]                 ; FastConv::Init(arg1,arg2)  (分块长, overlap)
  ```
- FastConv Init **0x10020a70(this, a, b)**：
  ```
  10020a82  [esi+0xc]=b ; [esi+8]=a ; [esi+4]=a+b    ; FFT 长度 = 分块长 + overlap
  10020a8a  call [0x1005f000] DestroyFFTInst
  10020a93  call [0x1005f004] CreateFFTInst
  10020ac7  kernel FFT 缓冲 = new([esi+8]*4)
  10020b15  input  FFT 缓冲 = new([esi+4]*4)
  10020b43  call [0x1005f00c] InitFFT(inst, [esi+4])  ; FFT 长度
  ```
- FastConv 设定核 **0x10020b70 → 0x10020d70**：
  ```
  10020d74  eax=[esi+0x10]              ; 核样点数 N
  10020d78  eax += [esi+8]-1
  10020d7c  idiv [esi+8]                ; 分段数 = ceil(N / 分块长)
  10020d8b  [esi+0x18]=分段数
  10020d96  为每个分段分配频域缓冲([esi+4]*4)
  ```
- FastConv 处理 **0x10020ba0**：拷贝入参块 → 补零到 FFT 长 → `FFT` → 分段累积(0x10020f90) → `IFFT` → overlap-add。

**结论**：经典 **均匀分段 overlap-save 频域卷积**；FFT 长度 = 分块长 + overlap，分段数 = `ceil(IR样点 / 分块长)`。**分块长为 2 的幂**（某处 `shl eax,cl`：`0x1004a37d mov eax,1; mov ecx,[esi+0x10]; shl eax,cl` → `分块长 = 2^[field]`，该字段疑似「卷积分块位数」），但**具体默认位数/数值未能从 StudioIr 路径直接钉死**（调用为虚分发，参数由效果字段传入；标注不确定）。

---

## 3. 是否还有其他加密/校验？

- **只有一层 XOR 流密码**：`decrypt_file`（0x10052400）→ `decrypt_block`（0x1001fb70），256B 常量表 @0x1005f788，公式 `index=(q²+0x13c1b)&0xFF`，`q = i>0x7fff ? i%0x7fff : i`，块 0x80000（前序 `docs/IR解密分析.md` 已证）。
- **无 CRC / MD5 / SHA / 校验和 / 签名校验**：strings 全量搜索无相关函数名；唯一 `SHA256` 字样来自 PE 代码签名证书，非音频逻辑。
- **无第二层变换**：解密产物即裸 WAV，加载器直接 `SetFilePathUTF8` 打开并 `ReadFrames`，不做 FFT 域处理、无关税、无峰值归一化（见 §2.4e）。
- RIFF 内的自定义块（如 504 的 `SyLp` 块）只是 WAV 元数据，不影响负载。

---

## 4. SS2 自带 DSP 原语与 76 插件对照

libSuperSound2 的 RTTI 里包含以下 `SUPERSOUND2` 效果/DSP 类（`strings | grep '?AV.*@SUPERSOUND2@@'`）：

- **卷积**：`ISuperSoundConvolution` / `SuperSoundFastConvolution` / `SuperSoundStereoConvolution`（§2.5）。
- **重采样**：`ResamplerEffect@RESAMPLER`、`PlaySpeedController`、`Resampler_base`、`CResampler`、`CResampler_SRC`、`CResampler_SSRC`、`ssrc_resampler`、`Resampler<NNN>`；并内嵌 **SoundTouch**（strings：`SoundTouch : Sample rate not defined`、`SoundTouch : Number of channels not defined`、`FIR filter length not divisible by 8`）→ 重采样算法用 SoundTouch（SSRC/Sinc）。
- **动态/滤波**：`LimiterEffect@LIMITER`、`MultiBandCompressorEffect@COMPRESSOR`、`ALREVERB`（`AlreverbEffect` + 参数 `density/diffusion/reverbGain/decay_time/early_gain/late_gain/…`）、`DeEsserEffect`、`ExciterEffect`、`VocalEffect`、`AmbienceEffect`、`PannerEffect`、`RotatorEffect`、`Panoramic51Effect`、`Mono2DualEffect`、`TwotoSix`、`StereoEnhancerEffect`（参数 `Width/Center`）、`Dfx*`（`Dfx3DSurround/DfxHyperbass/DfxHeadphone/DfxAmbience/DfxDynamicBoost` 的 `SS2EffectT<…>` 模板 + `DspWrapperRunner/IDspWrapper/GrowlDspWrapper/VBassDspWrapper`）。
- **EQ/滤波**：`Peaking*FilterQEffect@EQFILTER`、`LP/HP/BP/Notch/HS/LS/Tilt/EqFilterEffect`、`NTFilterEffect@BIQUADFILTER`、`BP/HP/LP/BSFilterEffect@BUTTERFILTER`、`IirEQ10Effect/IirEQ30Effect/EqfbEffect@IIR_EQ_FIXED_BANDS`、`HighShelfFilter`、`SuperEQEffect`（`%d Bands EQ II`、`window_bits`、`octave`、`start_f`、`gain_len`）。
- **采样器**：`SamplerEffect/StereoSampler/MultiFuncSampler(Effect)@SAMPLER`（参数 `Audio File`、`Min/Delay/Max Interval Time`）。
- **放大器/虚拟低音**：`AmplifierEffect`、`HyperBassEffect`、`Virtual bass`（DeaDBeeF 虚拟低音，版权串可见）、`ClipBoost`。
- **工具**：`WaveFile`、`AudioEffect`（基类）、`LayoutUtils`、`ISuperSound2`。

**与 76 插件注册表（docs/effect_registry.json，取自 qmcpcomx64.dll）对照**：注册表的 76 项全部是 `SUPERSOUND2` 命名空间的类（`ThroughEffect/StudioIrEffect/ChaosEffect/AmplifierEffect/RotatorEffect/LimiterEffect/SamplerEffect/IirEQ30Effect/ExciterEffect/ResamplerEffect/StereoEnhancerEffect/DelayEffect/…`），**与 libSuperSound2 的 RTTI 类名一一对应**。→ **76 个效果的实际 DSP 全部由 SS2 承担**；qmcpcom 只是宿主封装（`ss_op/ss_strong_bass/ss_wide_soundfield/panoramic_51/ss_3d_surround/sleep_effect` 等 `get_effect_stream` 把参数序列化成 `SUPERSOUND_*_TYPE` 交给 `supersound_params2stream`），并把效果类型 id 映射到 SS2 的 effect 工厂 `supersound_create_effect`。

> 换句话说：**卷积 FFT（IPP）、重采样（SoundTouch）、混响、限幅、多段压缩、EQ、滤波、变调/变速、采样器、3D/环绕全部由 SS2 实现**，qmcpcom 的 76 插件里没有一个把 DSP 下推到别的库。

---

## 5. 对转换保真度的影响（结论）

我们目前的做法：`decrypt_file` 得到裸 WAV → 直接作为 `kernels\*.wav` 给 V4A convolver 全长卷积。QQ 引擎实际链路多了以下**IR 级后处理**：

| 步骤 | QQ 引擎行为 | 我们是否做 | 影响 |
|---|---|---|---|
| **Trim** | 若 `Trim∈(-96,-30) dB`：从尾部找 `max|x| > 10^(Trim/20)` 的最后一帧，截断其后 | ❌ 未做 | 尾部含低于阈值的长混响尾巴时，**V4A 会多卷一段 QQ 已丢弃的尾巴**：混响尾音偏长、能量/亮度轻微偏差；对本身短、末尾即归零的 IR 无影响 |
| **Fade** | 截断后对末尾 `Fade` ms 施加线性淡出到 0 | ❌ 未做 | 若我们未截尾，本就不需要淡出；只有当 QQ 截尾而我们不截时，差异被 Trim 那条覆盖。**Fade 与 Trim 联动，单独存在意义有限** |
| **重采样** | IR 重采样到效果/会话目标采样率（默认常量 44100） | ❌ 未做 | 48kHz 的 IR（如 `fae6b7a7…` = 018 极重低音，48k）在 QQ 里会被降到 44.1k，我们的 kernel 保持 48k → **频响/时基有细微差异**（V4A 播放率与 kernel 采样率若不一致会变调/长度变化） |
| **峰值归一化/加窗/去直流** | **无** | — | 无差异 |
| **FFT 分段卷积** | 均匀分段 overlap-save（IPP FFT），数学上 = 全长线性卷积 | V4A convolver 也是 FFT 卷积 | 稳态结果**等价**，仅延迟/分段边界数值差；**不是保真度问题** |

**一句话总结**：QQ 引擎在解密之后、卷积之前**只在 SS2 内部对 IR 做「Trim 尾部阈值截断（dB）+ Fade 末尾线性淡出（ms）+ 重采样到目标采样率」三件事，没有归一化/加窗/二次加密**；我们直接把解密 WAV 全长卷积，**缺的正是这三步**——对带 Trim/Fade 的 IR（如 012-复合低音）会多卷一段 QQ 已裁掉的尾部混响，对非 44.1kHz 的 IR（018 的 48k）会有采样率不一致的细微差异，对已 44.1kHz 且 Trim/Fade 未启用的 IR 影响可忽略。

### 建议（供决策，不在本次改动范围）
1. 转换时读取预设的 `Trim`/`Fade` 值：若 `-96 < Trim < -30`，按 `10^(Trim/20)` 阈值裁尾，再对末尾 `Fade` ms 线性淡出到 0。
2. 统一把 kernel 重采样到 44100 Hz（与 QQ 默认一致），或至少保证 kernel 采样率与 V4A 播放采样率一致。

---

---

## 6. kernel 构建流水线与实测（tools/build_kernels.py）

### 6.1 新证据：Trim/Fade 的运行时缺省值 = 0（本语料全部不激活）

`§2.2/2.4` 只确定了"参数存在时"的行为。语料中 **11/12 个 IR 引用根本不带 Trim/Fade 参数**，
其运行时取值取决于效果对象成员（`+0x820`=Trim、`+0x824`=Fade）的初始化。本轮补齐证据：

```
10049b3d  mov dword ptr [edi + 0x824], 0    ; Fade = 0.0
10049b47  mov dword ptr [edi + 0x820], 0    ; Trim = 0.0
```

即 StudioIr 效果对象初始化时**显式清零**这两个成员（0.0 按浮点位解释）。结合 §2.4 的生效条件：

| 语料情况 | Trim 值 | 是否落在 (-96,-30) | 结论 |
|---|---|---|---|
| 11 个文件不带参数（缺省） | 0.0（0x10049b47 清零） | 否（0 > -30） | 不裁剪 |
| 018 / 504 显式携带 | -100.0 | 否（-100 ≤ -96） | 不裁剪 |

**结论：全语料 12 个 IR 的 Trim/Fade 均不激活，QQ 引擎按全长 IR 卷积——
我们此前直接使用全长解密产物恰好与 QQ 行为一致，不存在尾部处理缺口。**
build_kernels.py 仍完整实现 Trim/Fade 语义（含 VA 证据注释），供未来语料复用。

### 6.2 流水线与实测输出

`tools/build_kernels.py`：收集语料 id2 引用 → .enc（缓存/CDN `dldir1.qq.com/music/clntupate/ss2/irs/`）
→ `decrypt_ir` 解密 → Trim/Fade（按 §2.4 语义）→ 通道归一（4ch true-stereo 取对角）→
重采样归一 44100Hz（加窗 sinc，近似 SoundTouch 路径）→ float32/2ch WAV →
`kernels/441/<名>_441.wav` 与 `kernels/48k/<名>_48k.wav`（双速率可读命名，hash↔名字对照见
`kernels/kernel_index.json`；原为扁平 `kernels\<sha1>.wav`）。

| hash | 源格式 | Trim/Fade | 输出 |
|---|---|---|---|
| 2f823376 | 2ch/44.1k/0.186s | 未携带→不生效 | 0.186s |
| 36af30f9 | 2ch/44.1k/0.371s | 未携带→不生效 | 0.371s |
| 43e714f5 | 2ch/44.1k/0.001s | 未携带→不生效 | 0.001s |
| 4d11b1cd | 2ch/44.1k/0.371s | 未携带→不生效 | 0.371s |
| 6f7ed674 | 2ch/44.1k/0.743s | 未携带→不生效 | 0.743s |
| 74215679 | **4ch**/44.1k/2.226s | Trim=-100→不生效 | **对角降 2ch** 2.226s |
| 8da782aa | 2ch/44.1k/0.406s | 未携带→不生效 | 0.406s |
| b7c9156c | 2ch/44.1k/0.023s | 未携带→不生效 | 0.023s（012 备选 IR） |
| c2e6f778 | 2ch/44.1k/0.093s | 未携带→不生效 | 0.093s |
| d8d31861 | 2ch/44.1k/0.012s | 未携带→不生效 | 0.012s |
| f11a9f4c | 2ch/44.1k/0.371s | 未携带→不生效 | 0.371s |
| fae6b7a7 | 2ch/**48k**/0.044s | Trim=-100→不生效 | **重采样 44.1k** 0.044s |

校验：12/12 全部 float32 全有限、峰值 |x|≤1.19、首尾 RMS 形态合理；74215679 保留 ch0 直通尖峰（max=1.0）。
`kernels/441|48k/采样素材_黑胶_留声机_*.wav`（原 `679a81d9*`）、`采样素材_黑胶_留声机_2_*.wav`
（原 `de36ef18*`）为 id7 Sampler 音乐素材（对应效果 skipped），不属于卷积核，不归本流水线，仅保留作参考。

---

## 附录 A：关键地址速查

| 项 | VA |
|---|---|
| decrypt_file / decrypt_block / KEY256 | 0x10052400 / 0x1001fb70 / 0x1005f788 |
| StudioIrEffect vtable / 构造 / 重载 | 0x100650e4 / 0x100495d0 / 0x100498b0 |
| StudioIr RTTI TypeDescriptor | 0x10079d70 |
| IR/WAV 加载器（解密回退+Trim+Fade+重采样） | 0x10048c60 |
| AudioFile/Sampler 加载器（同型，decrypt@0x10047148） | 0x10046f90 |
| profile→音频对象 (0x82c) | 0x100493f0 |
| 建 StereoConv + 4 声道缓冲 | 0x10048990 |
| SuperSoundStereoConvolution vtable / 析构 / 加通道 | 0x10061528 / 0x10021090 / 0x10021180 |
| SuperSoundFastConvolution vtable / Init / 设核 / 处理 | 0x1006150c / 0x10020a70 / 0x10020b70 / 0x10020ba0 |
| IPP FFT IAT | 0x1005f000–0x1005f01c |
| 常量：-96.0 / -30.0 / 1000.0 / 10.0 / 20.0 | 0x10065ecc / 0x10065ec8 / 0x10065db0 / 0x10065c18 / 0x10065c48 |
| 字符串：IR File / Trim / Fade / Convolution | 0x10065020 / 0x10065028 / 0x10065008 / 0x10065030 |

## 附录 B：不确定性清单

1. **卷积分块长（FFT 段长）的确切默认数值**未钉死：机制为 `分块长 = 2^[字段]`、`FFT 长 = 分块长 + overlap`、`分段数 = ceil(N/分块长)`；具体位数由虚分发传入，未在 StudioIr 路径定位到立即数。
2. **重采样目标采样率**：机制为「内部对象 +0xc」，分配时常量 0xAC44(44100)，随后可能被效果/会话采样率覆盖；`018` 等 48k IR 在 QQ 内的最终卷积采样率由此决定（倾向 44100，但未 100% 证实）。
3. `StudioIr` 构造里 `[esi+0x34]=3 / [esi+0x3c]=2` 两字段语义未定。
4. `QQMusic_Protocol.dll` 内 `supersound_earprint_*` 字符串不在本 DLL 导出表中，疑为版本遗留。
5. 结论基于 **libSuperSound2.dll 与 qmcpcom/QQMusic\* 静态副本同源**（同 PDB 目录树）；若实际发声用的是 qmcpcom 副本，行为应一致但未逐字节比对。
