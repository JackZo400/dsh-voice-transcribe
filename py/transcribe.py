#!/usr/bin/env python3
"""transcribe.py — 音频 / 视频里的说话 → 文字（全程本地，不花 API 钱）。

做三件事：
  1. 任何音视频 → 16k 单声道 wav。**SILK 走 silk.py**（ffmpeg 解不了它）。
  2. 本地 SenseVoice 转写（sherpa-onnx 跑 int8 onnx，CPU）。
  3. stdout 吐一行 JSON，方便被别的程序（比如 dsh 插件）调用。

为什么是 SenseVoice，不是 whisper
==================================
同一条 1.7 秒的真实群语音（跟官方转写一字不差的那条）：whisper-medium 试了 5 组参数
（beam 1/5 × 词表提示 / 口语 / 无提示）**全部**听岔；SenseVoice 一次就对。
速度也不是一个量级：约 0.1 秒 / 条（whisper 5-8 秒），模型 239 MB int8（whisper medium 约 1.5 GB）。
所以这里**只有 SenseVoice 一条路**：whisper 的开关、参数、依赖都删干净了，
不留半截开关让人以为还能切回去。

用法
====
    python transcribe.py <文件> [文件2 ...] [选项]

选项
====
    --model-dir DIR     SenseVoice 模型目录（要有 model.int8.onnx + tokens.txt）。
                        不写就读环境变量 AILIN_ASR_SV_DIR
    --max-seconds N     只转前 N 秒（默认 120；长的宁可截断，也不要把调用方卡死）
    --language LANG     auto / zh / en / ja / ko / yue（默认 auto）
    --fix '错=对,错=对' 专有名词纠错表；不写就读环境变量 AILIN_ASR_FIX（默认空表）
    --num-threads N     推理线程数（默认 CPU 核数，最多 8）

输出（stdout 一行 JSON）
========================
    单文件 {"ok":true,"file":"a.silk","text":"…","engine":"sensevoice","lang":"zh","dur":3.62,"sec":1.0,"total_sec":1.3}
    多文件 {"ok":true,"results":[…],"total_sec":1.3}
    出错   {"ok":false,"error":"…"}    ← 退出码仍然是 0，错误放在 JSON 里，调用方好解析

设计取舍（都是踩出来的）
========================
· **懒加载**：sherpa_onnx 在真要识别的时候才 import。以前这里一上来就把旧引擎那套包
  import 进模块，结果**卸掉旧引擎会把 SenseVoice 一起带死**（整个请求直接报「没装那个包」）。
  现在模块级不碰任何重依赖，脚本没装模型也能跑起来、能给出人话。
· **模型只加载一次**：一批文件共用一个识别器。加载要 ~1 秒，一条一条加载是白等。
· **先全部转码、再加载模型**：转码失败的文件不该让整批白等一个模型。
· **超过 28 秒就切片**，切点挑最后 3 秒里最安静的那一下 —— 硬切会把词劈成两半。
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

# SenseVoice 一次吃 30 秒以内；留点余量，超过就切片再拼
SV_CHUNK_SECONDS = 28.0
# SenseVoice 认的 5 种语言 + auto
SV_LANGUAGES = ('auto', 'zh', 'en', 'ja', 'ko', 'yue')

# —— 专有名词纠错表（默认故意留空）——
# 为什么需要：whisper 能靠 initial_prompt 塞词表把名字掰回来，**SenseVoice 没这个接口**。
# 名字被叫错比听不见还难受，所以只能在结果上纠一道。格式：错=对,错=对。
# 默认是空表：谁的名字谁自己填（`--fix` 或环境变量 AILIN_ASR_FIX），
# 公开仓库里不带任何人的私人称谓。看着像这样的表就行（示例是编的）：
#   AILIN_ASR_FIX='张三=张三丰,小明=王小明'
# 表是从左往右替换的，短词会吃掉长词的前缀 —— 长的写前面。
SV_FIX_DEFAULT = ''


def fix_names(text: str, table: str | None = None) -> str:
    """按纠错表把听岔的词换回来。表形如 `错=对,错=对`。"""
    if table is None:
        table = os.environ.get('AILIN_ASR_FIX', SV_FIX_DEFAULT)
    for pair in str(table or '').split(','):
        if '=' not in pair:
            continue
        bad, good = pair.split('=', 1)
        bad, good = bad.strip(), good.strip()
        if bad and bad != good:
            text = text.replace(bad, good)
    return text


def resolve_model_dir(cli_value: str | None = None) -> str:
    """模型目录：命令行 > 环境变量 AILIN_ASR_SV_DIR。路径从来不写死在代码里。"""
    return str(cli_value or os.environ.get('AILIN_ASR_SV_DIR') or '').strip()


def find_model_files(model_dir: str) -> tuple[str, str]:
    """在模型目录里找 (模型, tokens)。int8 优先，没有就用 float32 那份。"""
    if not model_dir:
        raise RuntimeError(
            '没给 SenseVoice 模型目录：用 --model-dir 或环境变量 AILIN_ASR_SV_DIR 指一下（下载方式见 README）'
        )
    tokens = os.path.join(model_dir, 'tokens.txt')
    for name in ('model.int8.onnx', 'model.onnx'):
        model = os.path.join(model_dir, name)
        if os.path.exists(model) and os.path.exists(tokens):
            return model, tokens
    raise RuntimeError(f'模型目录里没有 model.int8.onnx（或 model.onnx）+ tokens.txt：{model_dir}')


def load_recognizer(model_dir: str, language: str = 'auto', num_threads: int = 0):
    """加载 SenseVoice 识别器 —— **用到才 import sherpa_onnx**（见文件头的「懒加载」）。"""
    model, tokens = find_model_files(model_dir)
    lang = (language or 'auto').strip().lower()
    if lang not in SV_LANGUAGES:
        raise RuntimeError(f'不认识的语言 {language}（可用：{"/".join(SV_LANGUAGES)}）')
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise RuntimeError(f'没装 sherpa-onnx：{exc}（pip install -r py/requirements.txt）') from exc
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=model,
        tokens=tokens,
        num_threads=num_threads or min(8, os.cpu_count() or 4),
        use_itn=True,
        language=lang,
        debug=False,
    )


def _which(cmd: str) -> str:
    """ffmpeg / ffprobe 走 PATH，也允许用环境变量指绝对路径。"""
    if os.path.isabs(cmd):
        return cmd
    return shutil.which(cmd) or cmd


def _truncate_wav(path: str, max_seconds: float) -> None:
    """把 wav 截到前 max_seconds 秒（SILK 那条路解出来的是整条，得自己截）。"""
    with wave.open(path, 'rb') as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    keep = int(max_seconds * rate) * channels * width
    if len(frames) <= keep:
        return
    with wave.open(path, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames[:keep])


def to_wav(src: str, max_seconds: int, out_dir: str) -> tuple[str, float]:
    """任何音视频 → 16k 单声道 wav。返回 (wav 路径, 秒数)。"""
    if is_silk(src):
        dst = os.path.join(out_dir, os.path.basename(src) + '.silk.wav')
        decode_to_wav(src, dst, DEFAULT_RATE)
        _truncate_wav(dst, float(max_seconds))
        try:
            with wave.open(dst, 'rb') as w:
                dur = w.getnframes() / float(w.getframerate() or DEFAULT_RATE)
        except Exception:
            dur = 0.0
        return dst, round(dur, 2)

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


def quiet_cut(pcm, lo: int, hi: int, rate: int) -> int:
    """在 [lo, hi) 里挑最安静的那一下当切点 —— 别在词中间下刀。

    实据：59 秒那条按 28 秒硬切，正好把「隔壁」劈成「隔。」+「必」，
    是「有没有可能是你 tts 的问题」问出来的 —— 怀疑的是合成音，挖出来的是这把刀。
    """
    import numpy as np

    win = max(1, int(0.02 * rate))  # 20ms 一个窗口
    hop = max(1, win // 2)
    best_e, best_p = None, hi
    for p in range(lo, hi - win + 1, hop):
        e = float(np.abs(pcm[p:p + win]).mean())
        if best_e is None or e < best_e:
            best_e, best_p = e, p + win // 2
    return best_p


def sv_text(rec, wav: str, fix_table: str | None = None) -> str:
    """SenseVoice 转一条 wav（16k 单声道）；超过 SV_CHUNK_SECONDS 秒就切段拼起来。

    切点在最后 3 秒里挑最安静的一下（硬切会把词切两半，见 quiet_cut 的实据）。
    """
    import numpy as np

    with wave.open(wav) as wf:
        rate = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    step = int(SV_CHUNK_SECONDS * rate)
    out: list[str] = []
    total = len(pcm)
    i = 0
    while i < total:
        end = min(i + step, total)
        if end < total:
            end = quiet_cut(pcm, max(i + rate, end - 3 * rate), end, rate)
        chunk = pcm[i:end]
        i = end
        if len(chunk) < rate * 0.2:  # 不够 0.2 秒的尾巴丢掉，别喂出幻觉
            break
        stream = rec.create_stream()
        stream.accept_waveform(sample_rate=rate, waveform=chunk)
        rec.decode_stream(stream)
        text = (stream.result.text or '').strip()
        if text:
            out.append(text)
    return fix_names(''.join(out), fix_table)


def parse_args(argv: list[str]) -> dict:
    opts = {
        'files': [],
        'model_dir': '',
        'max_seconds': 120,
        'language': 'auto',
        'fix': None,          # None = 用环境变量 / 默认（空）表
        'num_threads': 0,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ('--model-dir', '--max-seconds', '--language', '--fix', '--num-threads'):
            i += 1
            if i >= len(argv):
                break
            key = {'model-dir': 'model_dir', 'max-seconds': 'max_seconds', 'num-threads': 'num_threads'}.get(
                arg[2:], arg[2:]
            )
            value = argv[i]
            if key in ('max_seconds', 'num_threads'):
                opts[key] = int(value)
            else:
                opts[key] = value
        elif arg in ('--model', '--prompt', '--no-prompt', '--device', '--compute'):
            # whisper 那条路已经删了（参数也一起删）—— 别让人以为还能切回去
            raise ValueError(f'{arg} 是 whisper 时代的参数，已经删了；SenseVoice 用 --model-dir 指模型目录')
        elif arg.startswith('--'):
            pass  # 不认识的选项直接忽略，方便调用方版本错配时也不炸
        else:
            opts['files'].append(arg)
        i += 1
    return opts


def main(argv: list[str] | None = None) -> int:
    try:
        opts = parse_args(list(sys.argv[1:] if argv is None else argv))
    except ValueError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 0

    if not opts['files']:
        print(json.dumps({'ok': False, 'error': '没给文件'}, ensure_ascii=False))
        return 0

    started = time.time()
    tmpdir = tempfile.mkdtemp(prefix='transcribe-')
    results: list[dict] = []
    try:
        # 第一趟：全部转码。失败的文件这里就出结果，不用白等模型加载。
        prepared: list[tuple[dict, str]] = []
        for path in opts['files']:
            item: dict = {'file': os.path.basename(path)}
            try:
                if not os.path.exists(path):
                    raise RuntimeError('文件不在了')
                wav, dur = to_wav(path, int(opts['max_seconds']), tmpdir)
                prepared.append((item, wav))
                item['dur'] = dur
            except Exception as exc:
                item.update({'ok': False, 'error': str(exc)[:200]})
            results.append(item)

        # 第二趟：识别器只加载一次，把这批全转完（懒加载：sherpa_onnx 到这儿才 import）
        if prepared:
            load_started = time.time()
            rec = None
            load_error = ''
            try:
                rec = load_recognizer(resolve_model_dir(opts['model_dir']), opts['language'], opts['num_threads'])
            except Exception as exc:
                load_error = str(exc)[:200]
            load_sec = round(time.time() - load_started, 1)
            for item, wav in prepared:
                try:
                    if rec is None:
                        raise RuntimeError(load_error or 'SenseVoice 起不来')
                    item.update({
                        'ok': True,
                        'text': sv_text(rec, wav, opts['fix']),
                        'engine': 'sensevoice',
                        'lang': (opts['language'] or 'auto'),
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
