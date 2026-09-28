#!/usr/bin/env python3
"""transcribe.py 的自检 —— **不需要真模型、不需要联网**，秒级。

    python py/test_transcribe.py

分几档，一档比一档「真」：

  1. 纯逻辑：参数解析、纠错表、模型目录解析（任何 python3 都能跑）
  2. SILK 输入 + 截断：不装任何东西也能验（解码那步换成桩）
  3. 假 sherpa_onnx：把 sherpa_onnx 换成桩，验「它真的只在要用的时候才 import」这条接线
  4. 真模型（可选）：装了 sherpa-onnx、且 AILIN_ASR_SV_DIR（或 --model-dir）指向真模型时，
     自己合成一段 wav 跑完整流程
  5. 缺包时的报错（可选）：机器上没装 sherpa-onnx 时，必须给出人话而不是崩

**绝不假绿**：跑不了的档只打 SKIP 并说明原因，永远不算「过」。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import transcribe  # noqa: E402  （模块级必须不 import sherpa_onnx —— 下面会验）

fail: list[str] = []
skipped: list[str] = []


def ok(label: str, cond: bool) -> None:
    print(('  ✓ ' if cond else '  ✗ ') + label)
    if not cond:
        fail.append(label)


def skip(label: str, why: str) -> None:
    print(f'  – SKIP {label}（{why}）')
    skipped.append(label)


def run_main(argv: list[str]) -> dict:
    """跑 transcribe.main()，把最后一行 JSON 解出来。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        transcribe.main(argv)
    lines = [l for l in buf.getvalue().strip().split('\n') if l.strip()]
    return json.loads(lines[-1])


def make_wav(path: str, seconds: float = 1.0, rate: int = 16000) -> str:
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b'\x00\x00' * int(rate * seconds))
    return path


# ---------------------------------------------------------------- 1. 纯逻辑
print('参数与纠错（不需要任何依赖）：')
opts = transcribe.parse_args(['a.silk', 'b.mp3', '--max-seconds', '9', '--language', 'zh', '--model-dir', '/m'])
ok('文件与选项都认', opts['files'] == ['a.silk', 'b.mp3'] and opts['max_seconds'] == 9 and opts['language'] == 'zh')
ok('--model-dir 传下去了', opts['model_dir'] == '/m')
ok('不认识的选项忽略、不当成文件', transcribe.parse_args(['a.silk', '--whatever'])['files'] == ['a.silk'])
ok('--fix 传下去了', transcribe.parse_args(['a.wav', '--fix', 'x=y'])['fix'] == 'x=y')

for flag in ('--model', '--prompt', '--no-prompt', '--device', '--compute'):
    try:
        transcribe.parse_args(['a.wav', flag, 'v'])
        ok(f'{flag} 应当直接报错（whisper 已删）', False)
    except ValueError as exc:
        ok(f'{flag} 报错并说明走 --model-dir：{exc}'[:60], '--model-dir' in str(exc))

ok('没给文件时退出码是 0 且 JSON 里说明白', run_main([]) == {'ok': False, 'error': '没给文件'})

ok('纠错表：默认空表不动文本', transcribe.fix_names('张三昨天来了', '') == '张三昨天来了')
ok('纠错表：按 错=对 替换', transcribe.fix_names('张三昨天来了', '张三=张三丰') == '张三丰昨天来了')
ok('纠错表：多个对、忽略坏项', transcribe.fix_names('甲乙', '甲=A,坏的,乙=B') == 'AB')
ok('纠错表：先写的先生效（长的写前面）', transcribe.fix_names('abc', 'ab=1,b=2') == '1c')
os.environ['AILIN_ASR_FIX'] = '环境=变量'
ok('纠错表：环境变量 AILIN_ASR_FIX 兜底', transcribe.fix_names('这是环境测试') == '这是变量测试')
del os.environ['AILIN_ASR_FIX']

prev = os.environ.pop('AILIN_ASR_SV_DIR', None)
ok('模型目录：没配时是空串', transcribe.resolve_model_dir() == '')
os.environ['AILIN_ASR_SV_DIR'] = '/from/env'
ok('模型目录：读环境变量', transcribe.resolve_model_dir() == '/from/env')
ok('模型目录：命令行优先于环境变量', transcribe.resolve_model_dir('/from/cli') == '/from/cli')
if prev is not None:
    os.environ['AILIN_ASR_SV_DIR'] = prev
else:
    del os.environ['AILIN_ASR_SV_DIR']

src = open(os.path.join(HERE, 'transcribe.py'), encoding='utf-8').read()
ok('源码里没有 faster_whisper 的痕迹', 'faster_whisper' not in src and 'WhisperModel' not in src)
ok('源码里没有写死的本机绝对路径', '/home/' not in src and '/Users/' not in src)
ok('模块级没有一上来就 import sherpa_onnx（懒加载）', 'sherpa_onnx' not in sys.modules)

# ---------------------------------------------------------------- 2. SILK + 截断
print('SILK 输入与截断（用桩替掉解码，不需要 pilk）：')
tmp = tempfile.mkdtemp(prefix='transcribe-test-')
try:
    silk_src = os.path.join(tmp, 'x.amr')
    with open(silk_src, 'wb') as fh:
        fh.write(b'\x02#!SILK_V3' + b'\x00' * 64)

    def fake_decode(s, dst=None, rate=16000):
        make_wav(dst, seconds=3.0, rate=rate)
        return dst

    real_is_silk, real_decode = transcribe.is_silk, transcribe.decode_to_wav
    transcribe.is_silk, transcribe.decode_to_wav = (lambda p: True), fake_decode
    try:
        wav, dur = transcribe.to_wav(silk_src, 1, tmp)
        with wave.open(wav) as w:
            frames = w.getnframes()
        ok(f'超过 max-seconds 会被截断（3.0s → {dur}s）', abs(dur - 1.0) < 0.01 and frames == 16000)
        wav2, dur2 = transcribe.to_wav(silk_src, 10, tmp)
        ok(f'没超过就原样留着（{dur2}s）', abs(dur2 - 3.0) < 0.01)
    finally:
        transcribe.is_silk, transcribe.decode_to_wav = real_is_silk, real_decode
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- 3. 假 sherpa_onnx
print('接线（把 sherpa_onnx 换成桩，走完整流程）：')
has_numpy = importlib.util.find_spec('numpy') is not None
has_ffmpeg = shutil.which('ffmpeg') is not None
if not has_numpy or not has_ffmpeg:
    skip('假 sherpa_onnx 走完整流程', f'需要 numpy + ffmpeg（numpy={has_numpy} ffmpeg={has_ffmpeg}）')
else:
    tmp = tempfile.mkdtemp(prefix='transcribe-test-')
    try:
        model_dir = os.path.join(tmp, 'model')
        os.makedirs(model_dir)
        for name in ('model.int8.onnx', 'tokens.txt'):
            with open(os.path.join(model_dir, name), 'wb') as fh:
                fh.write(b'dummy')
        wav_in = make_wav(os.path.join(tmp, 'in.wav'), 1.0)

        seen: dict = {}

        class StubStream:
            def __init__(self) -> None:
                self.result = types.SimpleNamespace(text='张三在测试')

            def accept_waveform(self, sample_rate, waveform):
                seen['rate'] = sample_rate
                seen['samples'] = len(waveform)

        class StubRecognizer:
            @staticmethod
            def from_sense_voice(**kw):
                seen.update(kw)
                return StubRecognizer()

            def create_stream(self):
                return StubStream()

            def decode_stream(self, stream):
                seen['decoded'] = True
                seen['text'] = stream.result.text

        stub = types.ModuleType('sherpa_onnx')
        stub.OfflineRecognizer = StubRecognizer
        saved = sys.modules.get('sherpa_onnx')
        sys.modules['sherpa_onnx'] = stub
        try:
            out = run_main([wav_in, '--model-dir', model_dir, '--fix', '张三=张三丰'])
        finally:
            if saved is None:
                del sys.modules['sherpa_onnx']
            else:
                sys.modules['sherpa_onnx'] = saved

        ok('整条路走通了', out.get('ok') is True and out.get('engine') == 'sensevoice')
        ok('纠错表在结果上生效了', out.get('text') == '张三丰在测试')
        ok('喂给模型的是 16k', seen.get('rate') == 16000 and seen.get('samples') == 16000)
        ok('模型路径是 model.int8.onnx', str(seen.get('model', '')).endswith('model.int8.onnx'))
        ok('use_itn 开着（要标点）', seen.get('use_itn') is True)
        ok('线程数是正数', int(seen.get('num_threads', 0)) >= 1)
        ok('JSON 里有耗时字段', 'total_sec' in out and 'load_sec' in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- 4. 真模型（可选）
print('真模型（有就跑，没有就明确跳过）：')
real_dir = transcribe.resolve_model_dir()
have_real = importlib.util.find_spec('sherpa_onnx') is not None and real_dir
if not have_real:
    skip('真模型跑一遍', f'sherpa_onnx 装了={"yes" if importlib.util.find_spec("sherpa_onnx") else "no"}，'
                          f'模型目录={real_dir or "没配 AILIN_ASR_SV_DIR"}')
elif not has_ffmpeg:
    skip('真模型跑一遍', '没有 ffmpeg')
else:
    tmp = tempfile.mkdtemp(prefix='transcribe-test-')
    try:
        wav_in = make_wav(os.path.join(tmp, 'silence.wav'), 1.0)
        try:
            out = run_main([wav_in, '--model-dir', real_dir])
            ok('真模型：跑通且返回文本字段', out.get('ok') is True and isinstance(out.get('text'), str))
            print(f'       · 模型 {real_dir}：{json.dumps(out, ensure_ascii=False)[:160]}')
        except Exception as exc:  # pragma: no cover - 取决于环境
            ok(f'真模型：跑通（实际报错：{exc}）', False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- 5. 缺包时报错
print('环境缺东西时的报错：')
if importlib.util.find_spec('sherpa_onnx') is not None:
    skip('没装 sherpa-onnx 的报错', '这台机器上装了 sherpa-onnx')
else:
    tmp = tempfile.mkdtemp(prefix='transcribe-test-')
    try:
        model_dir = os.path.join(tmp, 'model')
        os.makedirs(model_dir)
        for name in ('model.int8.onnx', 'tokens.txt'):
            with open(os.path.join(model_dir, name), 'wb') as fh:
                fh.write(b'dummy')
        wav_in = make_wav(os.path.join(tmp, 'in.wav'), 1.0)
        out = run_main([wav_in, '--model-dir', model_dir])
        ok(f'没装包时给的是人话：{out.get("error", "")[:80]}', out.get('ok') is False and 'sherpa-onnx' in str(out.get('error', '')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

print()
if skipped:
    print('跳过 ' + str(len(skipped)) + ' 项：' + ' / '.join(skipped))
if fail:
    print(f'挂了 {len(fail)} 项：' + ' / '.join(fail))
    sys.exit(1)
print('全过')
