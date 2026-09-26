#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aep_parser.py

QQ 音乐 ".aep" 音效文件（QMAEP，FlatBuffers 风格的自定义变体）通用解析库。
不含任何针对单个样本的硬编码断言，可直接用于批量解析。

文件格式（经 hex 分析 + 逐字节验证，详见 docs/分析报告.md）：
  文件起点:  uoffset32 -> root table
  root table (3 fields):
      field0 -> string "QMAEP"        (magic)
      field1 -> string 效果名          (UTF-8, 前缀 u32 长度)
      field2 -> vector<EffectNode>
  EffectNode table (2 fields):
      field0 -> int32  node id        (标量)
      field1 -> vector<Param>
  Param table (3 fields):
      field0 -> string name           (参数名)
      field1 -> value blob: u32 长度 + 原始字节
                  长度 == 4 -> 小端 float32 标量（绝大多数参数的形态）
                  其他      -> UTF-8 字符串（如 "irs\\xxx.enc"，带结尾 '\\0'）
      field2 -> string unit / 备用字符串 (通常为空串)

table 前有 vtable：int32 soffset（相对 table 位置的有符号偏移），
vtable 结构 = u16 vtable_size, u16 object_size, u16 field_off[0..n-1]；
字段偏移 0 表示该字段缺省。

对外接口：
  parse(path)      -> {"path", "magic", "name", "nodes":[{"id", "params":{名: 值}}]}
  parse_bytes(data)-> 同上（不含 path）
  AepParseError    解析失败/缺魔数时抛出
"""
import json
import os
import struct
import sys

MAGIC = "QMAEP"


class AepParseError(Exception):
    """aep 文件结构非法、越界或魔数不符时抛出。"""


class Reader:
    """带边界检查的小端读取器；越界统一抛 AepParseError。"""

    def __init__(self, data):
        self.d = data
        self.n = len(data)

    def _need(self, o, size):
        if o < 0 or o + size > self.n:
            raise AepParseError("read out of bounds: offset=%d size=%d file=%d" % (o, size, self.n))

    def u16(self, o):
        self._need(o, 2)
        return struct.unpack_from("<H", self.d, o)[0]

    def i32(self, o):
        self._need(o, 4)
        return struct.unpack_from("<i", self.d, o)[0]

    def u32(self, o):
        self._need(o, 4)
        return struct.unpack_from("<I", self.d, o)[0]

    def f32(self, o):
        self._need(o, 4)
        return struct.unpack_from("<f", self.d, o)[0]

    def raw(self, o, size):
        self._need(o, size)
        return self.d[o:o + size]


def table_info(r, pos):
    """返回 (vtable_offset, vtable_size, object_size, [field_offsets])。"""
    soffset = r.i32(pos)
    vt = pos - soffset
    if vt < 0 or vt + 4 > r.n:
        raise AepParseError("bad vtable offset at %d (vt=%d)" % (pos, vt))
    vsize = r.u16(vt)
    osize = r.u16(vt + 2)
    if vsize < 4 or (vsize - 4) % 2 != 0:
        raise AepParseError("bad vtable size %d at %d" % (vsize, vt))
    n = (vsize - 4) // 2
    fields = [r.u16(vt + 4 + 2 * i) for i in range(n)]
    return vt, vsize, osize, fields


def field_pos(pos, off):
    """字段值绝对位置；off==0 表示字段缺省，返回 None。"""
    return None if off == 0 else pos + off


def read_string(r, target):
    """u32 长度 + UTF-8 内容 (+ '\\0')。返回解码后的字符串。"""
    ln = r.u32(target)
    if ln > r.n:
        raise AepParseError("string length %d too large at %d" % (ln, target))
    content = r.raw(target + 4, ln)
    try:
        s = content.decode("utf-8")
    except UnicodeDecodeError:
        raise AepParseError("string at %d is not valid UTF-8" % target)
    return s.rstrip("\x00")


def read_value(r, target):
    """value blob：u32 长度 + 原始字节；长度 4 视为 float32，否则视为 UTF-8 字符串。"""
    ln = r.u32(target)
    if ln > (1 << 20):
        raise AepParseError("value blob length %d too large at %d" % (ln, target))
    if ln == 4:
        return r.f32(target + 4)
    return read_string(r, target)


def read_uoffset_vec(r, target):
    """vector of uoffset32 -> 元素绝对位置列表。"""
    ln = r.u32(target)
    if ln > r.n:
        raise AepParseError("vector length %d too large at %d" % (ln, target))
    out = []
    for i in range(ln):
        ep = target + 4 + 4 * i
        out.append(ep + r.u32(ep))
    return out


def parse_bytes(data):
    """解析 aep 字节串，返回结构化 dict（不含 path）。"""
    if len(data) < 4:
        raise AepParseError("file too small (%d bytes)" % len(data))
    r = Reader(data)
    root = r.u32(0)
    vt, vsize, osize, fields = table_info(r, root)
    if len(fields) < 3:
        raise AepParseError("root table has %d fields, expected >=3" % len(fields))
    f0 = field_pos(root, fields[0])
    f1 = field_pos(root, fields[1])
    f2 = field_pos(root, fields[2])

    magic = read_string(r, f0 + r.u32(f0))
    if magic != MAGIC:
        raise AepParseError("bad magic %r, expected %r" % (magic, MAGIC))
    name = read_string(r, f1 + r.u32(f1))

    nodes = []
    vectarget = f2 + r.u32(f2)
    for npos in read_uoffset_vec(r, vectarget):
        nvt, nvs, nos, nf = table_info(r, npos)
        if len(nf) < 2:
            raise AepParseError("node table has %d fields, expected >=2" % len(nf))
        node_id = r.u32(field_pos(npos, nf[0]))
        params = {}
        vpos = field_pos(npos, nf[1])
        for ppos in read_uoffset_vec(r, vpos + r.u32(vpos)):
            pvt, pvs, pos_, pf = table_info(r, ppos)
            if len(pf) < 2:
                raise AepParseError("param table has %d fields, expected >=2" % len(pf))
            name_off = field_pos(ppos, pf[0])
            pname = read_string(r, name_off + r.u32(name_off))
            val_off = field_pos(ppos, pf[1])
            params[pname] = read_value(r, val_off + r.u32(val_off))
        nodes.append({"id": node_id, "params": params})

    return {"magic": magic, "name": name, "nodes": nodes}


def parse(path):
    """解析 aep 文件（路径），返回 {"path","magic","name","nodes":[...]}。"""
    with open(path, "rb") as f:
        data = f.read()
    result = parse_bytes(data)
    result["path"] = os.path.abspath(path)
    result["file_size"] = len(data)
    return result


def node_id_list(parsed):
    """节点 id 序列（按文件内顺序）。"""
    return [n["id"] for n in parsed["nodes"]]


def _smoke(dirpath, pattern="*.aep"):
    """对目录内所有 .aep 做 smoke test：打印每个文件的节点 id 集合与总体分布。"""
    import glob
    from collections import Counter
    files = sorted(glob.glob(os.path.join(dirpath, pattern)))
    if not files:
        print("[X] no files matching %s in %s" % (pattern, dirpath))
        return 1
    combos = Counter()
    idfreq = Counter()
    failed = []
    for fp in files:
        try:
            parsed = parse(fp)
        except Exception as e:  # noqa: BLE001 - smoke test 需完整报告失败
            failed.append((os.path.basename(fp), "%s: %s" % (type(e).__name__, e)))
            print("[FAIL] %-28s %s: %s" % (os.path.basename(fp), type(e).__name__, e))
            continue
        ids = node_id_list(parsed)
        combos[tuple(ids)] += 1
        for i in set(ids):
            idfreq[i] += 1
        print("  %-28s name=%-8s nodes=%s" % (os.path.basename(fp), parsed["name"], ids))
    print("-" * 60)
    print("[i] files: %d, parsed ok: %d, failed: %d" % (len(files), len(files) - len(failed), len(failed)))
    print("[i] node-id frequency (files containing id): %s" % dict(sorted(idfreq.items())))
    print("[i] distinct node-id sequences: %d" % len(combos))
    for combo, c in combos.most_common():
        print("      x%-3d %s" % (c, list(combo)))
    return 1 if failed else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("用法: python aep_parser.py <file.aep>             # 打印单文件摘要")
        print("      python aep_parser.py --dir <目录> [--pattern *.aep]  # smoke test")
        print("      python aep_parser.py --json <file.aep>      # 输出 JSON")
        return 0
    if argv[0] == "--dir":
        dirpath = argv[1] if len(argv) > 1 else "."
        pattern = "*.aep"
        if "--pattern" in argv:
            pattern = argv[argv.index("--pattern") + 1]
        return _smoke(dirpath, pattern)
    as_json = argv[0] == "--json"
    path = argv[1] if as_json else argv[0]
    try:
        parsed = parse(path)
    except (AepParseError, OSError) as e:
        print("[X] parse failed: %s: %s" % (type(e).__name__, e))
        return 1
    if as_json:
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    else:
        print("[i] file: %s (%d bytes)" % (parsed["path"], parsed.get("file_size", -1)))
        print("[i] magic = %s" % parsed["magic"])
        print("[i] name  = %s" % parsed["name"])
        for n in parsed["nodes"]:
            print("    node %-3d %s" % (n["id"], n["params"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
