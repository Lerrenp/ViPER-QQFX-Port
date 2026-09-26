#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_preset.py

ViPER4Android 预设 JSON 的独立校验器：完全基于 tools/v4a_schema.json 快照，
不依赖 ViPER4Android 源码路径。

校验项：
  1) 组名 (effectKey) 的集合与顺序 == schema.order；
  2) 每组字段名 (jsonKey) 与 schema 逐字一致（不多不少）；
  3) 每个数值的类型符合 schema.kind；
  4) 每个数值（含列表元素）落在 schema.range 内；
  5) 默认值一致性：对「带 enable 字段且未启用」的组，所有字段值必须等于 schema.default
     （带 enable 的组若 enable=true 则跳过，视为有意修改）。

用法:
  python validate_preset.py --preset presets/QQ音乐-全景环绕.json [--preset ...] [--schema tools/v4a_schema.json]
"""
import argparse
import json
import math
import os
import sys

META_KEYS = ("schemaVersion", "name", "createdAt")


def check_type(kind, v):
    if kind == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind in ("float", "double"):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if kind == "bool":
        return isinstance(v, bool)
    if kind == "string":
        return isinstance(v, str)
    if kind == "nullableLong":
        return v is None or (isinstance(v, int) and not isinstance(v, bool))
    if kind == "intList":
        return isinstance(v, list) and all(isinstance(x, int) and not isinstance(x, bool) for x in v)
    if kind in ("floatList", "doubleList"):
        return isinstance(v, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
    if kind == "boolList":
        return isinstance(v, list) and all(isinstance(x, bool) for x in v)
    return True


def _eq(a, b):
    """值相等判断，容忍 int/float 表示差异。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    return a == b


def validate(preset, schema):
    """返回 (errors, warnings)。preset 为已加载 dict，schema 为 v4a_schema.json 内容。"""
    errors, warnings = [], []
    order = schema["order"]
    by_key = {g["effectKey"]: g["fields"] for g in schema["groups"]}

    keys = [k for k in preset if k not in META_KEYS]
    if keys != order:
        errors.append("group order/set mismatch\n  preset : %s\n  schema : %s" % (keys, order))

    for key in keys:
        if key not in by_key:
            errors.append("unknown group %r" % key)
            continue
        fields = by_key[key]
        prefs = {f["jsonKey"]: f for f in fields}
        obj = preset[key]
        if not isinstance(obj, dict):
            errors.append("%s: expected object, got %s" % (key, type(obj).__name__))
            continue
        extra = set(obj) - set(prefs)
        missing = set(prefs) - set(obj)
        if extra:
            errors.append("%s: unknown field(s) %s" % (key, sorted(extra)))
        if missing:
            errors.append("%s: missing field(s) %s" % (key, sorted(missing)))

        for jk, f in prefs.items():
            if jk not in obj:
                continue
            v = obj[jk]
            if not check_type(f["kind"], v):
                errors.append("%s.%s: expected %s, got %r" % (key, jk, f["kind"], v))
                continue
            rng = f["range"]
            if rng is not None and v is not None and not isinstance(v, bool):
                vals = v if isinstance(v, list) else [v]
                lo, hi = rng
                for x in vals:
                    if isinstance(x, (int, float)) and not (lo - 1e-9 <= x <= hi + 1e-9):
                        errors.append("%s.%s: value %r outside range [%g, %g]" % (key, jk, x, lo, hi))

        # 默认值一致性：仅对「带 enable 且未启用」的组生效
        has_enable = "enable" in prefs
        enabled = obj.get("enable", None)
        if has_enable and enabled is not True:
            for jk, f in prefs.items():
                if jk not in obj:
                    continue
                if not _eq(obj[jk], f["default"]):
                    errors.append("%s.%s: value %r != schema default %r (group not enabled)"
                                  % (key, jk, obj[jk], f["default"]))
    return errors, warnings


def _load_schema(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _default_schema_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "v4a_schema.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description="校验 ViPER4Android 预设 JSON")
    ap.add_argument("--preset", action="append", required=True,
                    help="待校验的预设 JSON（可多次指定）")
    ap.add_argument("--schema", default=_default_schema_path(),
                    help="schema 快照路径 (默认: tools/v4a_schema.json)")
    args = ap.parse_args(argv)

    schema = _load_schema(args.schema)
    print("[i] schema: %s (%d groups)" % (os.path.abspath(args.schema), len(schema["groups"])))

    failed = False
    for preset_path in args.preset:
        with open(preset_path, encoding="utf-8") as f:
            preset = json.load(f)
        errors, warnings = validate(preset, schema)
        keys = [k for k in preset if k not in META_KEYS]
        print("[i] --- %s" % os.path.abspath(preset_path))
        print("    name=%r groups=%d enabled=%s"
              % (preset.get("name"), len(keys),
                 [k for k in keys if isinstance(preset.get(k), dict) and preset[k].get("enable") is True]))
        for w in warnings:
            print("    [!] " + w)
        if errors:
            failed = True
            print("    [X] VALIDATION FAILED:")
            for e in errors:
                print("        - " + e)
        else:
            print("    [OK] passed: group order/fields/types/ranges/defaults consistent with schema.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
