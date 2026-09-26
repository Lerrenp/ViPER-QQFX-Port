#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert_aep.py

单个 QQ 音乐 ".aep" 音效 -> ViPER4Android 预设 的转换器。

预设骨架 = 24 组完整默认值（由 v4a_schema.json 生成），name 取 .aep 内效果名。

映射规则（每条都标注置信级；规则依据见 docs/分析报告.md、docs/DSP内部处理分析.md
以及 ViPERDSP 源码 / MVerb 原作源码，详见 README「映射规则表」）：

  verified（有 DLL/DSP 源码双重证据，语义可精确对应）:
    id4  Amplifier        Gain(dB)         -> masterLimiter.outputVolume = 10^(G/20)，clamp 0.01..2.0
    id13 IirEQ10          10 段 peaking(dB) -> equalizer{enable,bandCount:10,bands:按频率升序}，Q 忽略
    id11 StereoEnhancer   Width(%)          -> stereoImager 三频段 width=W/100 clamp 0..2
    id12 Delay            单边 Time>0 且 Feedback=0 -> diffSurround{delay:clamp(t,1,20),reverse:Left>0,wetDryMix:1.0}

  approx（语义清晰但跨算法结构，标注近似）:
    id63 Mverb            -> reverb{roomSize:DECAY,damp:1-DAMPINGFREQ,wet:MIX,dry:1-MIX}
    id2  StudioIr         -> convolver{enable,kernelFile}（仅当 kernels/ 下存在对应解密 WAV）
    id51/33/34/35/50/24   参数/搁架滤波器 -> dynamicEq（bandCount<=10，threshold 恒生效）
    id57 SuperBass        -> bass{frequency,gain}

  skipped（无合理对应，记原因）:
    3D/5.1/人声/人声分离/PitchShifter/QTSEffect/Chaos/Rotator/Sampler/Exciter/
    DFX 系列(14/15/16/18/19)/HyperBass(22) 等

输出：<out.json>（预设）+ <out.json>.parse.json（解析 dump + 映射决策/警告）。
零映射文件（无任何可映射节点）由调用方决定是否落盘（batch_convert 只记 manifest）。
"""
import argparse
import copy
import json
import math
import os
import sys

import aep_parser

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 通过 schema/registry 默认定位到仓库内文件（可 --schema/--registry 覆盖）
DEF_SCHEMA = os.path.join(HERE, "v4a_schema.json")
DEF_REGISTRY = os.path.join(REPO, "docs", "effect_registry.json")
DEF_KERNELS = os.path.join(REPO, "kernels")

# 映射规则涉及的节点 id
ID_STUDIO_IR = 2
ID_AMPLIFIER = 4
ID_STEREO_ENHANCER = 11
ID_DELAY = 12
ID_IIR_EQ10 = 13
ID_SUPERBASS = 57
ID_MVERB = 63
# 滤波器家族：24=SuperEQ(FFT 图形 EQ)、33=LSFilter、34=HSFilter、35=PKFilter、50=HighShelfQ、51=PeakingQ
ID_EQ_FILTERS = frozenset((24, 33, 34, 35, 50, 51))

# 置信级标签
CONF_VERIFIED = "verified"
CONF_APPROX = "approx"
CONF_PARTIAL = "partial"
CONF_SKIPPED = "skipped"

# dynamicEq 恒生效阈值：ViPERDSP DynamicEQ.cpp 中 overshoot = envelope_dB - threshold，
# 仅当 overshoot > 0 才按比例施加目标增益；threshold = -80（schema 下限）时对任何 > -80 dBFS
# 的信号恒生效，从而近似一个静态 EQ 频段。
DEQ_THRESHOLD_ALWAYS = -80.0
DEQ_ATTACK_MS = 10.0
DEQ_RELEASE_MS = 100.0
DEQ_MAX_BANDS = 10  # DynamicEQ.h: kMaxBands = 10

# 滤波器类型枚举（ViPERDSP DynamicEQ.cpp SetBandFilterType）
FT_PEAK = 0
FT_LOW_SHELF = 1
FT_HIGH_SHELF = 2

# skipped 节点的具体原因（无合理对应语义时记入 dump/manifest，避免"未实现"式空话）
SKIP_REASONS = {
    3: "Chaos 多重混音/移位（Input/Output Multiplier、Shift、Wrap、Mixing Type），无对应 V4A 语义",
    5: "Rotator 旋转/变速（Speed、Offset），无对应",
    7: "Sampler 采样器（Audio File 回放 + Gain），V4A 无采样/音效叠加节点",
    9: "Exciter 激励器（MixBack/Frequency/ClipBoost/Harmonics）；V4A spectrumExtension 仅 2.2k+ 高频谐波激励且频段不匹配，无合理对应",
    14: "DFX Fidelity 保真度（单一 0..100 量），V4A 无对应单旋钮保真模块",
    15: "DFX HyperBass（单一量），V4A bass/psychoacousticBass 均需 cutoff+gain 双参数，结构不匹配",
    16: "DFX 3D Surround 3D 环绕，无对应",
    18: "DFX Ambience 环境声（单一 0..100 量），无对应",
    19: "DFX Dynamic Boost 动态增强（单一 0..100 量），无对应",
    21: "Vocal 人声增强，无对应（归入 skipped 人声类）",
    22: "HyperBass（Selectivity/Gain/Ratio），算法结构无对应",
    28: "PitchShifter 变调（Key），无对应",
    31: "BPFilter 带通（LowEdge/HighEdge，无增益），V4A dynamicEq 无带通类型",
    40: "TwotoSix 2→6 声道扩展（Enabled），无对应",
    55: "MultiFuncSampler 多功能采样器，无对应",
    56: "SleepEffect 睡眠音效（FreqBase/FreqDiff/DelayTime），无对应",
    58: "HandDraw3D 手绘 3D 声场（HRTF），无对应",
    59: "VocalEffectNew 人声效果，无对应（归入 skipped 人声类）",
    60: "Panoramic51IRBased 5.1 IR 全景（HRTFDataFile），无对应",
    62: "WideSoundField HRTF 宽声场（HRIRDataFile），无对应",
    69: "6→2 虚拟环绕（扬声器权重），无对应",
    70: "MusicSeparation 人声/伴奏分离，无对应",
    71: "2→6 人声分离扩展，无对应",
    73: "QTSEffect Near 近场 FIR（.irs），无对应",
    74: "QTSEffect Wide 宽场 FIR（.irs），无对应",
    75: "QTSEffect Front 前场 FIR（.irs），无对应",
}


def load_registry(path):
    """加载 effect_registry.json，返回 {id: 短类名}；缺失时返回 {}。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for e in data.get("effects", []):
        cls = e.get("class")
        if not cls:
            continue
        if cls.startswith(".?AV"):
            cls = cls[4:]
        out[e["id"]] = cls.split("@")[0]
    return out


def build_skeleton(schema, name, now_ms=None):
    """由 schema 生成 24 组完整默认值的预设骨架。"""
    import time
    preset = {
        "schemaVersion": schema.get("schemaVersion", 2.1),
        "name": name,
        "createdAt": int((now_ms if now_ms is not None else time.time() * 1000)),
    }
    for g in schema["groups"]:
        preset[g["effectKey"]] = {f["jsonKey"]: copy.deepcopy(f["default"]) for f in g["fields"]}
    return preset


def _clamp(v, lo, hi, label, warnings):
    if v < lo:
        warnings.append("%s = %g 低于下限 %g，已钳到 %g" % (label, v, lo, lo))
        return lo
    if v > hi:
        warnings.append("%s = %g 超过上限 %g，已钳到 %g" % (label, v, hi, hi))
        return hi
    return v


def _freq_of(name):
    """'16000 Hz' -> 16000.0；无法解析返回 None。"""
    if not name.endswith(" Hz"):
        return None
    try:
        return float(name[:-3].strip())
    except ValueError:
        return None


def _ir_kernel_name(ir_file, kernels_dir):
    """'irs\\7421....enc' -> kernels/<stem>.wav 若存在，返回 wav 文件名；否则 None。"""
    if not ir_file:
        return None
    base = os.path.basename(str(ir_file).replace("\\", "/"))
    stem = os.path.splitext(base)[0]
    kernel = stem + ".wav"
    if kernels_dir and os.path.exists(os.path.join(kernels_dir, kernel)):
        return kernel
    return None


def _add_deq_band(bands, freq, q, gain, ftype, src, warnings):
    """向 dynamicEq 频段表追加一个频段（freq/q/gain 按 V4A range 钳制）。"""
    f = _clamp(float(freq), 20.0, 20000.0, "%s freq" % src, warnings)
    qq = _clamp(float(q), 0.5, 8.0, "%s Q" % src, warnings)
    g = _clamp(float(gain), -12.0, 12.0, "%s gain" % src, warnings)
    bands.append({"freq": int(round(f)), "q": qq, "gain": g, "ftype": int(ftype), "src": src})


def _overall_confidence(decisions, unmapped, group_conf):
    """由「覆盖率 + 规则置信级」归一出单一 confidence：verified/approx/partial/skipped。"""
    if not decisions:
        return CONF_SKIPPED
    if unmapped:
        return CONF_PARTIAL
    if any(group_conf.get(g, CONF_APPROX) != CONF_VERIFIED for g in decisions):
        return CONF_APPROX
    return CONF_VERIFIED


def build(aep_path, schema, registry, now_ms=None, dump_path=None, kernels_dir=DEF_KERNELS):
    """解析并构造预设/解析 dump（不落盘）；返回结果 dict（含 preset、dump 对象）。"""
    parsed = aep_parser.parse(aep_path)
    name = parsed["name"]
    # 部分语料（尤其 050-061 房间系列）内置效果名为空，回退为文件名主干，避免预设名空白
    if not str(name).strip():
        name = os.path.splitext(os.path.basename(aep_path))[0]
    nodes = parsed["nodes"]
    preset = build_skeleton(schema, name, now_ms=now_ms)

    warnings = []
    unmapped = []
    decisions = {}          # group -> [决策说明]
    group_conf = {}         # group -> verified/approx
    gain_db_total = 0.0
    gain_seen = False
    deq_bands = []

    def note(group, conf, msg):
        decisions.setdefault(group, []).append(msg)
        # 同一组取最低置信级
        if group_conf.get(group) != CONF_APPROX:
            group_conf[group] = conf

    for node in nodes:
        nid = node["id"]
        params = node["params"]
        cls = registry.get(nid, "id%d" % nid)

        if nid == ID_AMPLIFIER:
            if "Gain" in params:
                g = float(params["Gain"])
                gain_db_total += g
                gain_seen = True
                note("masterLimiter", CONF_VERIFIED, "id4 Amplifier: Gain=%+.4f dB" % g)
            else:
                warnings.append("id4 Amplifier 缺 'Gain' 参数，忽略该节点")

        elif nid == ID_IIR_EQ10:
            bands = []
            for pname, pval in params.items():
                f = _freq_of(pname)
                if f is not None:
                    bands.append((f, float(pval)))
            bands.sort(key=lambda x: x[0])
            if bands:
                vals = []
                for f, v in bands:
                    vals.append(_clamp(v, -12.0, 12.0, "equalizer band %g Hz" % f, warnings))
                preset["equalizer"]["enable"] = True
                preset["equalizer"]["bandCount"] = len(vals)
                preset["equalizer"]["bands"] = vals
                if len(vals) != 10:
                    warnings.append("id13 IirEQ10: 频段数 %d != 10" % len(vals))
                if "Q" in params:
                    decisions.setdefault("equalizer", []).append(
                        "id13 IirEQ10: Q=%.6f 未映射（V4A 10 段 EQ 为固定 Q 最小相位 IIR）"
                        % float(params["Q"]))
                note("equalizer", CONF_VERIFIED,
                     "id13 IirEQ10: %d 段增益按频率升序映射" % len(vals))
            else:
                warnings.append("id13 IirEQ10 未找到 'NNNN Hz' 频段参数，忽略")

        elif nid == ID_STEREO_ENHANCER:
            if "Width" in params:
                w = float(params["Width"]) / 100.0
                w = _clamp(w, 0.0, 2.0, "stereoImager width", warnings)
                if "stereoImager" in decisions:
                    warnings.append("id11 StereoEnhancer 出现多次（串联），V4A stereoImager 仅单级，后者覆盖前者")
                for k in ("lowWidth", "midWidth", "highWidth"):
                    preset["stereoImager"][k] = w
                preset["stereoImager"]["enable"] = True
                note("stereoImager", CONF_VERIFIED,
                     "id11 StereoEnhancer: Width=%g%% -> width=%.4f" % (params["Width"], w))
                if "Center" in params and abs(float(params["Center"]) - 100.0) > 1e-6:
                    warnings.append("id11 StereoEnhancer: Center=%g != 100，mid 增益在 V4A stereoImager "
                                    "中不可表达（仅缩放 side）" % params["Center"])
            else:
                warnings.append("id11 StereoEnhancer 缺 'Width' 参数，忽略")

        elif nid == ID_DELAY:
            lt = float(params.get("Left Time", 0.0))
            rt = float(params.get("Right Time", 0.0))
            lf = float(params.get("Left Feedback", 0.0))
            rf = float(params.get("Right Feedback", 0.0))
            single = (lt > 0.0) != (rt > 0.0)
            no_fb = (lf == 0.0 and rf == 0.0)
            if single and no_fb:
                t = lt if lt > 0.0 else rt
                t = _clamp(t, 1.0, 20.0, "diffSurround delay(ms)", warnings)
                preset["diffSurround"].update({
                    "enable": True,
                    "delay": t,
                    "reverse": bool(lt > 0.0),
                    "wetDryMix": 1.0,
                    "lpCutoff": 0,
                })
                note("diffSurround", CONF_VERIFIED,
                     "id12 Delay: 单边延迟 %gms(左=%g,右=%g), Feedback=0 -> delay=%g, reverse=%s"
                     % (t, lt, rt, t, lt > 0.0))
            else:
                if not single:
                    reason = "双路均 %s（非单边延迟）" % ("为 0" if (lt == 0.0 and rt == 0.0) else ">0")
                else:
                    reason = "Feedback 非 0 (L=%g,R=%g)" % (lf, rf)
                unmapped.append({"id": nid, "class": cls, "params": params, "reason":
                                 "id12 Delay 不满足单边延迟+零反馈条件：%s" % reason})

        elif nid == ID_MVERB:
            # MVerb -> Freeverb(V4A reverb) 近似。
            # 依据 MVerb.h：DampingFreq*18400+100 作为反馈支路低通截止频率（越高越亮→阻尼越少），
            # 故 V4A damp = 1 - DAMPINGFREQ；Decay 为反馈增益(=衰减时间)，对应 V4A roomSize；
            # MIX 为干湿交叉淡入，故 wet=MIX、dry=1-MIX。
            if "reverb" in decisions:
                warnings.append("id63 Mverb 出现多次，V4A reverb 仅单级，后者覆盖前者")
            size = float(params.get("SIZE", 0.0))
            dampf = float(params.get("DAMPINGFREQ", 0.0))
            mix = float(params.get("MIX", 0.0))
            decay = float(params.get("DECAY", 0.0))
            room = _clamp(decay, 0.0, 1.0, "reverb.roomSize(<-DECAY)", warnings)
            damp = _clamp(1.0 - dampf, 0.0, 1.0, "reverb.damp(<-1-DAMPINGFREQ)", warnings)
            wet = _clamp(mix, 0.0, 1.0, "reverb.wet(<-MIX)", warnings)
            dry = _clamp(1.0 - mix, 0.0, 1.0, "reverb.dry(<-1-MIX)", warnings)
            preset["reverb"].update({
                "enable": True,
                "roomSize": room,
                # MVerb 湿声为立体声（左右 tank 延迟线不同）；V4A Freeverb width=0 会让湿声
                # 左右交叉退化为单声道，width=1 才是不交叉立体声，与 MVerb 行为一致。
                "width": 1.0,
                "damp": damp,
                "wet": wet,
                "dry": dry,
            })
            note("reverb", CONF_APPROX,
                 "id63 Mverb -> reverb: roomSize=DECAY=%g, damp=1-DAMPINGFREQ=%g, wet=MIX=%g, dry=1-MIX=%g, width=1.0(立体声湿声)"
                 % (room, damp, wet, dry))
            no_map = [k for k in ("SIZE", "DENSITY", "BANDWIDTHFREQ", "PREDELAY", "EARLYMIX", "GAIN")
                      if k in params]
            if no_map:
                warnings.append("id63 Mverb: %s 在 V4A reverb 中无可对应项，未映射（SIZE 缩放全部延迟线/扩散几何，"
                                "V4A reverb 无该维度）" % ",".join(no_map))

        elif nid in ID_EQ_FILTERS:
            if nid == 24:
                # SuperEQ(FFT 图形 EQ)：频段名为 'NNN Hz'，gain_len/start_f/octave/window_bits 为结构参数
                for pname, pval in params.items():
                    f = _freq_of(pname)
                    if f is not None:
                        _add_deq_band(deq_bands, f, 1.0, float(pval), FT_PEAK, "id24 SuperEQ", warnings)
                note("dynamicEq", CONF_APPROX,
                     "id24 SuperEQ: FFT 图形 EQ -> dynamicEq 峰值频段（Q 取 1.0 近似，start_f/octave/window_bits 未映射）")
            elif nid in (33,):
                _add_deq_band(deq_bands, float(params.get("Frequency", 0.0)),
                              float(params.get("Q", 1.0)), float(params.get("Gain", 0.0)),
                              FT_LOW_SHELF, "id33 LSFilter", warnings)
                note("dynamicEq", CONF_APPROX, "id33 LSFilter -> dynamicEq low shelf")
            elif nid == 34:
                _add_deq_band(deq_bands, float(params.get("Frequency", 0.0)),
                              float(params.get("Q", 1.0)), float(params.get("Gain", 0.0)),
                              FT_HIGH_SHELF, "id34 HSFilter", warnings)
                note("dynamicEq", CONF_APPROX, "id34 HSFilter -> dynamicEq high shelf")
            elif nid == 35:
                lo = float(params.get("LowEdge", 0.0))
                hi = float(params.get("HighEdge", 0.0))
                f0 = math.sqrt(max(lo, 1.0) * max(hi, 1.0))
                bw = max(hi - lo, 1.0)
                q = _clamp(f0 / bw, 0.5, 8.0, "id35 PKFilter Q(由边缘推算)", warnings)
                _add_deq_band(deq_bands, f0, q, float(params.get("Gain", 0.0)), FT_PEAK, "id35 PKFilter", warnings)
                note("dynamicEq", CONF_APPROX,
                     "id35 PKFilter(LowEdge/HighEdge) -> dynamicEq 峰值，f0=sqrt(lo*hi)=%g, Q=f0/(hi-lo)=%.3f" % (f0, q))
            elif nid == 50:
                _add_deq_band(deq_bands, float(params.get("Frequency_cut", 0.0)),
                              float(params.get("Q", 1.0)), float(params.get("dBgain", 0.0)),
                              FT_HIGH_SHELF, "id50 HighShelfQ", warnings)
                note("dynamicEq", CONF_APPROX, "id50 HighShelfFilterQ -> dynamicEq high shelf")
            elif nid == 51:
                _add_deq_band(deq_bands, float(params.get("Frequency_cut", 0.0)),
                              float(params.get("Q", 1.0)), float(params.get("dBgain", 0.0)),
                              FT_PEAK, "id51 PeakingFilterQ", warnings)
                note("dynamicEq", CONF_APPROX, "id51 PeakingFilterQ -> dynamicEq 峰值")
            if nid in (33, 34, 35, 50, 51) and "NN_Num" in params:
                warnings.append("id%d %s: NN_Num=%g/Channel=%s 在 V4A dynamicEq 中无对应项，未映射"
                                % (nid, cls, float(params.get("NN_Num", 0.0)), params.get("Channel")))

        elif nid == ID_SUPERBASS:
            freq = _clamp(float(params.get("Frequency", 60.0)), 15.0, 150.0, "bass.frequency", warnings)
            gain = _clamp(float(params.get("Gain", 0.5)), 0.5, 10.0, "bass.gain", warnings)
            preset["bass"].update({
                "enable": True,
                "mode": preset["bass"]["mode"],
                "frequency": int(round(freq)),
                "gain": gain,
                "antiPop": False,
            })
            note("bass", CONF_APPROX,
                 "id57 SuperBass -> bass: frequency=%g, gain=%g（V4A bass 为动态低音/次谐波结构，非同一算法）"
                 % (freq, gain))

        elif nid == ID_STUDIO_IR:
            ir_file = params.get("IR File", "")
            kernel = _ir_kernel_name(ir_file, kernels_dir)
            if kernel:
                # V4A convolver 单级：同文件多个 id2 时后者覆盖前者（如 012 的两个 IR）
                if "convolver" in decisions:
                    warnings.append("id2 StudioIr 出现多次，V4A convolver 仅单级，后者覆盖前者"
                                    "（前一个 kernel=%s 被弃用，未做串联/混合）" % preset["convolver"]["kernelFile"])
                preset["convolver"].update({
                    "enable": True,
                    "kernelFile": kernel,
                    "crossChannel": 0.0,
                })
                note("convolver", CONF_APPROX,
                     "id2 StudioIr -> convolver: kernelFile=%s（kernel 由 tools/build_kernels.py 按 SS2 加载器语义构建："
                     "解密+Trim/Fade（本语料均不激活）+4ch 对角降混+44.1kHz 归一，见 docs/SS2引擎分析.md）" % kernel)
                for k in ("Trim", "Fade"):
                    if k in params:
                        warnings.append("id2 StudioIr: %s=%g 未映射（convolver 无对应项）" % (k, float(params[k])))
            else:
                unmapped.append({"id": nid, "class": cls, "params": params, "reason":
                                 "id2 StudioIr 需要 .enc 解密后的 IR：kernels/%s 不存在或未解密，convolver 无法启用"
                                 % os.path.basename(str(ir_file).replace("\\", "/") or "?")})

        else:
            unmapped.append({"id": nid, "class": cls, "params": params,
                             "reason": SKIP_REASONS.get(nid, "无对应 V4A 语义/未实现映射")})

    # 汇总 Amplifier 增益 -> outputVolume
    if gain_seen:
        lin = 10.0 ** (gain_db_total / 20.0)
        lin = _clamp(lin, 0.01, 2.0, "masterLimiter.outputVolume", warnings)
        preset["masterLimiter"]["outputVolume"] = lin

    # 汇总滤波器频段 -> dynamicEq
    if deq_bands:
        if len(deq_bands) > DEQ_MAX_BANDS:
            warnings.append("dynamicEq 频段数 %d 超过上限 %d，已截断" % (len(deq_bands), DEQ_MAX_BANDS))
            deq_bands = deq_bands[:DEQ_MAX_BANDS]
        n = len(deq_bands)
        fts = [b["ftype"] for b in deq_bands]
        preset["dynamicEq"].update({
            "enable": True,
            "bandCount": n,
            "freqs": [b["freq"] for b in deq_bands],
            "qs": [b["q"] for b in deq_bands],
            "gains": [b["gain"] for b in deq_bands],
            "thresholds": [DEQ_THRESHOLD_ALWAYS] * n,
            "attacks": [DEQ_ATTACK_MS] * n,
            "releases": [DEQ_RELEASE_MS] * n,
            "filterTypes": fts,
        })
        note("dynamicEq", CONF_APPROX,
             "dynamicEq: %d 频段（threshold=%g 恒生效，type 0=peak/1=low shelf/2=high shelf）"
             % (n, DEQ_THRESHOLD_ALWAYS))

    for u in unmapped:
        warnings.append("未映射节点 id=%d(%s) 参数=%s：%s"
                        % (u["id"], u["class"], json.dumps(u["params"], ensure_ascii=False), u["reason"]))

    confidence = _overall_confidence(decisions, unmapped, group_conf)

    # 解析 dump + 映射决策
    dump = {
        "source": parsed["path"],
        "file_size": parsed.get("file_size"),
        "magic": parsed["magic"],
        "name": name,
        "nodes": [
            {"id": n["id"], "class": registry.get(n["id"], "id%d" % n["id"]), "params": n["params"]}
            for n in nodes
        ],
        "mapping": {
            "confidence": confidence,
            "groups": sorted(decisions.keys()),
            "group_confidence": {g: group_conf.get(g, CONF_APPROX) for g in sorted(decisions)},
            "decisions": decisions,
            "unmapped": unmapped,
        },
        "warnings": warnings,
    }

    return {
        "input": parsed["path"],
        "name": name,
        "node_ids": [n["id"] for n in nodes],
        "mapped_groups": sorted(decisions.keys()),
        "group_confidence": {g: group_conf.get(g, CONF_APPROX) for g in sorted(decisions)},
        "approx_groups": sorted(g for g in decisions if group_conf.get(g) != CONF_VERIFIED),
        "unmapped": [{"id": u["id"], "class": u["class"], "reason": u["reason"]} for u in unmapped],
        "warnings": warnings,
        "confidence": confidence,
        "has_mapping": bool(decisions),
        "preset": preset,
        "dump": dump,
        "suggested_dump_path": dump_path,
    }


def convert(aep_path, out_path, schema, registry, now_ms=None, dump_path=None, kernels_dir=DEF_KERNELS):
    """把单个 aep 转成预设；写 out_path 与解析 dump（默认 out_path+'.parse.json'）；返回结果 dict。"""
    res = build(aep_path, schema, registry, now_ms=now_ms, dump_path=dump_path, kernels_dir=kernels_dir)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res["preset"], f, ensure_ascii=False, indent=2)
        f.write("\n")

    dump_path = dump_path or (out_path + ".parse.json")
    res["dump"]["suggested_dump_path"] = dump_path
    os.makedirs(os.path.dirname(os.path.abspath(dump_path)), exist_ok=True)
    with open(dump_path, "w", encoding="utf-8") as f:
        json.dump(res["dump"], f, ensure_ascii=False, indent=2)
        f.write("\n")

    res["output_preset"] = os.path.abspath(out_path)
    res["output_parse"] = os.path.abspath(dump_path)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="QQ 音乐 .aep -> ViPER4Android 预设（单文件）")
    ap.add_argument("input", help="输入 .aep 文件")
    ap.add_argument("-o", "--output", required=True, help="输出预设 .json 路径")
    ap.add_argument("--schema", default=DEF_SCHEMA, help="schema 快照 (默认: tools/v4a_schema.json)")
    ap.add_argument("--registry", default=DEF_REGISTRY, help="插件注册表 (默认: docs/effect_registry.json)")
    ap.add_argument("--kernels-dir", default=DEF_KERNELS, help="解密 IR kernel 目录 (默认: kernels)")
    args = ap.parse_args(argv)

    schema = json.load(open(args.schema, encoding="utf-8"))
    registry = load_registry(args.registry)
    res = convert(args.input, args.output, schema, registry, kernels_dir=args.kernels_dir)

    print("[i] %s" % res["input"])
    print("[i] name = %s, nodes = %s" % (res["name"], res["node_ids"]))
    print("[i] confidence = %s, mapped groups = %s" % (res["confidence"], res["mapped_groups"]))
    print("[i] wrote preset : %s" % res["output_preset"])
    print("[i] wrote dump   : %s" % res["output_parse"])
    for w in res["warnings"]:
        print("    [!] " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
