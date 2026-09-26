#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 qmcpcomx64.dll 提取 effect 工厂派发表 (id -> 类名/构造函数/对象大小)。
数据来源:
  ss_op create_effect 内部函数 0x18008e010 -> 派发表查找 0x180099ec0
  跳转表: VA 0x18009ab1c, 基数 = ImageBase, 覆盖 type-1 in [0,0x4b] -> id 1..76
  每个 case: operator new(size) + 构造函数(call 0x1800xxxxx); 构造函数内 lea rax,[vtable];
            由 vtable[-8] 的 COL 解析 RTTI 类名。
严禁运行 DLL，仅静态读取。"""
import pefile, struct, capstone, json, os

DLL = r"D:\AI-Agent\ZCode\workspace\QQMusic去exe版\qmcpcomx64.dll"
p = pefile.PE(DLL)
ib = p.OPTIONAL_HEADER.ImageBase
mm = p.get_memory_mapped_image()
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64); md.detail = True

def rd(va, n): return mm[va - ib: va - ib + n]
def i32(va): return struct.unpack('<i', rd(va, 4))[0]
def disasm(va, n=0x60): return list(md.disasm(rd(va, n), va))

def rtti(vt):
    try:
        col = struct.unpack('<Q', rd(vt - 8, 8))[0]
        cr = col - ib
        if cr < 0 or cr + 20 > len(mm): return None
        sig, off, cd, ptd, pcd = struct.unpack_from('<IIIII', mm, cr)
        if sig == 1 and ptd + 16 < len(mm):
            return mm[ptd + 16: ptd + 16 + 160].split(b'\0')[0].decode('latin1')
    except Exception:
        pass
    return None

TBL = 0x18009ab1c
rows = []
for T in range(1, 77):
    disp = i32(TBL + (T - 1) * 4)
    tgt = ib + disp
    ctor = size = None
    for x in disasm(tgt, 0x50):
        if x.mnemonic == 'mov' and x.op_str.startswith('ecx, 0x') and size is None:
            size = x.op_str.split('0x')[1]
        if x.mnemonic == 'call' and x.op_str.startswith('0x1800'):
            ctor = int(x.op_str, 16); break
        if x.mnemonic == 'jmp': break
    cls = None; vt = None
    if ctor:
        for y in disasm(ctor, 0x160):
            if y.mnemonic == 'lea' and 'rip' in y.op_str:
                m = y.operands[-1]
                if m.type == capstone.x86.X86_OP_MEM and m.mem.base == capstone.x86.X86_REG_RIP:
                    cand = y.address + y.size + m.mem.disp
                    c = rtti(cand)
                    if c:
                        vt = cand; cls = c; break
    rows.append({"id": T, "case": hex(tgt), "new_size": ("0x" + size) if size else None,
                 "ctor": hex(ctor) if ctor else None, "vtable": hex(vt) if vt else None,
                 "class": cls})

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "effect_registry.json")
json.dump({"source": DLL, "table_va": hex(TBL), "base": hex(ib), "count": len(rows), "effects": rows},
          open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("wrote", out)
for r in rows:
    print("%3d %-45s %s" % (r["id"], r["class"], r["ctor"]))
