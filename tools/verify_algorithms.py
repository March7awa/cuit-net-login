#!/usr/bin/env python3
"""算法自检：确认内置的加密实现与真实系统逐字节一致。

用法::

    python tools/verify_algorithms.py

检查三件事：

1. ``campusnet.aes`` 命中 FIPS-197 C.1 标准测试向量；
2. ``campusnet.aes.encrypt_cryptojs_style`` 复现一份**真实门户抓包**：
   CryptoJS ``AES.encrypt(text, Base64.parse(key), {mode:ECB, padding:Pkcs7})``；
3. ``campusnet.providers.srun.xencode`` + 自定义 Base64 与门户自身的
   JavaScript 实现（``tools/srun_reference.js``，需要 node）对拍一致。

第 3 项在没装 node 时会跳过而不是失败。
"""

from __future__ import annotations

import json
import random
import shutil
import string
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet.aes import _encrypt_block, _expand_key, encrypt_cryptojs_style  # noqa: E402
from campusnet.providers.srun import srun_b64, xencode  # noqa: E402

failures = 0


def check(name: str, got, want) -> None:
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"         expected: {want}")
        print(f"         actual  : {got}")


def main() -> int:
    print("1) AES-128 单块加密 — FIPS-197 C.1 标准向量")
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    check("AES-128(key=000102..0f, pt=001122..ff)",
          _encrypt_block(plain, _expand_key(key)).hex(),
          "69c4e0d86a7b0430d8cdb78070b4c55a")

    print("\n2) CryptoJS 兼容模式 — 真实校园网抓包向量")
    print("   （成都信息工程大学 CAS 登录页，浏览器实际发出的密文）")
    check("AES-ECB-PKCS7('zzprobe0001', key=Qvy2L55c2aTCHUc6uX/Llw==)",
          encrypt_cryptojs_style("Qvy2L55c2aTCHUc6uX/Llw==", "zzprobe0001"),
          "Uz/UJgU3BdOiWO9Bfrykrg==")

    print("\n3) 深澜 xEncode + 自定义 Base64 — 与门户 JS 对拍")
    node = shutil.which("node")
    if not node:
        print("  [SKIP] 没找到 node，跳过这一项（Python 实现本身仍可用）")
    else:
        random.seed(20240918)
        cases = [
            {"msg": '{"username":"2025000000","password":"pw123456","ip":"10.18.0.100",'
                    '"acid":"1","enc_ver":"srun_bx1"}',
             "key": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},
            {"msg": "a", "key": "k"},
            {"msg": "abc", "key": "token123"},
            {"msg": '{"username":"张三","password":"密码abc","ip":"1.2.3.4"}', "key": "5f2a9c1e7b3d8046"},
            {"msg": "x" * 137, "key": "0123456789abcdef0123"},
        ]
        alphabet = string.ascii_letters + string.digits + '{}:" ,'
        for _ in range(8):
            cases.append({
                "msg": "".join(random.choice(alphabet) for _ in range(random.randint(1, 60))),
                "key": "".join(random.choice(string.hexdigits[:16]) for _ in range(random.randint(1, 40))),
            })

        proc = subprocess.run(
            [node, str(ROOT / "tools" / "srun_reference.js"), json.dumps(cases)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            check("node 运行 srun_reference.js", proc.stderr.strip()[:200], "")
        else:
            reference = json.loads(proc.stdout)
            mismatches = 0
            for case, want in zip(cases, reference):
                if srun_b64(xencode(case["msg"], case["key"])) != want:
                    mismatches += 1
                    print(f"         不一致: msg={case['msg'][:40]!r} key={case['key']!r}")
            check(f"{len(cases)} 组随机用例全部一致", mismatches, 0)

    print()
    if failures:
        print(f"{failures} 项失败 ❌")
    else:
        print("全部通过 ✅")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
