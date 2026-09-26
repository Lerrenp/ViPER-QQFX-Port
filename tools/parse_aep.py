#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_aep.py

解析 QQ 音乐 ".aep" 音效文件（QMAEP，FlatBuffers 风格的自定义变体），
输出结构化 JSON 并做关键值自检。

文件格式（经 hex 分析 + 逐字节验证）：
  文件起点:  uoffset32 -> root table
  root table (3 fields):
      field0 -> string "QMAEP"        (magic)
      field1 -> string "全景环绕"      (效果名, UTF-8, 前缀 u32 长度)
      field2 -> vector<EffectNode>
  EffectNode table (2 fields):
      field0 -> int32  node id  (标量, 例如 13=EQ, 4/11/12=Widener 子组)
      field1 -> vector<Param>
  Param table (3 fields):
      field0 -> string name            (参数名)
      field1 -> struct{ u32 typeTag; float32 value }
      field2 -> string unit / 备用字符串 (本文件恒为空串)
  string:  u32 长度 + UTF-8 内容 + '\0'
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 脚本可放在 workspace 或 workspace/output 下，均能定位到源文件
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "output" else HERE
AEP_PATH = os.path.join(ROOT, "500-全景环绕.aep")
OUT_PATH = os.path.join(ROOT, "output", "parsed_全景环绕.json")


class Reader:
    def __init__(self, data):
        self.d = data

    def u16(self, o):
        return struct.unpack_from("<H", self.d, o)[0]

    def i32(self, o):
        return struct.unpack_from("<i", self.d, o)[0]

    def u32(self, o):
        return struct.unpack_from("<I", self.d, o)[0]

    def f32(self, o):
        return struct.unpack_from("<f", self.d, o)[0]


def hx(n):
    return "0x%X" % n


def table_info(r, pos):
    """Return (vtable_offset, vtable_size, object_size, [field_offsets])."""
    soffset = r.i32(pos)
    vt = pos - soffset
    vsize = r.u16(vt)
    osize = r.u16(vt + 2)
    n = max(0, (vsize - 4) // 2)
    fields = [r.u16(vt + 4 + 2 * i) for i in range(n)]
    return vt, vsize, osize, fields


def field_pos(pos, off):
    """Absolute position of a field value, or None when the field is absent."""
    if off == 0:
        return None
    return pos + off


def read_string(r, target):
    """u32 length + UTF-8 bytes (+ NUL). Returns (value, length_prefix_offset, content_offset)."""
    ln = r.u32(target)
    content = r.d[target + 4:target + 4 + ln]
    return content.decode("utf-8"), target, target + 4


def read_uoffset_vec(r, target):
    """vector of uoffsets -> list of (element_pos, absolute_target)."""
    ln = r.u32(target)
    out = []
    for i in range(ln):
        ep = target + 4 + 4 * i
        out.append((ep, ep + r.u32(ep)))
    return out, target


def parse_param(r, pos):
    vt, vsize, osize, fields = table_info(r, pos)
    f0 = field_pos(pos, fields[0]) if len(fields) > 0 else None
    f1 = field_pos(pos, fields[1]) if len(fields) > 1 else None
    f2 = field_pos(pos, fields[2]) if len(fields) > 2 else None

    name, name_len_off, name_content_off = read_string(r, f0 + r.u32(f0))
    # f1 -> struct { u32 typeTag; float32 value }
    pv = f1 + r.u32(f1)
    type_tag = r.u32(pv)
    value = r.f32(pv + 4)
    unit = None
    if f2 is not None:
        unit, _, _ = read_string(r, f2 + r.u32(f2))
    return {
        "table_offset": pos,
        "table_offset_hex": hx(pos),
        "vtable_offset": vt,
        "name": name,
        "name_offset": name_len_off,
        "name_offset_hex": hx(name_len_off),
        "type_tag": type_tag,
        "value": value,
        "value_offset": pv + 4,
        "value_offset_hex": hx(pv + 4),
        "unit": unit,
    }


def parse_node(r, pos):
    vt, vsize, osize, fields = table_info(r, pos)
    f0 = field_pos(pos, fields[0])
    f1 = field_pos(pos, fields[1])
    node_id = r.u32(f0)  # scalar int32
    vec, vec_off = read_uoffset_vec(r, f1 + r.u32(f1))
    params = []
    for ep, tgt in vec:
        params.append(parse_param(r, tgt))
    return {
        "id": node_id,
        "table_offset": pos,
        "table_offset_hex": hx(pos),
        "vtable_offset": vt,
        "vector_offset": vec_off,
        "vector_offset_hex": hx(vec_off),
        "param_count": len(params),
        "params": params,
    }


def parse(data):
    r = Reader(data)
    root_off = r.u32(0)
    vt, vsize, osize, fields = table_info(r, root_off)

    f0 = field_pos(root_off, fields[0])
    f1 = field_pos(root_off, fields[1])
    f2 = field_pos(root_off, fields[2])

    magic, magic_off, _ = read_string(r, f0 + r.u32(f0))
    name, name_off, _ = read_string(r, f1 + r.u32(f1))
    nodes_vec, nodes_vec_off = read_uoffset_vec(r, f2 + r.u32(f2))
    nodes = [parse_node(r, tgt) for _, tgt in nodes_vec]

    return {
        "magic": magic,
        "magic_offset": magic_off,
        "magic_offset_hex": hx(magic_off),
        "name": name,
        "name_offset": name_off,
        "name_offset_hex": hx(name_off),
        "nodes_vector_offset": nodes_vec_off,
        "root": {
            "table_offset": root_off,
            "table_offset_hex": hx(root_off),
            "vtable_offset": vt,
            "vtable_offset_hex": hx(vt),
            "vtable_size": vsize,
            "object_size": osize,
            "field_offsets": fields,
        },
        "nodes": nodes,
    }


EQ_FREQS = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_REF = {31: 0.0, 63: 1.0, 125: 1.0, 250: 0.0, 500: 0.0,
          1000: 1.0, 2000: 1.0, 4000: 1.0, 8000: 2.0, 16000: 2.0}
Q_REF = (1.5) ** 0.5  # 1.2247449

WIDENER_REF = {
    "Left Time": 0.0,
    "Right Time": 26.1224,
    "Left Feedback": 0.0,
    "Right Feedback": 0.0,
    "Center": 100.0,
    "Width": 130.0,
    "Gain": 2.2789,
}


def build_report(parsed, size):
    # collect every param with its node id
    flat = []
    for node in parsed["nodes"]:
        for p in node["params"]:
            flat.append((node, p))

    # EQ bands: names like "NNNNN Hz"
    eq_items = []
    q_item = None
    for node, p in flat:
        nm = p["name"]
        if nm.endswith(" Hz"):
            hz = int(nm.split()[0])
            eq_items.append({
                "frequency_hz": hz,
                "frequency_label": nm,
                "gain_db": p["value"],
                "q": None,          # filled below
                "node_id": node["id"],
                "item_offset": p["table_offset"],
                "item_offset_hex": p["table_offset_hex"],
                "name_offset": p["name_offset"],
                "value_offset": p["value_offset"],
                "value_offset_hex": p["value_offset_hex"],
                "type_tag": p["type_tag"],
            })
        elif nm == "Q":
            q_item = {
                "name": "Q",
                "value": p["value"],
                "node_id": node["id"],
                "item_offset": p["table_offset"],
                "item_offset_hex": p["table_offset_hex"],
                "name_offset": p["name_offset"],
                "value_offset": p["value_offset"],
                "value_offset_hex": p["value_offset_hex"],
                "type_tag": p["type_tag"],
            }

    eq_items.sort(key=lambda e: e["frequency_hz"])
    q_val = q_item["value"] if q_item else None
    for e in eq_items:
        e["q"] = q_val

    # widener: everything that is not an EQ band / Q
    wid = {}
    wid_off = {}
    for node, p in flat:
        nm = p["name"]
        if nm.endswith(" Hz") or nm == "Q":
            continue
        wid[nm] = p["value"]
        wid_off[nm] = {
            "node_id": node["id"],
            "item_offset": p["table_offset"],
            "item_offset_hex": p["table_offset_hex"],
            "name_offset": p["name_offset"],
            "value_offset": p["value_offset"],
            "value_offset_hex": p["value_offset_hex"],
            "type_tag": p["type_tag"],
        }

    report = {
        "file": AEP_PATH,
        "file_size": size,
        "magic": parsed["magic"],
        "magic_offset_hex": parsed["magic_offset_hex"],
        "name": parsed["name"],
        "name_offset_hex": parsed["name_offset_hex"],
        "offset_index": {
            "root_table": parsed["root"]["table_offset_hex"],
            "root_vtable": parsed["root"]["vtable_offset_hex"],
            "magic_string": parsed["magic_offset_hex"],
            "name_string": parsed["name_offset_hex"],
            "nodes_vector": hx(parsed["nodes_vector_offset"]),
        },
        "eq": eq_items,
        "eq_q": q_item,
        "widener": wid,
        "widener_offsets": wid_off,
        "nodes": [
            {
                "id": n["id"],
                "table_offset": n["table_offset"],
                "table_offset_hex": n["table_offset_hex"],
                "vtable_offset": n["vtable_offset"],
                "param_count": n["param_count"],
                "param_names": [p["name"] for p in n["params"]],
                "param_offsets": [
                    {"name": p["name"], "item_offset": p["table_offset"],
                     "item_offset_hex": p["table_offset_hex"],
                     "value_offset": p["value_offset"],
                     "value_offset_hex": p["value_offset_hex"]}
                    for p in n["params"]
                ],
            }
            for n in parsed["nodes"]
        ],
        # 其他发现的字段：将所有原始 (float, name, 偏移) 对完整列出
        "all_float_name_pairs": [
            {
                "name": p["name"],
                "value": p["value"],
                "type_tag": p["type_tag"],
                "node_id": n["id"],
                "value_offset": p["value_offset"],
                "value_offset_hex": p["value_offset_hex"],
            }
            for n, p in flat
        ],
    }
    return report


def self_check(report):
    errors = []
    if report["magic"] != "QMAEP":
        errors.append("magic expected 'QMAEP', got %r" % report["magic"])
    if report["name"] != "全景环绕":
        errors.append("name expected '全景环绕', got %r" % report["name"])

    got_eq = {e["frequency_hz"]: round(e["gain_db"], 6) for e in report["eq"]}
    if len(report["eq"]) != 10:
        errors.append("EQ band count expected 10, got %d" % len(report["eq"]))
    for f, v in EQ_REF.items():
        if f not in got_eq:
            errors.append("EQ missing %d Hz" % f)
        elif abs(got_eq[f] - v) > 1e-6:
            errors.append("EQ %d Hz expected %s, got %s" % (f, v, got_eq[f]))

    qv = report["eq_q"]["value"] if report["eq_q"] else None
    if qv is None or abs(qv - Q_REF) > 1e-5:
        errors.append("Q expected ~%.6f, got %s" % (Q_REF, qv))

    for k, v in WIDENER_REF.items():
        if k not in report["widener"]:
            errors.append("widener missing %r" % k)
            continue
        gv = report["widener"][k]
        if abs(gv - v) > 1e-3:
            errors.append("widener %r expected %s, got %s" % (k, v, gv))
    return errors


def main():
    with open(AEP_PATH, "rb") as f:
        data = f.read()
    print("[i] file: %s (%d bytes)" % (AEP_PATH, len(data)))

    parsed = parse(data)
    report = build_report(parsed, len(data))

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("[i] magic = %s (offset %s)" % (report["magic"], report["magic_offset_hex"]))
    print("[i] name  = %s (offset %s)" % (report["name"], report["name_offset_hex"]))
    print("[i] nodes = %s" % [(n["id"], n["param_names"]) for n in report["nodes"]])
    print("[i] EQ (ascending):")
    for e in report["eq"]:
        print("      %7s : %+.4f dB  (value @ %s)" % (e["frequency_label"], e["gain_db"], e["value_offset_hex"]))
    print("[i] Q = %.6f (value @ %s)" % (report["eq_q"]["value"], report["eq_q"]["value_offset_hex"]))
    print("[i] widener:")
    for k in ["Left Time", "Right Time", "Left Feedback", "Right Feedback", "Center", "Width", "Gain"]:
        if k in report["widener"]:
            print("      %-15s : %.4f (value @ %s)" % (k, report["widener"][k], report["widener_offsets"][k]["value_offset_hex"]))
    print("[i] wrote %s" % OUT_PATH)

    errors = self_check(report)
    if errors:
        print("[X] SELF-CHECK FAILED:")
        for e in errors:
            print("    - " + e)
        sys.exit(1)
    print("[OK] self-check passed: all values match reference.")


if __name__ == "__main__":
    main()
