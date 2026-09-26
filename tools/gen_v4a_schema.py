#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_v4a_schema.py

从 ViPER4Android 源码 EffectGroups.kt（+ CompressorUnits.kt 的量纲换算函数）
提取 24 组 effect schema 快照，写出 v4a_schema.json。

快照内容：组顺序、每组字段的 jsonKey / 类型 / range / 默认值
（range 与 default 均为求值后的数值，便于校验器在无源码环境下独立工作）。

用法:
  python gen_v4a_schema.py --source <EffectGroups.kt 路径> --out v4a_schema.json
"""
import argparse
import json
import math
import os
import re
import sys

KINDS = ("int", "float", "bool", "string", "nullableLong",
         "intList", "floatList", "boolList", "doubleList")
CALL_RE = re.compile(r"\b(" + "|".join(KINDS) + r")\(")


def split_top(s):
    """按顶层逗号切分，忽略 (), [], {} 与字符串内逗号。"""
    out, buf, depth, quote = [], [], 0, None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


def balanced_call(text, open_idx):
    """text[open_idx]=='('；返回 (args_string, end_index_exclusive)。"""
    depth, i, quote = 0, open_idx, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i + 1
        i += 1
    raise ValueError("unbalanced parens")


def ns():
    """CompressorUnits.kt 的量纲换算函数。"""
    lg = math.log(10.0) / 20.0
    return {
        "compressorDbToRaw": lambda db: db * lg,
        "compressorRatioToRaw": lambda r: -r,
        "compressorMsToSeconds": lambda ms: ms / 1000.0,
        "compressorAdaptAmountToSeconds": lambda a: 4.0 ** a,
    }


def eval_range(expr, namespace):
    e = re.sub(r"(\d)f\b", r"\1", expr).strip()
    parts = e.split("..")
    if len(parts) != 2:
        return None
    lo = eval(parts[0], {"__builtins__": {}}, namespace)
    hi = eval(parts[1], {"__builtins__": {}}, namespace)
    return (float(lo), float(hi))


def eval_default(expr, namespace):
    if expr is None:
        return None
    e = expr.strip()
    if e == "true":
        return True
    if e == "false":
        return False
    if e == "null":
        return None
    if e.startswith('"') and e.endswith('"'):
        return e[1:-1]
    m = re.match(r"^List\((\d+)\)\s*\{(.*)\}$", e, re.S)
    if m:
        return [eval_default(m.group(2), namespace)] * int(m.group(1))
    m = re.match(r"^listOf\((.*)\)$", e, re.S)
    if m:
        inner = m.group(1).strip()
        if not inner:
            return []
        return [eval_default(x, namespace) for x in split_top(inner)]
    return eval(re.sub(r"(\d)f\b", r"\1", e), {"__builtins__": {}}, namespace)


def parse_effect_groups_kt(text):
    """返回 (groups:[(effectKey,[(jsonKey,kind,range,default)])], order:[effectKey])。"""
    namespace = ns()
    cls_re = re.compile(r'class\s+(\w+)\s*:\s*EffectGroupBuilder\(\s*"([^"]+)"\s*\)')
    matches = list(cls_re.finditer(text))
    groups = []
    for idx, m in enumerate(matches):
        key = m.group(2)
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else text.find("object Effects")
        if end < 0:
            end = len(text)
        block = text[start:end]
        prefs = []
        for cm in CALL_RE.finditer(block):
            kind = cm.group(1)
            args_str, _ = balanced_call(block, cm.end() - 1)
            args = split_top(args_str)
            if kind == "nullableLong":
                json_key = args[0].strip().strip('"')
                default_expr = None
            else:
                json_key = args[1].strip().strip('"')
                default_expr = args[2]
            rng = None
            for a in args:
                if a.startswith("range"):
                    rng = a.split("=", 1)[1].strip()
            prefs.append((json_key, kind,
                          eval_range(rng, namespace) if rng else None,
                          eval_default(default_expr, namespace)))
        groups.append((key, prefs))

    # object Effects 里的变量名 -> 类名 -> effectKey，再按 EFFECT_GROUPS 的 listOf 顺序取出
    obj = text[text.find("object Effects"):]
    var2cls = {}
    for m in re.finditer(r"val\s+(\w+)\s*=\s*(\w+)Effect\(\)", obj):
        var2cls[m.group(1)] = m.group(2) + "Effect"
    cls2key = {m.group(1): m.group(2) for m in matches}
    var2key = {v: cls2key[c] for v, c in var2cls.items() if c in cls2key}
    order = []
    eg = obj[obj.find("listOf("):]
    stop = eg.find(".map") if ".map" in eg else len(eg)
    for m in re.finditer(r"Effects\.(\w+)", eg[:stop]):
        key = var2key.get(m.group(1))
        if key and key not in order:
            order.append(key)
    return groups, order


def main(argv=None):
    ap = argparse.ArgumentParser(description="从 EffectGroups.kt 生成 v4a_schema.json 快照")
    ap.add_argument("--source", required=True,
                    help="EffectGroups.kt 路径（ViPER4Android 源码，只读引用）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "v4a_schema.json"),
                    help="输出 JSON 路径 (默认: tools/v4a_schema.json)")
    args = ap.parse_args(argv)

    text = open(args.source, encoding="utf-8").read()
    groups, order = parse_effect_groups_kt(text)
    by_key = dict(groups)

    out_groups = []
    for key in order:
        fields = []
        for json_key, kind, rng, default in by_key[key]:
            fields.append({
                "jsonKey": json_key,
                "kind": kind,
                "range": [rng[0], rng[1]] if rng else None,
                "default": default,
            })
        out_groups.append({"effectKey": key, "fields": fields})

    schema = {
        "schemaVersion": 2.1,
        "source": os.path.basename(args.source),
        "note": "range/default 已按 CompressorUnits.kt 换算函数求值为数值",
        "order": order,
        "groups": out_groups,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("[i] wrote %s" % os.path.abspath(args.out))
    print("[i] groups: %d, order ok: %s" % (len(order), len(order) == len(out_groups)))
    for g in out_groups:
        print("    %-22s %d fields" % (g["effectKey"], len(g["fields"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
