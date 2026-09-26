#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_convert.py

批量把 aep 目录下的全部 .aep 转成 ViPER4Android 预设：
  扫 <aep-dir>/*.aep -> convert_aep.build 逐个转换
  -> <presets-dir>/<原文件名>.json          （预设，仅当存在可映射节点）
  -> <parsed-dir>/<原文件名>.parse.json     （解析 dump + 映射决策/警告，仅当存在可映射节点）
  -> <manifest.json>                         （汇总清单，含置信级与校验结果）

零映射文件（无任何可映射节点，如空节点 809/999 或全为 skipped 类型）不产出预设/ dump，
只在 manifest 中记录 confidence=skipped 及原因。

此外有两张人工维护的例外表（见文件内 SKIP_SOURCES / OUT_NAME_OVERRIDE）：
  SKIP_SOURCES    按源文件名跳过（新旧语料重复时只保留新版），记 skipped + reason；
  OUT_NAME_OVERRIDE 按源文件名覆盖产物命名（让新版产物沿用用户熟知的正式名）。

用法:
  python batch_convert.py [--aep-dir aep] [--presets-dir presets] [--parsed-dir parsed]
                          [--manifest manifest.json] [--pattern "*.aep"] [--limit N]
                          [--kernels-dir kernels] [--dry-run]
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

CONF_ORDER = (convert_aep.CONF_VERIFIED, convert_aep.CONF_APPROX,
              convert_aep.CONF_PARTIAL, convert_aep.CONF_SKIPPED)

# 源文件跳过表（按 .aep 文件名）：命中则不产预设/dump，仅在 manifest 记 skipped + reason。
# 用途：同一效果存在新旧两份语料时，旧版不再重复产出，避免同名效果多份冗余预设。
SKIP_SOURCES = {
    "500-全景环绕.aep": "旧版预设（无 Gain 节点），已由新版取代；语料保留仅供参考",
}

# 产物命名覆盖表（按 .aep 文件名 -> 输出名主干）：
# 新版语料沿用用户熟知的正式名，避免产物名带 ".new" 后缀。
OUT_NAME_OVERRIDE = {
    "500-全景环绕.new.aep": "QQ音乐-全景环绕",
}


def _write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def process_one(aep_path, presets_dir, parsed_dir, schema, registry, kernels_dir):
    """转换单个文件（按需落盘）并做 schema 校验，返回 manifest 条目。"""
    base = os.path.basename(aep_path)
    stem = OUT_NAME_OVERRIDE.get(base, os.path.splitext(base)[0])
    out_preset = os.path.join(presets_dir, stem + ".json")
    out_parse = os.path.join(parsed_dir, stem + ".parse.json")
    res = convert_aep.build(aep_path, schema, registry, kernels_dir=kernels_dir)

    approx = [w for w in res["warnings"] if not w.startswith("未映射节点")]

    entry = {
        "source": os.path.relpath(aep_path, REPO).replace("\\", "/"),
        "name": res["name"],
        "node_ids": res["node_ids"],
        "node_classes": [registry.get(i, "id%d" % i) for i in res["node_ids"]],
        "confidence": res["confidence"],
        "mapped_groups": res["mapped_groups"],
        "approx_groups": res["approx_groups"],
        "approximation_warnings": approx,
        "unmapped": res["unmapped"],
        "validation": CONF_ORDER[3],  # skipped
        "preset": None,
        "parsed": None,
    }

    if base in SKIP_SOURCES:
        # 人工跳过：不产预设/dump，清理同名旧产物，仅记 manifest（reason 注明取代关系）
        entry["confidence"] = convert_aep.CONF_SKIPPED
        entry["reason"] = SKIP_SOURCES[base]
        for pth in (out_preset, out_parse):
            if os.path.exists(pth):
                os.remove(pth)
        return entry

    if not res["has_mapping"]:
        nodes_desc = ", ".join("id%d(%s)" % (i, registry.get(i, "id%d" % i)) for i in res["node_ids"])
        entry["reason"] = ("零映射：%s 均无可对应 V4A 语义，未产出预设"
                           % (nodes_desc if nodes_desc else "无任何节点"))
        for pth in (out_preset, out_parse):  # 清理可能存在的旧产物
            if os.path.exists(pth):
                os.remove(pth)
        return entry

    _write_json(out_preset, res["preset"])
    res["dump"]["suggested_dump_path"] = out_parse
    _write_json(out_parse, res["dump"])

    preset = json.load(open(out_preset, encoding="utf-8"))
    errors, _warn = validate_preset.validate(preset, schema)
    entry["validation"] = "pass" if not errors else "fail"
    entry["validation_errors"] = errors
    entry["preset"] = os.path.relpath(out_preset, REPO).replace("\\", "/")
    entry["parsed"] = os.path.relpath(out_parse, REPO).replace("\\", "/")
    return entry


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
    ap.add_argument("--kernels-dir", default=os.path.join(REPO, "kernels"),
                    help="解密 IR kernel 目录 (默认: kernels)")
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
            e = process_one(f, args.presets_dir, args.parsed_dir, schema, registry, args.kernels_dir)
        except Exception as exc:  # noqa: BLE001 - 批量需记录单个失败而不中断
            failed += 1
            print("[FAIL] %s: %s: %s" % (os.path.basename(f), type(exc).__name__, exc))
            entries.append({"source": os.path.relpath(f, REPO).replace("\\", "/"),
                            "error": "%s: %s" % (type(exc).__name__, exc),
                            "confidence": "error", "validation": "fail",
                            "preset": None, "parsed": None})
            continue
        entries.append(e)
        mark = {"pass": "OK ", "fail": "BAD", "skipped": "---"}[e["validation"]]
        print("[%s] %-26s conf=%-8s nodes=%-26s mapped=%s" % (
            mark, os.path.basename(f), e["confidence"], e["node_ids"], e["mapped_groups"]))

    n_pass = sum(1 for e in entries if e.get("validation") == "pass")
    n_fail = sum(1 for e in entries if e.get("validation") == "fail")
    n_skip = sum(1 for e in entries if e.get("validation") == "skipped")
    conf_counts = {c: sum(1 for e in entries if e.get("confidence") == c) for c in CONF_ORDER}

    manifest = {
        "generatedAt": int(time.time() * 1000),
        "schema": os.path.relpath(args.schema, REPO).replace("\\", "/"),
        "aep_dir": os.path.relpath(args.aep_dir, REPO).replace("\\", "/"),
        "count": len(entries),
        "presets_produced": n_pass + n_fail,
        "confidence": conf_counts,
        "validation": {"pass": n_pass, "fail": n_fail, "skipped_no_preset": n_skip},
        "entries": entries,
    }
    _write_json(args.manifest, manifest)

    print("-" * 72)
    print("[i] files: %d, presets produced: %d (validation pass %d / fail %d), no-preset(skipped): %d"
          % (len(entries), n_pass + n_fail, n_pass, n_fail, n_skip))
    print("[i] confidence: %s" % conf_counts)
    print("[i] manifest  : %s" % os.path.abspath(args.manifest))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
