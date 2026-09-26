#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert_aep.py

单个 QQ 音乐 ".aep" 音效 -> ViPER4Android 预设 的转换器。

预设骨架 = 24 组完整默认值（由 v4a_schema.json 生成），name 取 .aep 内效果名。
内置映射规则（依据见 docs/分析报告.md、docs/加载逻辑分析.md、docs/DSP内部处理分析.md）：
  id4  Amplifier        Gain(dB)         -> masterLimiter.outputVolume = 10^(G/20)，clamp 0.01..2.0
  id13 IirEQ10          10 段 peaking(dB) -> equalizer{enable,bandCount:10,bands:按频率升序}，Q 忽略
  id11 StereoEnhancer   Width(%)          -> stereoImager 三频段 width=W/100 clamp 0..2
  id12 Delay            单边 Time>0 且 Feedback=0 -> diffSurround{delay:clamp(t,1,20),reverse:Left>0,wetDryMix:1.0}
  其余节点（id63 Mverb、id8 IirEQ30、id2 StudioIr、id9 Exciter、低音/3D/5.1 类等）一律不映射，
  将节点 id/类名/参数 dump 记入 warning。
输出：<out.json>（预设）+ <out.json>.parse.json（解析 dump + 映射决策/警告）。
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

# 映射规则涉及的节点 id
ID_AMPLIFIER = 4
ID_STEREO_ENHANCER = 11
ID_DELAY = 12
ID_IIR_EQ10 = 13


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


def convert(aep_path, out_path, schema, registry, now_ms=None, dump_path=None):
    """把单个 aep 转成预设；写 out_path 与解析 dump（默认 out_path+'.parse.json'）；返回结果 dict。"""
    parsed = aep_parser.parse(aep_path)
    name = parsed["name"]
    nodes = parsed["nodes"]
    preset = build_skeleton(schema, name, now_ms=now_ms)

    warnings = []
    unmapped = []
    decisions = {}      # group -> 决策说明
    gain_db_total = 0.0
    gain_seen = False

    for node in nodes:
        nid = node["id"]
        params = node["params"]
        cls = registry.get(nid, "id%d" % nid)

        if nid == ID_AMPLIFIER:
            if "Gain" in params:
                g = float(params["Gain"])
                gain_db_total += g
                gain_seen = True
                decisions.setdefault("masterLimiter", []).append(
                    "id4 Amplifier: Gain=%+.4f dB" % g)
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
                decisions.setdefault("equalizer", []).append(
                    "id13 IirEQ10: %d 段增益按频率升序映射" % len(vals))
            else:
                warnings.append("id13 IirEQ10 未找到 'NNNN Hz' 频段参数，忽略")

        elif nid == ID_STEREO_ENHANCER:
            if "Width" in params:
                w = float(params["Width"]) / 100.0
                w = _clamp(w, 0.0, 2.0, "stereoImager width", warnings)
                for k in ("lowWidth", "midWidth", "highWidth"):
                    preset["stereoImager"][k] = w
                preset["stereoImager"]["enable"] = True
                decisions.setdefault("stereoImager", []).append(
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
                decisions.setdefault("diffSurround", []).append(
                    "id12 Delay: 单边延迟 %gms(左=%g,右=%g), Feedback=0 -> delay=%g, reverse=%s"
                    % (t, lt, rt, t, lt > 0.0))
            else:
                if not single:
                    reason = "双路均 %s（非单边延迟）" % ("为 0" if (lt == 0.0 and rt == 0.0) else ">0")
                else:
                    reason = "Feedback 非 0 (L=%g,R=%g)" % (lf, rf)
                unmapped.append({"id": nid, "class": cls, "params": params, "reason":
                                 "id12 Delay 不满足单边延迟+零反馈条件：%s" % reason})

        else:
            unmapped.append({"id": nid, "class": cls, "params": params, "reason": "无对应 V4A 语义/未实现映射"})

    # 汇总 Amplifier 增益 -> outputVolume
    if gain_seen:
        lin = 10.0 ** (gain_db_total / 20.0)
        lin = _clamp(lin, 0.01, 2.0, "masterLimiter.outputVolume", warnings)
        preset["masterLimiter"]["outputVolume"] = lin

    for u in unmapped:
        warnings.append("未映射节点 id=%d(%s) 参数=%s：%s"
                        % (u["id"], u["class"], json.dumps(u["params"], ensure_ascii=False), u["reason"]))

    # 写预设
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(preset, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # 写解析 dump + 决策
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
            "groups": sorted(decisions.keys()),
            "decisions": decisions,
            "unmapped": unmapped,
        },
        "warnings": warnings,
    }
    dump_path = dump_path or (out_path + ".parse.json")
    os.makedirs(os.path.dirname(os.path.abspath(dump_path)), exist_ok=True)
    with open(dump_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return {
        "input": parsed["path"],
        "output_preset": os.path.abspath(out_path),
        "output_parse": os.path.abspath(dump_path),
        "name": name,
        "node_ids": [n["id"] for n in nodes],
        "mapped_groups": sorted(decisions.keys()),
        "unmapped": [{"id": u["id"], "class": u["class"], "reason": u["reason"]} for u in unmapped],
        "warnings": warnings,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="QQ 音乐 .aep -> ViPER4Android 预设（单文件）")
    ap.add_argument("input", help="输入 .aep 文件")
    ap.add_argument("-o", "--output", required=True, help="输出预设 .json 路径")
    ap.add_argument("--schema", default=DEF_SCHEMA, help="schema 快照 (默认: tools/v4a_schema.json)")
    ap.add_argument("--registry", default=DEF_REGISTRY, help="插件注册表 (默认: docs/effect_registry.json)")
    args = ap.parse_args(argv)

    schema = json.load(open(args.schema, encoding="utf-8"))
    registry = load_registry(args.registry)
    res = convert(args.input, args.output, schema, registry)

    print("[i] %s" % res["input"])
    print("[i] name = %s, nodes = %s" % (res["name"], res["node_ids"]))
    print("[i] mapped groups = %s" % res["mapped_groups"])
    print("[i] wrote preset : %s" % res["output_preset"])
    print("[i] wrote dump   : %s" % res["output_parse"])
    for w in res["warnings"]:
        print("    [!] " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
