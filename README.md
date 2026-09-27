# ViPER-QQFX-Port

**把 QQ 音乐 PC 版的音效（`.aep`）转成 ViPER4Android 能直接导入的预设。**
QQ 音乐里那 35 个推荐音效（全景环绕、超重低音、各种房间混响……）本仓库已经全部转好，
自带配套卷积内核，你不用懂逆向、不用装工具，把文件拷进手机、导入即可听。

> 这是给普通用户的使用说明。想研究转换原理 / 自己加规则，请看
> [工作原理（简版）](#工作原理简版) 与 [docs/开发指南.md](docs/开发指南.md)。

---

## 目录

1. [这是什么](#这是什么)
2. [30 秒快速开始](#30-秒快速开始)
3. [预设一览表](#预设一览表)
4. [工作原理（简版）](#工作原理简版)
5. [从源码重建（可选）](#从源码重建可选)
6. [FAQ / 故障排查](#faq--故障排查)
7. [已知局限](#已知局限)
8. [版权与致谢](#版权与致谢)
9. 附录（进阶）：[A 仓库结构](#附录-a仓库结构) ·
   [B 映射规则表](#附录-b映射规则表进阶) ·
   [C 逆向要点](#附录-c逆向要点与-ir-解密进阶)

---

## 这是什么

QQ 音乐 PC 客户端有一批官方调好的音效（存成私有的 `.aep` 文件）。本仓库把其中的
**35 个音效转成了 ViPER4Android（配套 ViPERFX_RE Magisk 模块的 `com.llsl.viper4android` app）
可导入的 JSON 预设**，并把转换工具链与逆向分析报告一并开源。你只需要「拷文件 → 导入」，就能在
ViPER4Android 里用上这些 QQ 音效。

---

## 30 秒快速开始

用行话前先给一句话解释：**ViPER4Android（V4A）** 是安卓上著名的音效增强 app；**内核（kernel）**
是某些音效（如"录音棚环绕"）需要的卷积音频文件，要单独放进 app 的目录。

**前提**：手机已 root，并已装好两个东西——ViPERFX_RE Magisk 模块、`com.llsl.viper4android` app。
没有 root / 模块，下面都跑不起来。

按顺序做，每步一个动作：

1. **拿文件**：下载本仓库 Release 里的压缩包；或直接下载仓库的 `presets/` 与 `kernels/` 两个目录。
2. **拷内核**：把 `kernels/441/` 里的 `.wav` **全部**拷到手机
   `Android/data/com.llsl.viper4android/files/Kernel/`。
   （想用 48 kHz 就见下方「441 还是 48k」，改拷 `kernels/48k/` 那一套。）
3. **拷预设**：把 `presets/*.json`（想要哪些拷哪些，也可以全拷）拷到手机存储**任意位置**。
4. **导入**：打开 V4A app → **设置（Settings）** → **Files** → **Import preset（导入预设）**，
   选中刚拷进去的 `.json`（支持多选，会逐个导入）。
5. **开总开关**：导入**不会**自动打开 app 顶部的**总开关**（`masterEnable`），请手动打开，
   否则听不到任何效果。
6. **选内核**：如果你导入的是「需要内核」的音效（见下表），进 app 的**卷积器**界面，
   确认已选中与音效同名的内核（例如"录音棚环绕"选 `录音棚环绕_441`）。
7. **听**。

### 441 还是 48k？

`kernels/` 里每个内核都有两套采样率：`441/`（44.1 kHz）和 `48k/`（48 kHz），内容一样，只是采样率不同。

- **不确定就用 `441/`**。V4A 的卷积 DSP 默认按 44100 Hz 工作，这是最稳的默认。
- **手机音频会话是 48 kHz（多数新手机）且在意细节，用 `48k/`**。原因一句话：
  若内核是 44.1 kHz 而会话是 48 kHz，混响时基会被拉短约 8%（听感上尾巴略短）。

预设里的内核名默认带 `_441` 后缀；如果你装的是 `48k` 那一套，两种办法二选一：
在 app 卷积器界面手动重选同名 `_48k` 内核，或把预设 json 里的 `_441` 改成 `_48k` 再导入。

---

## 预设一览表

35 个预设全部通过 schema 校验（`validation: pass`），可直接导入。数据取自
[manifest.json](manifest.json)（`confidence` / `mapped_groups` 字段）。

- **保真度**：`verified` = 语义精确对应；`approx` = 跨算法近似；`partial` = 只映射了一部分节点、
  其余节点对不上（原因见 [附录 B](#附录-b映射规则表进阶) 与 `parsed/*.parse.json`）。
- **需要内核**：= 该预设用到 `convolver`，请把 `kernels/` 对应 wav 拷进手机 Kernel 目录。

| 预设（`presets/…`） | 映射到的 V4A 效果 | 保真度 | 需要内核 |
|---|---|---|---|
| `QQ音乐-全景环绕.json` | 差分环绕 + 10 段 EQ + 立体声宽度 + 总增益 | verified | 否 |
| `001-差分环绕.json` | 卷积 + 总增益 | partial | **是**（差分环绕） |
| `002-3D人声.json` | 立体声宽度 | partial | 否 |
| `007-录音棚环绕.json` | 卷积 + 总增益 | approx | **是**（录音棚环绕） |
| `008-小编制.json` | 卷积 + 动态 EQ + 总增益 | approx | **是**（小编制） |
| `009-民谣.json` | 卷积 + 总增益 | partial | **是**（民谣） |
| `010-流动低音.json` | 卷积 | approx | **是**（流动低音） |
| `011-现场环绕.json` | 卷积 + 总增益 | approx | **是**（现场环绕） |
| `012-复合低音.json` | 卷积 + 总增益 | partial | **是**（复合低音_超高保真） |
| `013-超高保真.json` | 卷积 | partial | **是**（复合低音_超高保真） |
| `014-留声机.json` | 总增益 | partial | 否 |
| `015-清澈旋律.json` | 卷积 + 总增益 | approx | **是**（清澈旋律） |
| `016-3D音效.json` | 总增益 | partial | 否 |
| `017-震撼低音.json` | 卷积 + 总增益 | approx | **是**（震撼低音） |
| `018-极重低音.json` | 卷积 + 总增益 | approx | **是**（极重低音） |
| `019-摇滚.json` | 动态 EQ | partial | 否 |
| `020-中国风.json` | 动态 EQ | partial | 否 |
| `050-KTV房间.json` | 混响 | approx | 否 |
| `051-浴室.json` | 混响 | approx | 否 |
| `052-起居室.json` | 混响 | approx | 否 |
| `053-音乐厅.json` | 混响 | approx | 否 |
| `054-小巷子.json` | 混响 | approx | 否 |
| `055-停车场.json` | 混响 | approx | 否 |
| `056-厨房.json` | 混响 | approx | 否 |
| `057-健身房.json` | 混响 | approx | 否 |
| `058-工作室.json` | 混响 | approx | 否 |
| `059-自习室.json` | 混响 | approx | 否 |
| `060-地铁.json` | 混响 | approx | 否 |
| `061-图书馆.json` | 混响 | approx | 否 |
| `065-智能伴奏.json` | 动态 EQ | partial | 否 |
| `501-超重低音.json` | 低音 | approx | 否 |
| `504-现场律动.json` | 卷积 + 混响 + 差分环绕 + 10 段 EQ + 宽度 + 总增益 | approx | **是**（现场律动） |
| `996-智能音效.json` | 动态 EQ + 立体声宽度 + 总增益 | partial | 否 |
| `997-明星音效.json` | 立体声宽度 + 总增益 | partial | 否 |
| `998-设备音效.json` | 动态 EQ + 立体声宽度 + 总增益 | partial | 否 |

> 汇总：35 个预设中，**12 个需要内核**（001/007/008/009/010/011/012/013/015/017/018/504），
> 涉及 12 个不同的卷积内核文件；`kernels/` 共 14 个内核（另 2 个是采样素材，见附录 C）。
> 50 个推荐音效里另有 16 个因「没有可对应的 V4A 效果」不产出预设（如 004-黑胶、062-外放环绕、
> 808-睡眠音效等），原因逐条记在 manifest.json。

---

## 工作原理（简版）

- `.aep` 是 QQ 音乐的私有音效文件（内部标记 `QMAEP`，一种 FlatBuffers 风格格式），
  里面是一串**效果节点**：EQ、增益、延迟、卷积 IR、混响、动态增强……
- 转换器把每个节点翻译成 V4A 预设里对应的**效果组**（`equalizer` / `reverb` / `convolver` /
  `dynamicEq` / `stereoImager` / `diffSurround` / `bass` / `masterLimiter`）。
- 能精确对应上的标 **verified**；跨算法只能近似的标 **approx**；只转出一部分、其余节点对不上的标
  **partial**；完全对不上的节点不硬凑，记下原因后跳过。
- 卷积类音效用到 QQ 加密的 IR 文件（`.enc`）。本仓库已把它的解密算法完整还原，并预处理成
  V4A 能读的 2 声道 wav 内核（含重采样、4 声道降混）。
- `masterEnable`（总开关）**不属于预设内容**，所以导入后要手动开。

细节见 [docs/开发指南.md](docs/开发指南.md) 与 `docs/` 下的逆向报告。

---

## 从源码重建（可选）

需要 Python 3。安装依赖：

```bash
pip install -r requirements.txt      # 目前只有 numpy（内核重采样用）
```

常用工具，一条命令一个例子：

```bash
# 解析 .aep（打印结构；--dir 可做全语料 smoke test）
python tools/aep_parser.py --dir aep

# 重建卷积内核：自动从腾讯 CDN 下载 .enc、解密、做 SS2 加载器后处理，输出 441/ 与 48k/ 两套
python tools/build_kernels.py

# 单文件转换：.aep -> V4A 预设（同时产出 <out>.parse.json 解析/决策 dump）
python tools/convert_aep.py aep/500-全景环绕.new.aep -o out/全景环绕.json

# 全量转换：aep/*.aep -> presets/*.json + parsed/*.parse.json + manifest.json
python tools/batch_convert.py

# 校验预设（组序/字段/类型/range/默认值）
python tools/validate_preset.py --preset presets/*.json
```

顺序提示：`build_kernels.py` 必须在 `batch_convert.py` **之前**跑，否则预设里的 `convolver`
不会启用（找不到对应内核）。完整依赖关系与工具清单见
[docs/开发指南.md](docs/开发指南.md)。

---

## FAQ / 故障排查

**Q：导入失败 / 文件选不中。**
预设必须是 `.json` 扩展名的 JSON 文件；app 的文件选择器按 `application/json` 过滤，
别把扩展名改成 `.txt` 之类。

**Q：导入成功了，但完全没声音 / 没效果。**
按顺序排查：① app 顶部**总开关**没打开（导入不会自动开，这是最常见原因）；② 该音效需要内核，
而你没把 wav 拷进 `Android/data/com.llsl.viper4android/files/Kernel/`；③ 内核**名字对不上**
（预设指向 `xxx_441`，你装的是 `xxx_48k`）——在卷积器界面重选内核，或改 json 里的后缀。

**Q：某些效果有效、卷积那部分没效果。**
几乎都是内核问题：文件没拷对目录、文件名不匹配、或拷贝中断导致 wav 损坏。
重新从 `kernels/` 拷一份对应采样率的 wav 即可。

**Q：混响/空间感听着有点"短"或"怪"。**
多半是 441 / 48k 采样率不匹配，按上文「441 还是 48k」换成与手机会话一致的那一套。

**Q：一加载卷积核，音量突然变得超级大。**
先确认你用的不是 **v1.0.0 的 48k 包**——那版 48k 内核重采样时有 bug（低频被放大最多约
16 倍），**升级到 v1.0.1+ 重新拷贝 `kernels/` 即可**。排除后仍觉得偏响属正常：QQ 和 V4A
都不对卷积核做归一化，卷积本身会抬电平，QQ 靠链末限幅器兜底，V4A 由主限幅器兜底——
建议先把媒体音量调小再开效果。另外 `采样素材_*.wav` 是效果内部播放用的采样素材，
**不是卷积核**，别手动选进卷积器。

**Q：需要 root 吗？**
需要。V4A 依赖 root + Magisk 模块（ViPERFX_RE），并在 app 内安装驱动后才生效；未 root 无法使用。

**Q：为什么有些 QQ 音效没有对应预设？**
有些音效（3D 声场、人声分离、变调、采样器等）在 V4A 里没有语义等价的效果，硬转会失真，
因此如实跳过并在 `manifest.json` 里写明原因，而不是凑一个假预设。

---

## 已知局限

| 项 | 说明 |
|---|---|
| Right Time 26.12 ms → 20 ms | app `diffSurround.delay` range 为 1..20 ms，导入时被钳制（听感差异小） |
| `Q` 未映射（id13） | V4A 10 段 EQ 为固定 Q 最小相位 IIR，无 Q 入参 |
| `Center≠100` | V4A `stereoImager` 只缩放 side，mid 增益不可表达，转换时记 warning |
| 滤波器→dynamicEq 为近似 | id51/50/33/34/35/24 用 `dynamicEq`（threshold=−80 恒生效）近似静态滤波器；V4A 无带通类型（id31 跳过），id35 的 Q 由频率边缘推算 |
| id63 MVerb→reverb 为近似 | 用 `roomSize←DECAY / damp←1−DAMPINGFREQ / wet←MIX / dry←1−MIX / width=1.0`；`SIZE/DENSITY/BANDWIDTHFREQ/PREDELAY/EARLYMIX/GAIN` 无对应 |
| id57 SuperBass→bass 为近似 | V4A `bass` 为动态低音/次谐波结构，与 QQ SuperBass 算法不同 |
| id2 StudioIr→convolver | `.enc` 已可完整解密，14 个内核全部补齐；IR 为 2/4 声道，V4A 卷积器按立体声处理（4ch 已取对角降混） |
| 012 双 IR 串联塌缩 | 012 串联两个 id2 卷积，V4A 卷积器仅单级，**后者覆盖前者**（`复合低音_备选` 被弃用），属结构性近似 |
| 065 的 −12 dB 钳制 | id51 两个频段 gain 源值 −19 dB 超过 dynamicEq 下限 −12 dB，被钳到 −12（削减不足，明显） |
| 大量节点不映射 | id9 `Exciter`、id14/15/16/18/19 DFX 系列、id21/59 人声、id22 `HyperBass`、3D/5.1/人声分离/QTSEffect/PitchShifter/Chaos/Rotator/Sampler 等无合理对应，原因见各 `<名>.parse.json` |
| `Gain` 浮点噪声 | `10^(G/20)` 由 float32 `Gain` 计算，如 `1.2999999583` 而非精确 `1.3`（差 ~4e-8，可忽略） |
| 空内置名回退 | 050-061 房间系列 `.aep` 内置效果名为空，预设名回退为文件名主干 |

---

## 版权与致谢

- `.aep` 音效文件与 IR（`.enc`）内容版权归**腾讯（QQ 音乐）**所有。本仓库仅用于
  **个人学习与互操作性研究**，请勿商用或再分发；如需使用请自行获取授权。
- 致谢：
  - **audioeffect-qm**（`AllenHeartcore/audioeffect-qm`）：`.enc` 解密算法的早期线索（128 字节表近似）。
  - **ViPERFX_RE / ViPERDSP**：V4A 的 DSP 实现，本仓库映射语义以其源码为准。
  - **ViPER4Android**（`com.llsl.viper4android`）：预设格式（schemaVersion 2.1）与导入/内核下发流程。
- 本仓库不包含 `ViPER4Android-main` / `ViPERFX_RE-dev` 源码；DLL 逆向结论为原创静态分析结果。

---

## 附录 A：仓库结构

```
ViPER-QQFX-Port/
├── README.md                # 本文件（用户说明 + 进阶附录）
├── manifest.json            # 批量转码汇总清单（每文件置信级/映射组/unmapped/校验结果）
├── requirements.txt         # Python 依赖（numpy）
├── aep/                     # 语料：51 个 .aep（50 推荐音效 + 新版 500）+ recommendbase.json
│   ├── 001-差分环绕.aep ... 999-编辑音效.aep
│   ├── recommendbase.json   # 官方元数据（效果描述/标签/IR CDN 直链）
│   └── 500-全景环绕.new.aep # 新版（比旧版多一个 Gain 节点）
├── docs/                    # 逆向报告与开发指南
│   ├── 开发指南.md              # 知识地图 + 如何加映射 + 如何重建（开发者入口）
│   ├── 分析报告.md              # 「全景环绕」首轮逆向记录（格式解析 + 映射决策）
│   ├── 加载逻辑分析.md          # 引擎加载链 / 76 项插件注册表
│   ├── DSP内部处理分析.md       # 各插件 DSP 内部处理逆向
│   ├── IR解密分析.md            # .enc IR 完整解密算法 + 验证数据
│   ├── SS2引擎分析.md           # libSuperSound2 引擎静态分析 + 内核构建流水线
│   ├── 范围核查报告.md          # 超出 V4A 支持范围的专项审计（历史快照）
│   ├── effect_registry.json     # 76 项 id → 类名注册表
│   └── extract_registry.py      # 注册表提取脚本（历史成果）
├── tools/                   # 工具链（8 个脚本，见 docs/开发指南.md）
│   ├── aep_parser.py            # 通用 .aep 解析库 + smoke test
│   ├── parse_aep.py             # 历史：500 专用解析器（含自检断言）
│   ├── v4a_schema.json          # 24 组 V4A schema 快照
│   ├── gen_v4a_schema.py        # 由 EffectGroups.kt 生成 v4a_schema.json
│   ├── validate_preset.py       # 基于 schema 的独立预设校验器
│   ├── convert_aep.py           # 单文件转换（映射规则/置信级在此）
│   ├── batch_convert.py         # 批量转换 + manifest 汇总
│   ├── build_kernels.py         # 卷积核构建流水线（解密+后处理+双速率+可读命名）
│   └── decrypt_ir.py            # QQ .enc IR 解密器（完整算法）
├── presets/                 # 预设成品（35 个，全部由批量生成，全部 pass）
├── parsed/                  # 解析 dump（35 个 <名>.parse.json，含映射决策/警告）
└── kernels/                 # 双速率内核（441/ 与 48k/ 两套）+ kernel_index.json
```

> 完整中间产物（DLL 字符串/反汇编中转文件、临时逆向脚本等）保留在工作区 `output/逆向/`，
> 未纳入本仓库。

---

## 附录 B：映射规则表（进阶）

置信级：**verified** = 有 DLL/DSP 源码双重证据、语义精确对应；**approx** = 语义清晰但跨算法结构，
标注近似；**skipped** = 无合理对应，仅在 dump/manifest 记录原因。

| QQ 节点 | V4A 目标 | 规则 | 置信级 |
|---|---|---|---|
| id4 `Amplifier` `Gain`(dB) | `masterLimiter.outputVolume` | `10^(G/20)`，clamp 0.01..2.0 | **verified**（线性级可交换） |
| id13 `IirEQ10` 10 段 | `equalizer{enable,bandCount:10,bands:升序}` | 增益照抄，越界 ±12 clamp；**Q 不映射** | **verified**（频率同源；Q 不可表达） |
| id11 `StereoEnhancer` `Width`(%) | `stereoImager` 三频段 `width` | `W/100`，clamp 0..2；`Center≠100` 记 warning | **verified**（`Center=100` 时） |
| id12 `Delay` 单边 Time>0 且 Feedback=0 | `diffSurround` | `delay=clamp(t,1,20)`、`reverse=(Left>0)`、`wetDryMix=1.0` | **verified**（26.12 ms 被钳到 20 ms） |
| id63 `Mverb` | `reverb` | `roomSize=DECAY`、`damp=1−DAMPINGFREQ`、`wet=MIX`、`dry=1−MIX`、`width=1.0` | **approx**（Freeverb↔MVerb 结构不同） |
| id2 `StudioIr` | `convolver{kernelFile}` | 仅当对应 kernel 存在时启用；4ch IR 取对角降为立体声；kernelFile 指向 441 版可读名 | **approx**（算法已完全解密，见附录 C） |
| id51/50 `Peaking/HighShelfFilterQ`、id33/34 `LS/HSFilter`、id35 `PKFilter`、id24 `SuperEQ` | `dynamicEq` | 频段按文件顺序合并：`freqs` clamp 20..20000、`qs` clamp 0.5..8、`gains` clamp −12..12、`filterTypes` 0=peak/1=low shelf/2=high shelf；`thresholds` 恒设 −80（恒生效） | **approx**（动态 EQ 近似静态滤波器；id35 f0=√(lo·hi)、Q=f0/(hi−lo)；id24 Q 取 1.0） |
| id57 `SuperBass` | `bass` | `frequency` clamp 15..150、`gain` clamp 0.5..10 | **approx**（V4A bass 为动态低音/次谐波结构） |
| 其余节点 | 不映射 | dump 节点 id/类名/参数 + 具体原因入 warning | **skipped** |

> **`dynamicEq` threshold 极性结论（源码实证）**：ViPERDSP `DynamicEQ.cpp` 中
> `overshoot = envelope_dB − threshold`，仅当 `overshoot > 0` 时按比例施加目标增益。因此
> `threshold = −80`（schema 下限）对任何电平高于 −80 dBFS 的信号恒生效，可用来承载本应静态的 EQ 频段。
> 滤波器类型枚举同文件 `SetBandFilterType`：0=PEAK、1=LOW_SHELF、2=HIGH_SHELF。
>
> **MVerb 参数语义（原作源码实证）**：`MVerb.h` 中 `DampingFreq*18400+100` 是反馈支路低通截止频率
> （越高越亮、阻尼越少），故 V4A `damp = 1 − DAMPINGFREQ`；`Decay` 为反馈增益，对应 `roomSize`；
> `MIX` 为干湿交叉淡入，故 `wet=MIX, dry=1−MIX`。`SIZE/DENSITY/BANDWIDTHFREQ/PREDELAY/EARLYMIX/GAIN`
> 无对应项，记 warning。`reverb.width` 取 1.0（MVerb 湿声为立体声；V4A `width=0` 会退化为单声道湿声）。

---

## 附录 C：逆向要点与 IR 解密（进阶）

### C.1 `.aep` 格式（QMAEP）

`.aep` 是 QQ 音乐私有音效文件，魔数 **`QMAEP`**，为 **FlatBuffers 风格的自定义变体**：

```
文件起点:  uoffset32 -> root table
root table (3 fields):
    field0 -> string "QMAEP"        (magic)
    field1 -> string 效果名          (UTF-8, 前缀 u32 长度)
    field2 -> vector<EffectNode>
EffectNode table (2 fields):
    field0 -> int32   node id
    field1 -> vector<Param>
Param table (3 fields):
    field0 -> string name
    field1 -> value blob: u32 长度 + 原始字节
                长度 == 4 -> 小端 float32 标量（绝大多数参数）
                其他      -> UTF-8 字符串（如 IR 文件名 "irs\\xxx.enc"）
    field2 -> string unit / 备用（通常为空串）
```

与标准 FlatBuffers 的差异：文件根多一段魔数字符串字段，参数采用「name 字符串 + 带长度前缀的值 blob」
形式，不能用通用 FlatBuffers 库直接读取，需按其 schema 遍历。逐字节 hex 证据见
[docs/分析报告.md](docs/分析报告.md) §1。

### C.2 引擎侧结论（`qmcpcom(.dll/x64.dll)` + `libSuperSound2.dll`）

- 纯静态逆向（pefile + capstone + RTTI，未运行任何二进制）还原出 **76 项插件注册表**（id → 类名），
  见 [docs/effect_registry.json](docs/effect_registry.json)。
- **实际处理链 = `.aep` 节点序列 + 链尾无条件追加的固定 `Limiter`(id=6)**。
- 关键插件语义（已验证）：id4 Amplifier 的 `Gain` 参数单位为 **dB**，设置路径 `line=10^(G/20)`；
  id11 StereoEnhancer 为纯 mid/side 矩阵；id12 Delay 延迟支路 100% 湿声、不做任何滤波；
  id13 IirEQ10 为 10 段 Regalia-Mitra peaking（Q 为参数，|gain|<0.001 旁路）；
  官方宣传的 "ATF 增强算法" 在 DLL 中**不存在**。
- `libSuperSound2.dll` 是 SS2 引擎本体：卷积（IPP FFT）、重采样（SoundTouch）、混响、限幅、
  EQ/滤波、采样器等 DSP 全部由它承担；`qmcpcom` 只是把效果 id 映射到 SS2 工厂的宿主封装。

证据见 [docs/加载逻辑分析.md](docs/加载逻辑分析.md)、[docs/DSP内部处理分析.md](docs/DSP内部处理分析.md)、
[docs/SS2引擎分析.md](docs/SS2引擎分析.md)。

### C.3 语料与批量结果

51 个 `.aep` 文件全部解析成功，共 33 种节点序列。批量转码（`tools/batch_convert.py`）结果：

| 指标 | 值 |
|---|---|
| 输入文件 | 51 |
| 产出预设（`presets/*.json`） | **35**（校验全部 pass） |
| 不产出预设（全 skipped） | 16 |
| 置信级分布 | **verified 1 / approx 21 / partial 13 / skipped 16** |

- **verified 1**：`500-全景环绕.new.aep`（节点 4/11/12/13）。旧版 `500-全景环绕.aep`（节点 11/12/13）
  仍可映射但被人工跳过（预设已被新版取代），仅在 manifest 记 skipped。
- **skipped 16**：无任何可映射节点，不落盘：004/062/064/502/503/505/506/600/601/602/807/808/809/995/999，
  另加人工跳过的旧版 `500-全景环绕.aep`。
- 各 V4A 组被产出的预设数：`masterLimiter 16, reverb 13, convolver 12, stereoImager 6,
  dynamicEq 6, equalizer 2, diffSurround 2, bass 1`。

### C.4 IR（`.enc`）完整解密

`id2` 的 `IR File` 指向 `irs\<sha1>.enc` 加密 IR。公开仓库 `audioeffect-qm/qmae/decrypt.py`
只用一段 128 字节 XOR 密钥表无限循环，长文件在 `0x8000` 之后损坏（作者注明 "structure unclear"）。
本仓库对引擎 DLL 做**纯静态逆向**，恢复了**完整算法**：

- **流格式**：`.enc` 不是容器，而是「裸 WAV 明文 ⊕ 密钥流」的 XOR 流密码，逐字节处理。
- **代码位置**：`libSuperSound2.dll` 导出 `SUPERSOUND2::decrypt_file` @ VA 0x10052400，
  按 0x80000 字节分块，核心例程 0x1001fb70 对块内偏移 `i`：

  ```
  q        = i            if i <= 0x7fff
             i % 0x7fff   otherwise
  index    = (q*q + 0x13c1b) & 0xFF
  plain[i] = enc[i] XOR KEY256[index]
  ```

  `KEY256` 为 DLL VA 0x1005f788 处的 256 字节常量（已内嵌进 `tools/decrypt_ir.py`）。
- **为什么公开算法会在 0x8000 后损坏**：因为 `(i+128)² ≡ i² (mod 256)`，`i ∈ [0,0x7fff]` 内
  `index` 只依赖 `i mod 128`，所以前 ~32 KiB 恰好是「128 字节表循环」；`i>0x7fff` 后改按
  `i mod 0x7fff` 取 `q`，相位漂移，再叠加 0x80000 分块重置——公开算法缺这两点，故长文件后段全乱。
- `tools/build_kernels.py` 已内嵌完整算法与 SS2 加载器后处理语义。**本语料 12 个 IR 的
  Trim/Fade 均不激活**（缺省 0；仅 018/504 显式 Trim=−100，也在生效区间外），故 QQ 引擎按全长 IR
  卷积，与直接使用解密产物一致。
- 参考仓库 19 个 `.enc` 中，全部浮点 IR 用本算法解密后 **0 个非有限值**；本地/参考语料的 14 个内核
  全部通过硬判据（float 有限 / `|x|≤8` / `data` 长度一致 / 无满幅段），0 失败。

详见 [docs/IR解密分析.md](docs/IR解密分析.md)。
