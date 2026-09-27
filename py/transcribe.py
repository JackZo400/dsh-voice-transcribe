#!/usr/bin/env python3
"""transcribe.py — 音频 / 视频里的说话 → 文字（全程本地，不花 API 钱）。

做三件事：
  1. 任何音视频 → 16k 单声道 wav。**SILK 走 silk.py**（ffmpeg 解不了它）。
  2. 本地 faster-whisper 转写（默认 CPU + int8）。
  3. stdout 吐一行 JSON，方便被别的程序（比如 dsh 插件）调用。

用法
====
    python transcribe.py <文件> [文件2 ...] [选项]

选项
====
    --model PATH|NAME   faster-whisper 模型（目录或模型名）。默认 medium
    --max-seconds N     只转前 N 秒（默认 120；长的宁可截断，也不要把调用方卡死）
    --language zh       语言（默认 auto）
    --prompt TEXT       词表提示：专有名词最容易听岔，把名字塞进去能明显掰回来
    --no-prompt         不用提示词
    --device cpu        设备
    --compute int8      精度

输出（stdout 一行 JSON）
========================
    单文件 {"ok":true,"file":"a.silk","text":"…","lang":"zh","dur":3.62,"sec":6.1,"total_sec":6.1}
    多文件 {"ok":true,"results":[…],"total_sec":12.3}
    出错   {"ok":false,"error":"…"}    ← 退出码仍然是 0，错误放在 JSON 里，调用方好解析

设计取舍（都是踩出来的）
========================
· **模型只加载一次**：一批文件共用一个实例。加载 medium 要好几秒，一条一条地加载是白等。
· **vad_filter 开着**：纯静音 / 纯音乐最容易被 whisper「幻觉」成「谢谢观看」这类字幕话。
· **beam_size=1**：够用，速度翻倍。聊天语音不是听写考试。
· **先全部转码、再加载模型**：转码失败的文件不该让整批白等一个模型。
· **转不出字就如实返回空**，绝不硬编。
· 视频不特殊处理：ffmpeg 会把音轨抽出来，画面那部分由调用方自己抽帧。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from silk import DEFAULT_RATE, decode_to_wav, is_silk  # noqa: E402

FFMPEG = os.environ.get('FFMPEG', 'ffmpeg')
FFPROBE = os.environ.get('FFPROBE', 'ffprobe')
DEFAULT_MODEL = os.environ.get('WHISPER_MODEL', 'medium')


def _which(cmd: str) -> str:
    """ffmpeg / ffprobe 走 PATH，也允许用环境变量指绝对路径。"""
    if os.path.isabs(cmd):
        return cmd
    found = shutil.which(cmd)
    return found or cmd


def to_wav(src: str, max_seconds: int, out_dir: str) -> tuple[str, float]:
    """任何音视频 → 16k 单声道 wav。返回 (wav 路径, 秒数)。"""
    if is_silk(src):
        dst = os.path.join(out_dir, os.path.basename(src) + '.wav')
        decode_to_wav(src, dst, DEFAULT_RATE)
        try:
            with wave.open(dst, 'rb') as w:
                dur = w.getnframes() / float(w.getframerate() or DEFAULT_RATE)
        except Exception:
            dur = 0.0
        return dst, round(min(dur, float(max_seconds)), 2)

    dst = os.path.join(out_dir, f'{os.path.basename(src)}.{max_seconds}.wav')
    cmd = [
        _which(FFMPEG), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
        '-i', src, '-t', str(max_seconds), '-vn', '-ac', '1', '-ar', '16000',
        '-f', 'wav', dst,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError(f'ffmpeg 转换失败：{(proc.stderr or "").strip()[:200]}')
    dur = 0.0
    try:
        probe = subprocess.run(
            [_which(FFPROBE), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', dst],
            capture_output=True, text=True, timeout=30,
        )
        dur = float((probe.stdout or '0').strip() or 0)
    except Exception:
        pass
    return dst, round(dur, 2)


def parse_args(argv: list[str]) -> dict:
    opts = {'files': [], 'model': DEFAULT_MODEL, 'max_seconds': 120, 'language': 'auto',
            'prompt': os.environ.get('WHISPER_PROMPT', ''), 'device': 'cpu', 'compute': 'int8'}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ('--model', '--max-seconds', '--language', '--prompt', '--device', '--compute'):
            i += 1
            if i >= len(argv):
                break
            key = {'max-seconds': 'max_seconds'}[arg[2:]] if arg == '--max-seconds' else arg[2:].replace('-', '_')
            opts[key] = int(argv[i]) if arg == '--max-seconds' else argv[i]
        elif arg == '--no-prompt':
            opts['prompt'] = ''
        elif arg.startswith('--'):
            pass  # 不认识的选项直接忽略，方便调用方版本错配时也不炸
        else:
            opts['files'].append(arg)
        i += 1
    return opts


def main(argv: list[str] | None = None) -> int:
    opts = parse_args(list(sys.argv[1:] if argv is None else argv))
    if not opts['files']:
        print(json.dumps({'ok': False, 'error': '没给文件'}, ensure_ascii=False))
        return 0

    started = time.time()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        print(json.dumps({'ok': False, 'error': f'没装 faster-whisper：{exc}（pip install faster-whisper）'}, ensure_ascii=False))
        return 0

    tmpdir = tempfile.mkdtemp(prefix='transcribe-')
    results: list[dict] = []
    try:
        # 第一趟：全部转码。失败的文件这里就出结果，不用白等模型加载。
        prepared: list[tuple[str, float]] = []
        for path in opts['files']:
            item: dict = {'file': os.path.basename(path)}
            try:
                if not os.path.exists(path):
                    raise RuntimeError('文件不在了')
                wav, dur = to_wav(path, int(opts['max_seconds']), tmpdir)
                prepared.append((wav, dur))
                item['dur'] = dur
            except Exception as exc:
                item.update({'ok': False, 'error': str(exc)[:200]})
            results.append(item)

        # 第二趟：模型只加载一次，把这批全转完
        if prepared:
            load_started = time.time()
            model = WhisperModel(opts['model'], device=opts['device'], compute_type=opts['compute'],
                                 cpu_threads=min(8, os.cpu_count() or 4))
            load_sec = round(time.time() - load_started, 1)
            language = None if opts['language'] in ('auto', '', None) else opts['language']
            for (wav, _dur), item in zip(prepared, [r for r in results if r.get('dur') is not None]):
                try:
                    segments, info = model.transcribe(
                        wav,
                        language=language,
                        beam_size=1,
                        vad_filter=True,
                        initial_prompt=opts['prompt'] or None,
                        vad_parameters={'min_silence_duration_ms': 500},
                    )
                    item.update({
                        'ok': True,
                        'text': ''.join(seg.text for seg in segments).strip(),
                        'lang': getattr(info, 'language', '') or '',
                        'sec': round(time.time() - load_started, 1),
                        'load_sec': load_sec,
                    })
                except Exception as exc:
                    item.update({'ok': False, 'error': str(exc)[:200]})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    payload = results[0] if len(results) == 1 else {'ok': True, 'results': results}
    payload['total_sec'] = round(time.time() - started, 1)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
