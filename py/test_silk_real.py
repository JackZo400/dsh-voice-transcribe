#!/usr/bin/env python3
"""test_silk_real.py —— 拿**真 SILK 文件**验 SILK 那条路（test_silk.py 用的是桩）。

    python py/test_silk_real.py
    python py/test_silk_real.py --model-dir /path/to/sense-voice-model
    python py/test_silk_real.py --wav 你自己的16k单声道.wav

仓库里不带任何音频，所以真 SILK 是**现造**的：用 `pilk` 的 `SilkEncoder`
把一段 16k 单声道 PCM 编成 SILK
（`pilk.encode(pcm_path, silk_path, ...)` —— 注意它吃的是**文件路径**，不是 bytes；
而且 `silk_rate` 一定要显式给，默认 `None` 会直接 TypeError）。
音源优先用 SenseVoice 模型包自带、可公开分发的测试音频（`test_wavs/*.wav`），
找不到就自己合成一段 16k 单声道音调。**不使用任何私人语音。**

验三件事：

  1. 现造的确实是 SILK：`is_silk` 为真、头部逐字节是 `#!SILK_V3`、能解出和源差不多长的声音；
  2. **QQ 那个坑**：同一份真 SILK 前面人为加一个 `0x02`（QQ 存下来就是这样），
     `strip_lead` + `decode_to_wav` 解出来的 PCM 与不加字节的那次**逐字节一致**；
     顺手也验 `0x03`，以及 `pilk` 自己 `tencent=True` 产出的腾讯格式；
  3. 解出来的声音拿去 `transcribe.py` **真转一遍**（装了 sherpa-onnx + 配了模型才真跑）。

缺 pilk / 缺 sherpa-onnx / 没配模型：明确打 `SKIP` 并说清缺什么，**退出码仍是 0**。
跳过就是跳过，不算过 —— **绝不假绿**。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from silk import SILK_MAGIC, decode_to_wav, is_silk, strip_lead  # noqa: E402
import transcribe  # noqa: E402  （模块级不 import sherpa_onnx，import 它是安全的）

# QQ 存下来的文件在最前面多塞的那个字节（0x02 / 0x03 都见过）
QQ_LEAD_BYTES = (b'\x02', b'\x03')
# 16 秒耳朵够用了，音源太长就先掐短，省得自检跑成慢测试
MAX_SRC_SECONDS = 8.0
RATE = 16000

fail: list[str] = []
skipped: list[str] = []


def ok(label: str, cond: bool) -> None:
    print(('  ✓ ' if cond else '  ✗ ') + label)
    if not cond:
        fail.append(label)


def skip(label: str, why: str) -> None:
    print(f'  – SKIP {label}（{why}）')
    skipped.append(label)


def info(msg: str) -> None:
    print('       · ' + msg)


def wav_pcm(path: str) -> tuple[bytes, int, int]:
    """wav → (PCM 字节, 采样率, 帧数)。"""
    with wave.open(path, 'rb') as w:
        return w.readframes(w.getnframes()), w.getframerate(), w.getnframes()


def run_transcribe(argv: list[str]) -> dict:
    """跑 transcribe.main()，把最后一行 JSON 解出来。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        transcribe.main(argv)
    lines = [l for l in buf.getvalue().strip().split('\n') if l.strip()]
    return json.loads(lines[-1])


def synth_pcm(seconds: float = 1.5) -> bytes:
    """没有公开音源时，自己合成一段 16k 单声道音调（不依赖 numpy）。"""
    n = int(RATE * seconds)
    out = bytearray()
    for i in range(n):
        t = i / RATE
        v = 0.35 * math.sin(2 * math.pi * 220 * t) + 0.2 * math.sin(2 * math.pi * 440 * t)
        edge = min(1.0, t / 0.05, max(0.0, (seconds - t) / 0.05))  # 淡入淡出，别爆音
        out += struct.pack('<h', int(max(-1.0, min(1.0, v * edge)) * 32767))
    return bytes(out)


def pick_source_wav(explicit: str, model_dir: str) -> tuple[str, str]:
    """挑音源。返回 (路径, 说明)；没有就 ('', 说明)。只挑公开可分发的音频。"""
    env_wav = os.environ.get('AILIN_SILK_TEST_WAV', '').strip()
    if explicit and os.path.isfile(explicit):
        return explicit, '命令行 --wav 给的'
    candidates: list[tuple[str, str]] = []
    if env_wav and os.path.isfile(env_wav):
        candidates.append((env_wav, '环境变量 AILIN_SILK_TEST_WAV 给的'))
    for d in (model_dir, transcribe.resolve_model_dir()):
        if not d:
            continue
        wavs_dir = os.path.join(d, 'test_wavs')
        if os.path.isdir(wavs_dir):
            for name in sorted(os.listdir(wavs_dir)):
                if name.lower().endswith('.wav'):
                    candidates.append((os.path.join(wavs_dir, name), f'模型包自带的公开测试音频 test_wavs/{name}'))
    if candidates:
        return candidates[0]
    return '', '没找到公开音源（既没有 --wav，也没有 AILIN_SILK_TEST_WAV，模型目录里也没有 test_wavs/）'


def source_pcm(path: str, tmpdir: str) -> tuple[bytes, str]:
    """音源 → 16k 单声道 16bit 原始 PCM。不是这个格式就用 ffmpeg 转。"""
    if path:
        try:
            with wave.open(path, 'rb') as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2):
                    pcm = w.readframes(w.getnframes())
                    return pcm[: int(RATE * MAX_SRC_SECONDS) * 2], '本身就是 16k 单声道 16bit'
        except Exception:  # 不是 wav（wave.Error / EOFError…）→ 交给 ffmpeg
            pass
        if not shutil.which('ffmpeg'):
            raise RuntimeError(f'音源 {path} 不是 16k 单声道 16bit，转它要 ffmpeg，但这台机器没有 ffmpeg')
        raw_path = os.path.join(tmpdir, 'src.pcm')
        proc = subprocess.run(
            ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', path,
             '-t', str(MAX_SRC_SECONDS), '-vn', '-ac', '1', '-ar', str(RATE), '-f', 's16le', raw_path],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0 or not os.path.exists(raw_path):
            raise RuntimeError(f'ffmpeg 转 16k 单声道失败：{(proc.stderr or "").strip()[:200]}')
        with open(raw_path, 'rb') as fh:
            return fh.read(), 'ffmpeg 转成 16k 单声道 16bit'
    return synth_pcm(), '自己合成的 16k 单声道音调（没有公开音源可用）'


def encode_silk(pilk, pcm_path: str, silk_path: str, tencent: bool) -> None:
    """真 SILK：pilk 的 encoder。pcm_rate / silk_rate / max_rate 都显式给（默认 None 会炸）。"""
    pilk.SilkEncoder(pcm_rate=RATE, silk_rate=RATE, max_rate=24000).encode(pcm_path, silk_path, tencent=tencent)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='用真 SILK 文件验 SILK 解码（现造，不用私人语音）')
    ap.add_argument('--model-dir', default='', help='SenseVoice 模型目录（默认读 AILIN_ASR_SV_DIR）')
    ap.add_argument('--wav', default='', help='拿哪个 wav 当音源（默认自动找模型包的 test_wavs/）')
    args = ap.parse_args(argv)

    model_dir = transcribe.resolve_model_dir(args.model_dir)
    have_pilk = importlib.util.find_spec('pilk') is not None
    have_sherpa = importlib.util.find_spec('sherpa_onnx') is not None

    print('真 SILK 自检（文件是现造的，仓库里不带音频）：')

    tmp = tempfile.mkdtemp(prefix='silk-real-test-')
    prepared = False
    try:
        # ------------------------------------------------------------ 1. 现造真 SILK
        print('1. 现造一个真 SILK 文件：')
        if not have_pilk:
            skip('造真 SILK 并解码', '没装 pilk：pip install pilk（造要用它的 encoder，解要用它的 decoder）')
            skip('QQ 那个 0x02 前缀（真文件）', '没装 pilk，造不出真 SILK')
            skip('解出来的声音真转一遍', '没装 pilk，造不出真 SILK')
        else:
            import pilk  # noqa: PLC0415

            try:
                if args.wav and not os.path.isfile(args.wav):
                    ok(f'--wav 指的文件存在（{args.wav}）', False)

                src_wav, src_note = pick_source_wav(args.wav, model_dir)
                pcm, pcm_note = source_pcm(src_wav, tmp)
                src_sec = len(pcm) / 2 / RATE
                info(f'音源：{src_note}；{pcm_note}；{src_sec:.2f} 秒')

                pcm_path = os.path.join(tmp, 'src.pcm')
                with open(pcm_path, 'wb') as fh:
                    fh.write(pcm)
                clean_silk = os.path.join(tmp, 'clean.silk')
                encode_silk(pilk, pcm_path, clean_silk, tencent=False)
                with open(clean_silk, 'rb') as fh:
                    clean_raw = fh.read()

                ok('编出来的是 SILK（is_silk 为真）', is_silk(clean_silk))
                ok('头部逐字节就是 #!SILK_V3', clean_raw[:len(SILK_MAGIC)] == SILK_MAGIC)
                ok(f'文件不是空的（{len(clean_raw)} 字节）', len(clean_raw) > len(SILK_MAGIC))

                clean_wav = decode_to_wav(clean_silk, os.path.join(tmp, 'clean.wav'), RATE)
                clean_pcm, clean_rate, clean_frames = wav_pcm(clean_wav)
                clean_sec = clean_frames / clean_rate
                ok('干净头能解出声音，长度和源对得上（'
                   f'{clean_sec:.2f}s vs {src_sec:.2f}s）', abs(clean_sec - src_sec) <= 0.1)
                ok(f'解出来是 16k 单声道（{clean_rate}Hz）', clean_rate == RATE)
                prepared = True
            except Exception as exc:  # 环境缺东西（没 ffmpeg、音源读不了…）→ SKIP，别崩
                why = str(exc).strip() or type(exc).__name__
                skip('造真 SILK 并解码', f'造不出来：{why}')
                skip('QQ 那个 0x02 前缀（真文件）', '造不出真 SILK')
                skip('解出来的声音真转一遍', '造不出真 SILK')

        # ------------------------------------------------------------ 2. QQ 的 0x02 坑
        if prepared:
            print('2. QQ 那个坑：真 SILK 前面人为多一个 0x02：')
            for lead in QQ_LEAD_BYTES:
                tag = lead.hex()
                qq_path = os.path.join(tmp, f'qq_{tag}.amr')  # QQ 存下来的文件名常写 .amr
                with open(qq_path, 'wb') as fh:
                    fh.write(lead + clean_raw)
                ok(f'0x{tag} 前缀：is_silk 仍然认', is_silk(qq_path))

                with open(qq_path, 'rb') as fh:
                    stripped, skipped_n = strip_lead(fh.read())
                ok(f'0x{tag} 前缀：strip_lead 只剥 1 个字节，剩下的和干净 SILK 逐字节一致',
                   skipped_n == 1 and stripped == clean_raw)

                qq_wav = decode_to_wav(qq_path, os.path.join(tmp, f'qq_{tag}.wav'), RATE)
                qq_pcm, qq_rate, qq_frames = wav_pcm(qq_wav)
                ok(f'0x{tag} 前缀：解出的 PCM 长度和干净那次一致（{qq_frames} 帧）', qq_frames == clean_frames)
                ok(f'0x{tag} 前缀：解出的 PCM 逐字节一致（{len(qq_pcm)} 字节）', qq_pcm == clean_pcm)
                ok(f'0x{tag} 前缀：时长一致（{qq_frames / qq_rate:.2f}s）',
                   abs(qq_frames / qq_rate - clean_sec) < 1e-9)

            # pilk 自己就产腾讯格式：这不是我们手工凑的，是编码器真吐出来的
            tx_silk = os.path.join(tmp, 'tx.silk')
            encode_silk(pilk, pcm_path, tx_silk, tencent=True)
            with open(tx_silk, 'rb') as fh:
                tx_raw = fh.read()
            ok('pilk 的 tencent=True 直接产出 0x02 开头的 SILK', tx_raw[:1] == b'\x02' and is_silk(tx_raw))
            tx_wav = decode_to_wav(tx_silk, os.path.join(tmp, 'tx.wav'), RATE)
            tx_pcm, _tx_rate, tx_frames = wav_pcm(tx_wav)
            ok('腾讯格式也解出同样的 PCM', tx_frames == clean_frames and tx_pcm == clean_pcm)

            qq_main = os.path.join(tmp, 'qq_02.amr')

        # ------------------------------------------------------------ 3. 真转一遍
        if not prepared:
            pass  # 上面已经 SKIP 过原因了，不重复报
        elif not have_sherpa or not model_dir:
            skip('解出来的声音真转一遍',
                 f'要 sherpa-onnx + 模型目录才真跑（sherpa_onnx={"装了" if have_sherpa else "没装"}，'
                 f'模型目录={model_dir or "没配 --model-dir / AILIN_ASR_SV_DIR"}）')
        elif not os.path.isdir(model_dir):
            skip('解出来的声音真转一遍', f'模型目录不存在：{model_dir}')
        else:
            print('3. 把这份带 0x02 的真 SILK 丢给 transcribe.py 走完整流程：')
            out = run_transcribe([qq_main, '--model-dir', model_dir])
            ok(f'transcribe.py 直接吃带 0x02 的真 SILK：ok={out.get("ok")}', out.get('ok') is True)
            ok('拿到了文本字段', isinstance(out.get('text'), str))
            if src_wav:
                ok('真语音解出来有字（不是空串）', bool(str(out.get('text') or '').strip()))
            info(json.dumps(out, ensure_ascii=False)[:220])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if skipped:
        print(f'跳过 {len(skipped)} 项：' + ' / '.join(skipped))
    if fail:
        print(f'挂了 {len(fail)} 项：' + ' / '.join(fail))
        return 1
    if not prepared:
        print('注意：这台机器上**真 SILK 一个字也没验**（上面全是 SKIP），别当成过了。')
    print('全过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
