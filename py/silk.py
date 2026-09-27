#!/usr/bin/env python3
"""silk.py — SILK 语音（QQ / 微信那种）解码成 WAV。

为什么需要这个东西
==================
QQ 里发出来的**真人语音其实就是 SILK**（文件头 `#!SILK_V3`），只不过文件名常写着
`.amr`。这件事有两个坑，踩过才知道：

1. **ffmpeg 大多解不了它。** 常见构建里根本没有 silk 解码器
   （`ffmpeg -decoders | grep silk` 输出是空的），直接喂过去只会得到
   `Invalid data found when processing input` —— 看着像文件坏了，其实是没人认这个格式。

2. **QQ 存下来的文件常常在最前面多一个字节**（`0x02` 或 `0x03`），
   不剥掉，连专门的 silk 解码库都会拒绝。

这个模块把这两步处理掉，再用 `pilk`（纯 Python、无编译依赖）解成 PCM，
封成标准 WAV。

用法
====
    python silk.py in.silk                      # → in.wav
    python silk.py in.amr --out-dir wav/        # 指定输出目录
    python silk.py a.silk b.amr --rate 24000
    python silk.py --check in.amr               # 只判断是不是 SILK，不解码

依赖
====
    pip install pilk

当库用
======
    from silk import is_silk, decode_to_wav

    if is_silk(path):
        decode_to_wav(path, 'out.wav')
"""
from __future__ import annotations

import argparse
import os
import sys
import wave

SILK_MAGIC = b'#!SILK_V3'
DEFAULT_RATE = 16000

__all__ = ['SILK_MAGIC', 'DEFAULT_RATE', 'is_silk', 'strip_lead', 'decode_to_wav']


def _head(source, n: int = 24) -> bytes:
    """取前 n 个字节。source 可以是路径或 bytes；读不到就返回空。"""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source[:n])
    try:
        with open(source, 'rb') as fh:
            return fh.read(n)
    except OSError:
        return b''


def is_silk(source) -> bool:
    """是不是 SILK。两种开头都认：正版 `#!SILK_V3`，以及前面多一个字节的。"""
    head = _head(source)
    return head.startswith(SILK_MAGIC) or head[1:].startswith(SILK_MAGIC)


def strip_lead(raw: bytes) -> tuple[bytes, int]:
    """剥掉 QQ 多塞的那个字节。返回 (数据, 剥掉几个字节)。"""
    if raw.startswith(SILK_MAGIC):
        return raw, 0
    if raw[1:].startswith(SILK_MAGIC):
        return raw[1:], 1
    raise ValueError('这不是 SILK：开头既不是 #!SILK_V3，也不是多一个字节的 #!SILK_V3')


def decode_to_wav(src: str, dst: str | None = None, rate: int = DEFAULT_RATE) -> str:
    """SILK 文件 → 单声道 WAV（默认 16k）。返回输出路径。

    需要 `pilk`（`pip install pilk`）——它是纯 Python 实现，不用编译任何东西。
    """
    try:
        import pilk
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError('解 SILK 需要 pilk：pip install pilk') from exc

    with open(src, 'rb') as fh:
        raw = fh.read()
    data, _skipped = strip_lead(raw)

    if dst is None:
        dst = os.path.splitext(src)[0] + '.wav'
    tmp_silk = f'{dst}.tmp.silk'
    tmp_pcm = f'{dst}.tmp.pcm'
    try:
        # pilk 只吃文件路径，所以先落一份剥好的 silk
        with open(tmp_silk, 'wb') as fh:
            fh.write(data)
        pilk.decode(tmp_silk, tmp_pcm, rate)
        with open(tmp_pcm, 'rb') as fh:
            pcm = fh.read()
        with wave.open(dst, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
    finally:
        for path in (tmp_silk, tmp_pcm):
            try:
                os.unlink(path)
            except OSError:
                pass
    return dst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='SILK（QQ / 微信语音）解码成 WAV')
    ap.add_argument('files', nargs='+', help='一个或多个 silk 文件（.amr/.silk 都可能）')
    ap.add_argument('--out-dir', default=None, help='输出目录（默认跟源文件同目录）')
    ap.add_argument('--rate', type=int, default=DEFAULT_RATE, help=f'采样率，默认 {DEFAULT_RATE}')
    ap.add_argument('--check', action='store_true', help='只判断是不是 SILK，不解码')
    args = ap.parse_args(argv)

    failed = 0
    for path in args.files:
        if args.check:
            print(f'{"silk" if is_silk(path) else "not-silk"}\t{path}')
            continue
        dst = None
        if args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
            dst = os.path.join(args.out_dir, os.path.splitext(os.path.basename(path))[0] + '.wav')
        try:
            out = decode_to_wav(path, dst, args.rate)
            size = os.path.getsize(out)
            print(f'{path}\t→\t{out}\t({size / 1024:.1f} KB)')
        except Exception as exc:
            print(f'{path}\t✗\t{exc}', file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
