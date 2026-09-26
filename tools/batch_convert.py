#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_convert.py

批量把 aep 目录下的全部 .aep 转成 ViPER4Android 预设：
  扫 <aep-dir>/*.aep -> convert_aep.convert 逐个转换
  -> <presets-dir>/<原文件名>.json          （预设）
  -> <parsed-dir>/<原文件名>.parse.json     （解析 dump + 映射决策/警告）
  -> <manifest.json>                         （汇总清单，含校验结果）

用法:
  python batch_convert.py [--aep-dir aep] [--presets-dir presets] [--parsed-dir parsed]
                          [--manifest manifest.json] [--pattern "*.aep"] [--limit N] [--dry-run]
"""
import argparse
import glob
import json
import os
import sys
import time

import convert_aep
import validate_preset

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def process_one(aep_path, presets_dir, parsed_dir, schema, registry):
    """转换单个文件并做 schema 校验，返回 manifest 条目。"""
    stem = os.path.splitext(os.path.basename(aep_path))[0]
    out_preset = os.path.join(presets_dir, stem + ".json")
    out_parse = os.path.join(parsed_dir, stem + ".parse.json")
    res = convert_aep.convert(aep_path, out_preset, schema, registry, dump_path=out_parse)

    preset = json.load(open(out_preset, encoding="utf-8"))
    errors, _warn = validate_preset.validate(preset, schema)
    approx = [w for w in res["warnings"] if ("钳到" in w or "未映射节点" not in w)]

    return {
        "source": os.path.relpath(aep_path, REPO).replace("\\", "/"),
        "name": res["name"],
        "node_ids": res["node_ids"],
        "node_classes": [registry.get(i, "id%d" % i) for i in res["node_ids"]],
        "mapped_groups": res["mapped_groups"],
        "approximation_warnings": approx,
        "unmapped": res["unmapped"],
        "validation": "pass" if not errors else "fail",
        "validation_errors": errors,
        "preset": os.path.relpath(out_preset, REPO).replace("\\", "/"),
        "parsed": os.path.relpath(out_parse, REPO).replace("\\", "/"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="批量转换 aep 目录 -> V4A 预设 + manifest")
    ap.add_argument("--aep-dir", default=os.path.join(REPO, "aep"),
                    help=".aep 所在目录 (默认: aep)")
    ap.add_argument("--presets-dir", default=os.path.join(REPO, "presets"),
                    help="预设输出目录 (默认: presets)")
    ap.add_argument("--parsed-dir", default=os.path.join(REPO, "parsed"),
                    help="解析 dump 输出目录 (默认: parsed)")
    ap.add_argument("--manifest", default=os.path.join(REPO, "manifest.json"),
                    help="汇总清单输出路径 (默认: manifest.json)")
    ap.add_argument("--pattern", default="*.aep", help="文件匹配模式 (默认: *.aep)")
    ap.add_argument("--schema", default=os.path.join(HERE, "v4a_schema.json"),
                    help="schema 快照 (默认: tools/v4a_schema.json)")
    ap.add_argument("--registry", default=os.path.join(REPO, "docs", "effect_registry.json"),
                    help="插件注册表 (默认: docs/effect_registry.json)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件（0=全部）")
    ap.add_argument("--dry-run", action="store_true", help="只列出待处理文件，不写任何输出")
    args = ap.parse_args(argv)

    files = sorted(glob.glob(os.path.join(args.aep_dir, args.pattern)))
    if args.limit and args.limit > 0:
        files = files[:args.limit]
    if not files:
        print("[X] no files matching %s in %s" % (args.pattern, args.aep_dir))
        return 1

    print("[i] aep dir   : %s" % os.path.abspath(args.aep_dir))
    print("[i] files     : %d" % len(files))
    if args.dry_run:
        for f in files:
            print("    would convert %s" % os.path.relpath(f, REPO))
        return 0

    os.makedirs(args.presets_dir, exist_ok=True)
    os.makedirs(args.parsed_dir, exist_ok=True)
    schema = json.load(open(args.schema, encoding="utf-8"))
    registry = convert_aep.load_registry(args.registry)

    entries, failed = [], 0
    for f in files:
        try:
            e = process_one(f, args.presets_dir, args.parsed_dir, schema, registry)
        except Exception as exc:  # noqa: BLE001 - 批量需记录单个失败而不中断
            failed += 1
            print("[FAIL] %s: %s: %s" % (os.path.basename(f), type(exc).__name__, exc))
            entries.append({"source": os.path.relpath(f, REPO).replace("\\", "/"),
                            "error": "%s: %s" % (type(exc).__name__, exc), "validation": "fail"})
            continue
        entries.append(e)
        mark = "OK " if e["validation"] == "pass" else "BAD"
        print("[%s] %-26s nodes=%-28s mapped=%s" % (
            mark, os.path.basename(f), e["node_ids"], e["mapped_groups"]))

    n_pass = sum(1 for e in entries if e.get("validation") == "pass")
    n_fail = len(entries) - n_pass
    manifest = {
        "generatedAt": int(time.time() * 1000),
        "schema": os.path.relpath(args.schema, REPO).replace("\\", "/"),
        "aep_dir": os.path.relpath(args.aep_dir, REPO).replace("\\", "/"),
        "count": len(entries),
        "validation": {"pass": n_pass, "fail": n_fail},
        "entries": entries,
    }
    with open(args.manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print("-" * 60)
    print("[i] converted: %d, validation pass: %d, fail: %d" % (len(entries), n_pass, n_fail))
    print("[i] manifest  : %s" % os.path.abspath(args.manifest))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
