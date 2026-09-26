# QQ音乐 PC 客户端音效引擎（qmcpcom）静态逆向分析

目标：验证「全景环绕」音效在 .aep 参数之外是否存在额外内部处理。
方法：纯静态分析（strings / pefile / capstone），**未运行或加载任何 DLL**。

## 0. 样本与坐标约定

| 文件 | 说明 |
|---|---|
| `D:\AI-Agent\ZCode\workspace\QQMusic去exe版\qmcpcom.dll` | x86，2.97 MB，ImageBase = `0x10000000`，本次主要分析对象 |
| `D:\AI-Agent\ZCode\workspace\QQMusic去exe版\qmcpcomx64.dll` | x64 对照，未深入 |
| `D:\AI-Agent\ZCode\workspace\500-全景环绕.aep` | 预设（QMAEP/FlatBuffers 格式，1372 字节） |

下文所有地址若无特别说明均为 x86 `qmcpcom.dll` 的**虚拟地址（VA）**（= `0x10000000 + 文件偏移 + 0xC00`，.text 段）。常量按小端 float/double 解读。

引擎为第三方 `SUPERSOUND2`（RTTI 命名空间 `SUPERSOUND2`、日志前缀 `[SS2L]`）音频插件框架；`.aep` 即 QMAEP/FlatBuffers 效果链配置（`"ss_config::create_effect import: file ext is not .aep!!!"` @0x10268e9c）。

---

## 1. `.aep` 结构与节点还原

对 `500-全景环绕.aep` 按参数条目格式 `04 00 00 00 | float32 | u32 strlen | name` 扫描，得到全部条目（`输出\逆向` 脚本已复核）：

| 偏移 | 值 | 名称 |
|---|---|---|
| 0x0c4 | 2 | `16000 Hz` |
| 0x104 | 2 | `8000 Hz` |
| 0x140 | 1 | `4000 Hz` |
| 0x17c | 1 | `2000 Hz` |
| 0x1b8 | 1 | `1000 Hz` |
| 0x1f4 | 0 | `500 Hz` |
| 0x230 | 0 | `250 Hz` |
| 0x26c | 1 | `125 Hz` |
| 0x2a8 | 1 | `63 Hz` |
| 0x2e4 | 0 | `31 Hz` |
| 0x320 | 1.22474 | `Q` |
| 0x384 | 0 | `Right Feedback` |
| 0x3c8 | 0 | `Left Feedback` |
| 0x40c | 26.1224 | `Right Time` |
| 0x44c | 0 | `Left Time` |
| 0x4b0 | 100 | `Center` |
| 0x4ec | 130 | `Width` |
| 0x548 | 2.27887 | `Gain` |

文件头含 `"QMAEP"`（0x4c）与 UTF-8 效果名「全景环绕」（0x38）。解析器（VA 0x100dff05 附近）支持 `Version / num_track / out_type / master_gain` 字段（格式串 @0x10222a48），即预设层面可带一个整体 `master_gain`（属配置数据，非隐藏代码）。

**结论：预设实际由 4 个插件节点构成**（与派单描述一致）：

1. **EQ**（`10 Bands EQ II` / IirEQ10）：10 个频段增益 + `Q`
2. **Delay**：`Left/Right Time`、`Left/Right Feedback`
3. **Stereo Enhancer**（立体声扩展）：`Width`、`Center`
4. **Gain**

注意：`Width/Center` 与 `Left/Right Time/Feedback` **属于两个不同插件类**——已通过构造函数逐一验证（见下），不是同一个「stereoizer」的参数组。

---

## 2. 立体声扩展插件（Stereo Enhancer, `id11`，参数 Width/Center）

**定位链**：参数字符串 `"Width"`@0x10227484、`"Center"`@0x1022748c → 构造函数 VA **0x1012e810**（`mov dword [esi],0x10227498` 写入 vtable）→ vtable **0x10227498** → 其专属虚函数槽 +0xDC = **0x1012ed60**（Process）。

构造函数（0x1012e810）关键片段：

```
1012e853  mov  dword ptr [esi], 0x10227498      ; vtable
...
1012e8e7  push 0x10227484                        ; "Width"
1012e916  call 0x101286a0                        ; registerParam(this,"Width",...)
1012e910  push 0x1022748c                        ; "Center"
...
1012e96a  push 0x10227484 ; call getParam        ; 读 Width
1012e978  fdiv dword ptr [0x10222d40]            ; /100.0
1012e98a  fstp dword ptr [esi+0x3ac]             ; this->W = Width/100
1012e982  push 0x1022748c ; call getParam        ; 读 Center
1012e992  fdiv dword ptr [0x10222d40]            ; /100.0
1012e99a  fstp dword ptr [esi+0x3b0]             ; this->C = Center/100
```

常量 `[0x10222d40] = 100.0`（float）→ **Width/Center 单位为百分比**，进程内归一化为因子。

**Process（0x1012ed60，签名 `ret 0xc`：`Process(float** buf, int* nframes, int* chanIdx)`）完整数学**：

```
1012ed8f  movss xmm1,[esi+0x3ac]        ; W
1012ed98  movss xmm0,[esi+0x3b0]        ; C
1012edbb  addss xmm2,xmm3               ; L+R
1012edbf  subss xmm3,[eax+ecx]          ; L-R
1012edc4  mulss xmm2,xmm4               ; (L+R)*0.5   <- xmm4=[0x10271c74]=0.5
1012edc8  mulss xmm3,xmm4               ; (L-R)*0.5
1012edcc  mulss xmm0,xmm2               ; C*mid
1012edd0  mulss xmm1,xmm3               ; W*side
1012edd4  addss xmm1,xmm0 ; [edx+ecx]=   ; L' = C*mid + W*side
1012edf0  mulss xmm1,xmm2               ; C*mid
1012edf4  mulss xmm0,xmm3               ; W*side
1012edf8  subss xmm1,xmm0 ; [eax+ecx]=   ; R' = C*mid - W*side
```

即标准 **mid/side 立体声宽度矩阵**：

```
mid  = (L+R) * 0.5
side = (L-R) * 0.5
C    = Center / 100
W    = Width  / 100
L'   = C*mid + W*side
R'   = C*mid - W*side
```

- **无任何滤波**（无低通/全通/EQ 调用），**无延迟线**，是逐样本瞬时矩阵。
- 常数 `0.5`@0x10271c74 与 归一化 `100.0`@0x10222d40 是仅有的固定量。
- 代入预设（Width=130→W=1.30，Center=100→C=1.00）：
  `L' = 1.15·L − 0.15·R`，`R' = −0.15·L + 1.15·R`（纯加宽，无电平补偿）。

**Center/Width 语义**：两者均为百分比因子；`Width` 控制 side 分量权重（1.0=原状，>1 加宽，<1 收窄），`Center` 控制 mid 分量权重（中置/整体电平）。系数直接相乘、无耦合、无归一化。

---

## 3. Delay 插件（`id12`，参数 Left/Right Time、Feedback）

**定位链**：`"Left Time"`@0x10227620 等 → 构造函数 VA **0x1012eed0**（`mov dword [edi],0x1022765c`）→ vtable **0x1022765c**。真正的延迟 DSP 在两个被调用的核心函数：

### 3.1 延迟线处理（核心数学） 0x10187330

```
1018734a  movsd xmm3,[0x10222128]       ; =1000.0
10187360  movd  xmm1,[esi-0x20]         ; 时间参数(ms, int)
10187369  movd  xmm0,[eax+0x14]         ; 采样率
1018736e  divsd xmm1,xmm3               ; /1000
10187389  mulsd xmm1,xmm0               ; * sampleRate
10187396  cvttsd2si edx,xmm1            ; delaySamples = ms/1000*SR   <-- 单位换算
...
1018739a  divsd xmm0,[0x102251d0]       ; =100.0
1018739e  cvtpd2ps xmm2,xmm0            ; fb = Feedback/100
内层循环（每帧）：
101873b3  movss xmm1,[edi]              ; 输入 x
101873b7  movss xmm0,[eax+ecx*4]        ; 环形缓冲读出 = 延迟样本 d
101873bc  movss [edi],xmm0              ; 输出 = d（100% 湿声，覆盖输入）
101873c6  mulss xmm0,xmm2               ; d*fb
101873ca  addss xmm0,xmm1               ; + x
101873ce  movss [eax+ecx*4],xmm0        ; 回写缓冲 = x + fb*d
101873d8  ... 读指针=(读+1<len)?+1:0     ; 环形回绕
```

- **时间单位为毫秒**：`delaySamples = Left/Right Time(ms) / 1000 × SampleRate`。
- **Feedback 单位为百分比**：反馈系数 `= Feedback / 100`（默认/上限表向量给出 最高 120%）。
- **延迟支路无任何滤波**：输出直接取环形缓冲值，回写为 `输入 + fb×延迟输出`，纯标量反馈梳状/延迟，无低通、全通、均衡、阻尼。
- 输出为 **100% 湿声**（延迟样本覆盖输入，无干湿混合参数）。

### 3.2 缓冲分配 0x10187420

```
10187483  imul eax,ecx                  ; param * sampleRate
1018748e  divsd xmm0,[0x10222128]       ; /1000
10187496  addsd xmm0,[0x10271cf0]       ; +1
1018749e  mulsd xmm0,[0x10221e48]       ; *4 (float 字节数)
```

缓冲字节数 `=(ms/1000*SR + 1)×4`，再次印证 **参数是毫秒**。

**预设代入**：`Left Time=0`、`Right Time=26.1224 ms`（@44.1kHz 恰为 **1152 样本**）、`Feedback=0`。
→ 左声道直通（延迟 0），右声道延迟约 26.12 ms；无反馈、无滤波。**这是一个 Haas/单边延迟型立体声加宽支路**，而非带滤波的立体声扩展。

---

## 4. EQ 插件（`id13`，10 段 + Q）

**定位链**：核心类 `IirEQ10Effect@IIR_EQ_FIXED_BANDS@SUPERSOUND2`（RTTI 名 @0x102b2524）→ vtable **0x102277EC**；构造 0x1012f5c0（`call 0x10186940` 基类构造，参数 `0xa`=10 段）。

基类构造 **0x10186940** 注册参数并动态生成频段名：

```
10186ab6  push 0x1024f1b0 / "en" / 0x10228370 "Q" / this ; registerParam("Q",...)
10186d3a  lea eax,[名字缓冲] ; push "%d Hz"(0x10228a3c) ; sprintf ; registerParam(<"%d Hz">)
```

→ **每段参数名运行时用 `sprintf("%d Hz", freq)` 生成**，与 `.aep` 里出现的 `"31 Hz"…"16000 Hz"` 完全对应；`Q` 是独立参数。

**频段表**（静态，10 个 float）：

```
0x10230C60: 31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000 (Hz)
```
（另一份同名表 @0x102277C0，值相同。）`.aep` 的 `"31 Hz"/"63 Hz"` 是 `%d` 截断显示。

**系数计算 0x101AC5A0**（`(this, sampleRate, freq, Q, gain_dB)`）与 **逐样本处理 0x101AC4A0 / 0x101870D0**：

```
0x101ac5a8  andps  |gain| ; 若 |gain|<0.001(0x10229d78) 或 2f>=SR -> 旁路(flag[+0x1c]=1, 直通)
0x101ac5e8  xmm3 = gain / 20.0 (0x10271d78)
0x101ac5f4  pow(10.0(0x10271d28), gain/20) - 1.0  -> [esi+0x18] = A-1 , A=10^(dB/20)
0x101ac63a  w = 2πf/SR   (2π @0x10271d20) ; tan(w*Q*0.5) ; 由 tan 构造二阶全通系数
处理：
101ac4a0:  y = x + [esi+0x18]·(二阶全通(x))   ; Regalia-Mitra 型 peaking
```

即 **级联 10 个二阶 peaking 滤波器**，用高效「全通型 peaking」结构 `y = x + (A−1)·AP₂(x)`，`A = 10^(dBgain/20)`，中心频率为上表。**逐帧逐通道**串接（0x101870d0）。

**Q 的用法**：`Q` 作为参数读出（0x10186e28 处经 `[vtable+0x1c]`/0x101c6da0 后传入 0x101ac5a0 的 Q 形参），进入 `tan(w·Q·0.5)` 的全通极点设计。另有一处 0x10189540 把 **Q 硬编码为 `0x3F9CC471` = 1.2247449**（=`sqrt(1.5)`）传给同一系数函数；`.aep` 的 `Q=1.22474` 与该硬编码默认值一致。

**固定项排查**：`|gain|<0.001` 与 `2f≥SR` 走旁路（线性直通），**无固定 preamp、无固定 highpass/lowpass/shelf**；0 dB 频段被跳过。

---

## 5. Gain 插件（`id4`）

**定位**：`"Gain"`@0x1026a438 → 构造 VA **0x1012bcd0**（vtable **0x102268D4**）：

```
1012bd8c  push 0x1026a438 "Gain" ; call 0x101286a0   ; 注册
1012bdad  call getParam("Gain")                       ; 读原值
1012bdbd  call 0x101006f0 ; fstp dword [esi+0x3a8]     ; 转换后存为线性增益
```

**转换函数 0x101006F0**：

```
101006f8  divss xmm1,[0x10271d78]   ; /20.0
10100700  movsd xmm0,[0x10271d28]   ; 10.0
1010070b  call 0x101eede0           ; pow(10, g/20)
```

→ `linear = 10^(Gain/20)`，即 **Gain 参数单位为 dB，被转换为线性幅度**后再作用于链上该节点。默认表（0x1029E858 处块）含 `-96.0 / 18.0`，即典型增益范围 **[-96, +18] dB**。
预设 `Gain = 2.27887 dB → ×1.300`。

---

## 6. 隐藏处理排查

### 6.1 「ATF」是否为真实算法 —— **否，未发现任何 ATF 实现**

对 x86/x64、ASCII/UTF-16 全量检索 `ATF`（含大小写），**唯一匹配是单词 `PLATFORM`**（`pl-ATF-orm`）。既无 `"ATF"` 独立字符串、无 RTTI 类名、无相关参数名或资源文件。官方「独创 ATF 增强算法」在本引擎中**无可对应模块**，判断为营销话术。

### 6.2 串音消除 / 声场类标记（CrossCancellation、SoundFieldGain、HeadRadius…）的归属

这些字符串属于 **HRTF/声场类插件**，与本预设无关：

- `NeedCrossCancellation`@0x1022B514、`OriginalSoundGain`@0x1022B500、`HeadRadius`@0x1022AA84、`HRTF`@0x1022AD94：引用集中在 **0x1002xxx / 0x10144217 / 0x10144763 / 0x10144d5e …** 同一组函数，属 `Ultra Wide Sound Field`（`HRIRDataFile/HRIR`）与 `5.1 Panoramic` 系列。
- `SoundFieldGain`、`ss_wide_soundfield`、`SUPERSOUND_WIDESOUNDFIELD_TYPE`：属 `ss_wide_soundfield`（`QMCPCOM`）另一套 `get_effect_stream` 内置效果 API；`ss_3d_surround` / `panoramic_51` 同理。
- 全景环绕预设走的是 QMAEP 插件链，**不经过**上述 HRTF/wide-soundfield 模块。

### 6.3 Freeverb / 混响 / 限幅 / Dither / 重采样

- **Freeverb**：无 `freeverb` 字符串；Freeverb 特征梳状调谐（1116/1188/1277/1356/1422/1557…）未构成连续数组（个别 dword 命中系巧合，如 1422、1557 不存在）。存在的是 **MVerb**（`mverb`@0x1022A44C，参数 DAMPINGFREQ/DENSITY/BANDWIDTHFREQ/DECAY/PREDELAY/EARLYMIX/GAIN/LOW/…）与 HRTF 卷积，均**未被本预设调用**。
- **限幅器**：存在插件类 `LimiterEffect@LIMITER@SUPERSOUND2`（vtable 0x10226BE0），但其 vtable 仅在自身构造/工厂（0x1012c7d0、0x1012ca6e）被引用，**没有无条件插入到效果链**。
- **Dither**：无任何 `dither`/`noise shaping` 字符串 —— 不存在。
- **重采样**：`Resampler::…`、Speex/RubberBand、`PushSincResampler` 等仅用于采样率转换（引擎级），不是随预设附加的处理。

### 6.4 结论分类

- **(a) 全景环绕的链 = 恰好 `.aep` 里的 4 个节点**（EQ + Delay + Stereo Enhancer + Gain），未发现被静默追加的插件；预设层面另有可选 `master_gain` 配置字段（数据驱动）。
- **(b) 插件内部固定行为**（均为算法固有、非隐藏）：
  1. Stereo Enhancer：mid/side 各乘固定 `0.5`；系数 = 参数/100。
  2. Delay：`100% 湿声`输出；时间 `ms→samples`、反馈 `%→系数`；**无滤波**。
  3. EQ：`A=10^(dB/20)`、`Q=1.2247`（默认/sqrt(1.5)）、`|gain|<0.001` 与 Nyquist 旁路；无 preamp/HP/LP。
  4. Gain：`10^(dB/20)` 线性化。
- **(c) 全局固定后级**：在 qmcpcom 内**未找到**无条件挂载的限幅/抖动/滤波后级；最终输出音量/限幅若存在，位于播放器/渲染层（如 `QQMusicAudioRender.dll`、`MVVolumeProcess.dll`）——**超出本 DLL 范围，未证实**。

---

## 7. 与 `.aep` 默认参数交叉验证

| 参数 | `.aep` 值 | DLL 默认/范围证据 | 结论 |
|---|---|---|---|
| Width | 130 | 描述块 0x1029F940 = {400.0, 400.0, 1.0, 100.0} | 百分比，最大值含 400，默认 100；进程内 /100 |
| Center | 100 | 同上（默认 100） | 百分比，默认 100 |
| Left/Right Time | 0 / 26.1224 | 向量 @0x10226570 = {0.0, 1000.0, 1000.0, 1.0}，另含 260.0 | **毫秒**；samples=ms/1000·SR（26.1224ms@44.1k=1152 样本） |
| Left/Right Feedback | 0 / 0 | 向量 @0x102277B0 = {0.0, 120.0, 120.0, 1.0}，另含 10.0 | **百分比**（上限 120%），系数=值/100 |
| Q | 1.22474 | 硬编码常量 0x3F9CC471=1.2247449（=sqrt(1.5)），默认向量 @0x102ADA48 附近 | peaking 设计 Q，默认 1.2247 |
| 频段 | 31Hz..16kHz | 表 @0x10230C60 = 31.25/62.5/125/250/500/1k/2k/4k/8k/16k Hz | `%d` 截断显示 |
| Gain | 2.27887 | 范围块 0x1029E858 含 -96.0 / +18.0 | **dB**，linear=10^(dB/20)，预设≈×1.30 |

（描述块字段布局未完全逆向，`{…}` 为原始常量，语义标注存在不确定性。）

---

## 8. 方法摘要（可复现）

1. `strings -a/-el -t x` 导出两版 DLL 全量字符串并分类；参数名用 pefile 映射 文件偏移→RVA→VA。
2. 用 `find_ptr(VA)`（4 字节小端搜索）定位参数字符串的代码交叉引用 → 构造函数。
3. 构造函数中 `mov dword [reg], imm` 抓取 vtable 指针 → 从 vtable 找专属虚函数槽。
4. 对 vtable 各槽用 capstone 反汇编定位 Process / 系数函数；读取引用常量（float/double）确认单位与公式。
5. 用 RTTI（TypeDescriptor→CompleteObjectLocator→vtable）校验类身份。
6. 直接解析 `.aep` 条目（`04 00 00 00|float|strlen|name`）做交叉验证。
7. 脚本：`输出\逆向\{pe_helper.py, d.py, rtti.py}`。

## 9. 不确定性 / 未决

- 参数描述块（default/min/max/step）的精确字段布局未完全确定，上表 `{…}` 为原始常量，请按「量级/单位」理解。
- 0x10189540 使用硬编码 Q=1.2247；IirEQ10 另从 `Q` 参数读取并传入系数函数。两者数值一致，无法仅凭静态区分「预设 Q 是否真的改变算法」还是「恰好等于默认」——倾向后者为默认值，但标注为不确定。
- 未追踪效果链的**连接顺序/增益节点位置**的完整图；`master_gain` 的实际取值未从 FlatBuffers 中解出。
- 输出级是否存在播放器层的全局限幅/音量，属 qmcpcom 之外，未验证。
- 全部分析为静态推断，**未运行 DLL/插件**；个别 vtable 槽的语义（如 +0x1c 返回值）依赖调用上下文推断。
