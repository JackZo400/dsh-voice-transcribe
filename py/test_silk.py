#!/usr/bin/env python3
"""silk.py 的自检 —— 不需要真的语音样本，也不解码，纯逻辑。

    python py/test_silk.py

真样本解码（可选，手上真有 QQ/微信语音再跑）：

    python py/silk.py --check 你的文件.amr
    python py/silk.py 你的文件.amr --out-dir out/
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from silk import SILK_MAGIC, is_silk, strip_lead  # noqa: E402

fail = []


def ok(label, cond):
    print(('  ✓ ' if cond else '  ✗ ') + label)
    if not cond:
        fail.append(label)


def temp_bytes(data: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix='silk-test-')
    with os.fdopen(fd, 'wb') as fh:
        fh.write(data)
    return path


BODY = bytes(range(64))

print('is_silk：')
ok('干净的头（#!SILK_V3）', is_silk(SILK_MAGIC + BODY))
ok('前面多一个 0x02（QQ 存下来就是这样）', is_silk(b'\x02' + SILK_MAGIC + BODY))
ok('前面多一个 0x03', is_silk(b'\x03' + SILK_MAGIC + BODY))
ok('空文件不算', not is_silk(b''))
ok('mp3 头不算', not is_silk(b'ID3\x03\x00\x00\x00' + BODY))
ok('短于 24 字节也不崩', not is_silk(b'#!SIL'))
ok('传 bytes 和传路径结果一致', is_silk(SILK_MAGIC + BODY) == is_silk(temp_bytes(SILK_MAGIC + BODY)))
ok('路径不存在时返回 False（不抛）', is_silk('/nonexistent/nope.amr') is False)

print('strip_lead：')
data, skipped = strip_lead(SILK_MAGIC + BODY)
ok('干净头：剥 0 个字节', skipped == 0 and data == SILK_MAGIC + BODY)
data, skipped = strip_lead(b'\x02' + SILK_MAGIC + BODY)
ok('0x02 前缀：剥 1 个字节', skipped == 1 and data.startswith(SILK_MAGIC))
ok('剥完的长度对得上', len(data) == len(SILK_MAGIC + BODY))
try:
    strip_lead(b'ID3\x03\x00\x00\x00' + BODY)
    ok('不是 silk 要抛 ValueError', False)
except ValueError:
    ok('不是 silk 要抛 ValueError', True)

print()
if fail:
    print(f'挂了 {len(fail)} 项：' + ' / '.join(fail))
    sys.exit(1)
print('全过')
