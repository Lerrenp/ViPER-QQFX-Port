# ViPER-QQFX-Port

QQ 音乐音效（`.aep`）→ ViPER4Android（`com.llsl.viper4android`，schemaVersion 2.1）预设转换器。

本仓库把「QQ 音乐 PC 音效引擎格式逆向 + DLL 静态验证 + 预设转换成果」整理为可复现的
工具链与语料，目标是把 QQ 音乐的推荐音效批量转成 ViPER4Android 可导入的 JSON 预设。

---

## 1. 成果摘要

### 1.1 `.aep` 格式逆向结论

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
                长度 == 4 -> 小端 float32 标量
                其他      -> UTF-8 字符串（如 IR 文件名，带结尾 '\0'）
    field2 -> string unit / 备用（通常为空串）
```

与标准 FlatBuffers 的差异：文件根多一段魔数字符串字段，参数采用「name 字符串 +
带长度前缀的值 blob（float/字符串）」形式，不能用通用 FlatBuffers 库直接读取，需按其
schema 遍历。逐字节验证与 hex 证据见 [docs/分析报告.md](docs/分析报告.md)。

### 1.2 DLL 静态逆向结论（引擎 `qmcpcom(.dll/x64.dll)`）

对 QQ 音乐 PC 客户端音效引擎做**纯静态**分析（pefile + capstone + RTTI，未运行任何二进制）：

- 从 RTTI 还原出 **76 项插件注册表**（id → 类名），见
  [docs/effect_registry.json](docs/effect_registry.json)；
- **实际处理链 = `.aep` 节点序列 + 链尾无条件追加的固定 `Limiter`(id=6)**；
- 「全景环绕」实际链 = **Amp(4) → StereoEnhancer(11) → Delay(12) → IirEQ10(13) → Limiter(6)**；
- **无参数之外的隐藏处理**：StereoEnhancer 为纯 mid/side 矩阵；Delay 延迟支路 100% 湿声、
  不做任何滤波；IirEQ10 为 10 段 Regalia-Mitra peaking（Q 为参数，|gain|<0.001 旁路）；
  宣传的 "ATF 增强算法" 在 DLL 中不存在。

证据见 [docs/加载逻辑分析.md](docs/加载逻辑分析.md)、[docs/DSP内部处理分析.md](docs/DSP内部处理分析.md)。

### 1.3 关键插件语义（已验证）

| id | 类名 | 语义 |
|---|---|---|
| 4 | AmplifierEffect | 参数 `Gain` 单位 **dB**，设置路径 `pow(10, G/20)` 转线性，范围约 [-96,+18] dB |
| 11 | StereoEnhancerEffect | 纯 mid/side 矩阵：`L'=C·mid+W·side, R'=C·mid−W·side`，`C=Center/100, W=Width/100` |
| 12 | DelayEffect | `samples = Time(ms)/1000·SR`，`Feedback/100` 回灌，延迟支路 100% 湿声、无滤波 |
| 13 | IirEQ10Effect | 10 段 peaking（31.25/62.5/125/250/500/1k/2k/4k/8k/16k Hz），Q 为参数，单位 dB |

### 1.4 映射表（`.aep` 节点 → V4A 组）

置信级：**verified** = 有 DLL/DSP 源码双重证据、语义精确对应；**approx** = 语义清晰但跨算法结构，标注近似；
**skipped** = 无合理对应，仅在 dump/manifest 记录原因。

| QQ 节点 | V4A 目标 | 规则 | 置信级 |
|---|---|---|---|
| id4 `Amplifier` `Gain`(dB) | `masterLimiter.outputVolume` | `10^(G/20)`，clamp 0.01..2.0 | **verified**（线性级可交换） |
| id13 `IirEQ10` 10 段 | `equalizer{enable,bandCount:10,bands:升序}` | 增益照抄，越界 ±12 clamp；**Q 不映射** | **verified**（频率同源；Q 不可表达） |
| id11 `StereoEnhancer` `Width`(%) | `stereoImager` 三频段 `width` | `W/100`，clamp 0..2；`Center≠100` 记 warning | **verified**（`Center=100` 时） |
| id12 `Delay` 单边 Time>0 且 Feedback=0 | `diffSurround` | `delay=clamp(t,1,20)`、`reverse=(Left>0)`、`wetDryMix=1.0` | **verified**（26.12 ms 被钳到 20 ms） |
| id63 `Mverb` | `reverb` | `roomSize=DECAY`、`damp=1−DAMPINGFREQ`、`wet=MIX`、`dry=1−MIX` | **approx**（Freeverb↔MVerb 结构不同） |
| id2 `StudioIr` | `convolver{kernelFile}` | 仅当 `kernels/<hash>.wav` 存在（.enc 解密成功）时启用；4ch IR 在 V4A 卷积器中降为立体声 | **approx**（算法已完全解密，见 1.7；四声道 IR 降为立体声） |
| id51/50 `Peaking/HighShelfFilterQ`、id33/34 `LS/HSFilter`、id35 `PKFilter`、id24 `SuperEQ` | `dynamicEq` | 频段按文件顺序合并：`freqs` clamp 20..20000、`qs` clamp 0.5..8、`gains` clamp −12..12、`filterTypes` 0=peak/1=low shelf/2=high shelf；`thresholds` 恒设 −80（恒生效） | **approx**（动态 EQ 近似静态滤波器；id35 f0=√(lo·hi)/Q=f0/(hi−lo)；id24 Q 取 1.0） |
| id57 `SuperBass` | `bass` | `frequency` clamp 15..150、`gain` clamp 0.5..10 | **approx**（V4A bass 为动态低音/次谐波结构） |
| 其余节点 | 不映射 | dump 节点 id/类名/参数 + 具体原因入 warning | **skipped** |

> **`dynamicEq` threshold 极性结论（源码实证）**：ViPERDSP `DynamicEQ.cpp` 中
> `overshoot = envelope_dB − threshold`，仅当 `overshoot > 0` 时按比例施加目标增益
> （`desired_gain_db = target_gain * min(overshoot/12, 1)`）。因此 `threshold = −80`（schema 下限）
> 对任何电平高于 −80 dBFS 的信号恒生效，可用来承载本应静态的 EQ 频段。
> 滤波器类型枚举同文件 `SetBandFilterType`：0=PEAK、1=LOW_SHELF、2=HIGH_SHELF。

> **MVerb 参数语义（原作源码实证）**：`MVerb.h` 中 `DampingFreq*18400+100` 是反馈支路低通截止频率
> （越高越亮、阻尼越少），故 V4A `damp = 1 − DAMPINGFREQ`；`Decay` 为反馈增益（=衰减时间），
> 对应 V4A/Freeverb 的 `roomSize`；`MIX` 为干湿交叉淡入，故 `wet=MIX, dry=1−MIX`。
> `SIZE/DENSITY/BANDWIDTHFREQ/PREDELAY/EARLYMIX/GAIN` 在 V4A reverb 中无对应项，记 warning。
> `reverb.width` 取 1.0：MVerb 湿声为立体声，V4A Freeverb `width=1` 为不交叉立体声湿声（`width=0` 会退化为单声道湿声）。

### 1.5 语料节点构成（`aep/` 51 个文件 smoke test）

51 个文件全部解析成功。节点 id 出现频次（含该 id 的文件数）：
`63:13, 4:16, 2:12, 9:8, 11:7, 3:1, 5:1, 7:2, 12:3, 13:3, 14:3, 15:3, 16:4, 18:3, 19:4, 21:3, 22:1, 24:1, 28:2, 31:1, 33:3, 34:1, 35:1, 40:1, 50:1, 51:3, 55:1, 56:1, 57:1, 58:1, 59:1, 60:1, 62:1, 69:1, 70:1, 71:1, 73:1, 74:1, 75:1`，
共 33 种节点序列。多数节点序列可由扩展后的规则覆盖（见 1.6）。

### 1.6 批量转码结果（`tools/batch_convert.py` 全量 51 文件）

| 指标 | 值 |
|---|---|
| 输入文件 | 51 |
| 产出预设（`presets/*.json`） | **34**（校验全部 pass） |
| 不产出预设（全 skipped） | 17 |
| 置信级分布 | **verified 2 / approx 14 / partial 18 / skipped 17** |

- **verified 2**：`500-全景环绕.aep`（旧版，节点 11/12/13）与 `500-全景环绕.new.aep`（节点 4/11/12/13）。
- **approx 14**：12 个 `[63]` 房间/空间系列 + `501-超重低音`（仅 approx 规则命中，无 skipped 节点）
  + `504-现场律动`（id2 IR 解密成功后 `convolver` 命中，且无 skipped 节点）。
- **partial 18**：既有 verified/approx 命中、又含 skipped 节点的文件（如 019-摇滚、020-中国风、
  996/997/998 等）。
- **skipped 17**：无任何可映射节点，不落盘（仅在 `manifest.json` 记录 `reason`）：
  004/010/013/062/064/502/503/505/506/600/601/602/807/808/809/995/999。

各 V4A 组被产出的预设数：`masterLimiter 16, reverb 13, stereoImager 7, dynamicEq 6, equalizer 3,
diffSurround 3, bass 1, convolver 1`。`unmapped` 节点按**含该节点的文件数**：id2:11、id9:8、id7:2、id16:4、id19:4、id3:1、id14:3、
id15:3、id18:3、id21:3、id28:2，其余 id5/22/31/40/55/56/58/59/60/62/69/70/71/73/74/75 各 1
（个别文件会重复出现同一节点，如 id3 在单个文件中出现 6 次，按文件数计为 1）。

### 1.7 id2 `StudioIr` 的 IR 解密（结论：算法已完全破解）

`id2` 的 `IR File` 指向 `irs\<sha1>.enc` 加密 IR。公开仓库 `audioeffect-qm/qmae/decrypt.py`
只用一段 128 字节 XOR 密钥表无限循环，长文件在 `0x8000` 之后损坏（作者注明 "structure unclear"）。
本期对引擎 DLL 做**纯静态逆向**，恢复了**完整算法**，可完全解密 `.enc`：

- **流格式**：`.enc` 不是 QMAE 容器，而是「裸 WAV 明文 ⊕ 密钥流」的 XOR 流密码，逐字节处理。

- **代码位置**：`QQMusic去exe版/libSuperSound2.dll`（x86，ImageBase `0x10000000`）导出
  `SUPERSOUND2::decrypt_file` @ **VA 0x10052400**。它把输入按 **0x80000 字节（512 KiB）分块**
  读入（`0x10052550 push 0x80000` / `0x10052587 fread`），对每块调用核心例程 **0x1001fb70**，
  再写出结果。核心例程对块内偏移 `i` 执行（反汇编摘录见 `tools/decrypt_ir.py` 头注释）：

  ```
  q        = i            if i <= 0x7fff     ; 0x1001fb8c cmp esi,0x7fff / 0x1001fb92 jle
             i % 0x7fff   otherwise          ; 0x1001fb94..0x1001fbaf（除 0x7fff 的魔法数运算）
  index    = (q*q + 0x13c1b) & 0xFF         ; 0x1001fbb1 imul / 0x1001fbb4 add / 0x1001fbb9 and
  plain[i] = enc[i] XOR KEY256[index]       ; 0x1001fbc7 mov cl,[eax+0x1005f788] / 0x1001fbcd xor
  ```

  `KEY256` 为 DLL **VA 0x1005f788** 处的 256 字节常量（已内嵌进 `tools/decrypt_ir.py`）。

- **为什么公开算法会在 0x8000 后损坏**：因为 `(i+128)² ≡ i² (mod 256)`，在 `i ∈ [0,0x7fff]`
  区间内 `index` 只依赖 `i mod 128`，所以前 ~32 KiB 密钥流恰好是「128 字节表循环」；公开仓库的
  128 字节硬编码表正是 `KEY256[(i²+0x13c1b)&0xFF]`（`i=0..127`）。`i>0x7fff` 后改按 `i mod 0x7fff`
  取 `q`，相位漂移；再叠加 0x80000 分块重置——公开算法缺少这两点，故长文件后段全乱。

- **本地唯一 `.enc`（504-现场律动）验证**（详见 [docs/IR解密分析.md](docs/IR解密分析.md)）：
  输出 `kernels/742156792c2f7c863b9d5cec1cd0622546a1f877.wav`（RIFF/WAVE，4ch / 32-bit float /
  44.1 kHz，2.226 s）：
  1. 前 0x8000 字节与旧「128B 表循环」结果**逐字节一致**；
  2. 392660 个 float **全部有限、`|x| ≤ 1`、无 NaN/Inf**；
  3. `data` 长度字段与实际负载一致（1570640 B），RMS 包络 `0.0109 → 0` 单调衰减。

- `tools/decrypt_ir.py` 已内嵌完整算法，可解密任意 `.enc`。参考仓库 19 个 `.enc` 中，
  全部浮点 IR（001/007/008/009/010/012-2/013/015/017/504/0045）解密后 **0 个非有限值**。

- 本地语料库只含 1 个 `.enc`；其余 **12 个 id2 预设（11 个不同 hash）**引用的 `.enc` 实体不存在
  （客户端按需下载），下载到后可用 `tools/decrypt_ir.py` 同样解密。

> **convolver kernel 安装**：把 `kernels/<sha1>.wav` 放入 V4A app 的 Kernel 目录后，预设内
> `convolver.kernelFile` 即可引用同名文件。当前 IR 为 4 声道，V4A 卷积器按立体声处理（下混）。


---

## 2. 仓库结构

```
ViPER-QQFX-Port/
├── README.md
├── .gitignore
├── manifest.json            # 批量转码汇总清单（每文件置信级/映射组/unmapped/校验结果）
├── aep/                     # 语料：50 个推荐音效 .aep + recommendbase.json + 新版 500
│   ├── 001-差分环绕.aep ... 999-编辑音效.aep
│   ├── recommendbase.json   # 官方元数据（效果描述/标签）
│   └── 500-全景环绕.new.aep # 工作区根目录的新版（1372B，多一个 Gain 节点）
├── docs/                    # 逆向与格式文档
│   ├── 分析报告.md                         # 格式解析 + 映射决策完整报告
│   ├── 加载逻辑分析.md                     # 引擎加载链 / 76 项插件注册表
│   ├── DSP内部处理分析.md                  # 各插件 DSP 内部处理逆向
│   ├── IR解密分析.md                        # .enc IR 完整解密算法 + 验证数据
│   ├── effect_registry.json                # 76 项 id → 类名注册表
│   └── extract_registry.py                 # 注册表提取脚本（历史成果）
├── tools/                   # 工具链
│   ├── aep_parser.py            # 通用 .aep 解析库（parse()）+ smoke test
│   ├── parse_aep.py             # 历史：500 专用解析器（含自检断言，原样保留）
│   ├── v4a_schema.json          # 24 组 V4A schema 快照（组序/字段/类型/range/default）
│   ├── gen_v4a_schema.py        # 由 EffectGroups.kt 生成 v4a_schema.json
│   ├── validate_preset.py       # 基于 schema 的独立预设校验器
│   ├── convert_aep.py           # 单文件转换 CLI（build/convert + 映射规则/置信级）
│   ├── batch_convert.py         # 批量转换 CLI + manifest 汇总
│   └── decrypt_ir.py            # QQ .enc IR 解密器（完整算法，见 1.7）
├── presets/                 # 预设成品（34 个由批量生成 + 1 个手工对照）
│   ├── 001-差分环绕.json ... 998-设备音效.json
│   └── QQ音乐-全景环绕.json   # 手工验证版（对照 build_preset.py，已通过新校验器）
├── parsed/                  # 解析 dump（34 个 <名>.parse.json + 历史 parsed_500-全景环绕.json）
└── kernels/                 # 解密后的卷积 IR WAV（如 504 的 742156...wav，见 1.7）
```

> `500-全景环绕` 的两个版本：`aep/500-全景环绕.aep` 是语料中的**旧版**（1276B，节点 11/12/13，
> 无 Gain）；`aep/500-全景环绕.new.aep` 是工作区根目录的**新版**（1372B，节点 4/11/12/13，
> 多一个 `Gain≈+2.27887 dB` 节点）。手工成品 `presets/QQ音乐-全景环绕.json` 按**新版**整理；
> 批量转码会另外生成 `presets/500-全景环绕.json`（旧版）与 `presets/500-全景环绕.new.json`（新版），
> 三者并存、各按来源标注，不去重。
>
> 完整中间产物（DLL 字符串/反汇编中转文件、临时逆向脚本等）保留在 `workspace/output/逆向/`，
> 未纳入本仓库。

---

## 3. 工具用法

所有脚本均为 Python 3，路径参数化、不写死绝对路径，均支持 `--help`。

### 3.1 解析 `.aep`

```bash
python tools/aep_parser.py aep/500-全景环绕.new.aep          # 单文件摘要
python tools/aep_parser.py --json aep/001-差分环绕.aep        # 输出 JSON
python tools/aep_parser.py --dir aep                          # 全语料 smoke test（节点 id 分布）
```

### 3.2 单文件转换

```bash
python tools/convert_aep.py aep/500-全景环绕.new.aep -o out/全景环绕.json
# 生成 out/全景环绕.json（预设）与 out/全景环绕.json.parse.json（解析 dump + 映射决策/警告）
```

可选 `--schema`（默认 `tools/v4a_schema.json`）、`--registry`（默认 `docs/effect_registry.json`）、
`--kernels-dir`（默认 `kernels/`，id2 convolver 的 IR 查找目录）。

### 3.3 批量转换

```bash
python tools/batch_convert.py
# 扫 aep/*.aep -> presets/<名>.json + parsed/<名>.parse.json -> manifest.json
# 存在可映射节点的文件才落盘；全 skipped（零映射）只记 manifest（reason）
```

可选 `--aep-dir/--presets-dir/--parsed-dir/--manifest/--pattern/--limit/--kernels-dir/--dry-run`。
`manifest.json` 每条含 `confidence`（verified/approx/partial/skipped）、`mapped_groups`、`unmapped`、
`validation` 等字段。

### 3.4 校验预设

```bash
python tools/validate_preset.py --preset presets/QQ音乐-全景环绕.json
# 校验组序/字段/类型/range/默认值一致性（完全基于 v4a_schema.json，不依赖 V4A 源码）
```

### 3.5 解密 `.enc` IR（可选）

```bash
python tools/decrypt_ir.py -i "QQMusic去exe版/resae/irs/<sha1>.enc" -o kernels/<sha1>.wav
# 内嵌完整算法（KEY256 + 0x80000 分块 + (q²+0x13c1b)&0xFF 取模），输出裸 WAV
```
解密后再跑 `tools/batch_convert.py`，对应预设的 `convolver` 会自动启用（见 1.7）。

### 3.6 重新生成 schema 快照（可选）

```bash
python tools/gen_v4a_schema.py \
  --source /path/to/ViPER4Android-main/.../effect/EffectGroups.kt \
  --out tools/v4a_schema.json
```

---

## 4. 预设导入方法（ViPER4Android）

1. 把生成的 `.json` 拷到手机存储任意位置（保持 `.json` 扩展名）；
2. 打开 V4A app → **设置（Settings）** → **Import preset（导入预设）**；
3. 文件选择器以 `application/json` 过滤，选中该 `.json`；单文件导入会立即套用；
4. **注意总开关**：`masterEnable` **不属于**预设内容，导入后需**手动打开 app 顶部的总开关**，
   效果才会实际生效。

---

## 5. 已知局限

| 项 | 说明 |
|---|---|
| Right Time 26.12 ms → 20 ms | app `diffSurround.delay` range 为 1..20 ms，导入时被钳制 |
| `Q` 未映射（id13） | V4A 10 段 EQ 为固定 Q 最小相位 IIR，无 Q 入参 |
| `Center≠100` | V4A `stereoImager` 只缩放 side，mid 增益不可表达，转换时记 warning |
| 滤波器→dynamicEq 为近似 | id51/50/33/34/35/24 用 `dynamicEq`（threshold=−80 恒生效）近似静态滤波器；V4A 无带通类型（id31 跳过），id35 的 Q 由频率边缘推算 |
| id63 MVerb→reverb 为近似 | 用 `roomSize←DECAY / damp←1−DAMPINGFREQ / wet←MIX / dry←1−MIX / width=1.0`；`SIZE/DENSITY/BANDWIDTHFREQ/PREDELAY/EARLYMIX/GAIN` 无对应 |
| id57 SuperBass→bass 为近似 | V4A `bass` 为动态低音/次谐波结构，与 QQ SuperBass 算法不同 |
| id2 StudioIr→convolver | `.enc` 已可完整解密（见 1.7）；IR 为 4 声道，V4A 卷积器按立体声处理（下混）；本地 12 个 id2 预设中仅 504 的 `.enc` 存在 |
| 大量节点不映射 | id9 `Exciter`、id14/15/16/18/19 DFX 系列、id21/59 人声、id22 `HyperBass`、3D/5.1/人声分离/QTSEffect/PitchShifter/Chaos/Rotator/Sampler 等无合理对应，原因见 1.6 与各 `<名>.parse.json` |
| `Gain` 浮点噪声 | `10^(G/20)` 由 float32 `Gain` 计算，如 `1.2999999583` 而非精确 `1.3`（差 ~4e-8，可忽略） |
| 空内置名回退 | 050-061 房间系列 `.aep` 内置效果名为空，预设名回退为文件名主干 |

---

## 6. 版权声明

- `.aep` 音效文件版权归**腾讯（QQ 音乐）**所有，本仓库仅用于**个人学习与互操作性研究**，
  请勿商用或再分发；如需使用请自行获取授权。
- ViPER4Android 预设格式参考 `com.llsl.viper4android` 与 `ViPERFX_RE`（ViPERDSP）项目，
  相关格式与 DSP 语义解释仅用于互操作。
- 本仓库不包含 `ViPER4Android-main` / `ViPERFX_RE-dev` 源码，DLL 逆向结论为原创静态分析结果。
