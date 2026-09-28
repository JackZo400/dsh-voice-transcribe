# dsh-voice-transcribe

[简体中文](README.md) | English

A **local speech/video transcription** plugin for
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (dsh):
it lets the Agent "hear" — hand it one audio or video file and it turns the speech
inside into text.

The engine is **SenseVoice** (sherpa-onnx running int8 onnx). **Everything runs locally —
zero API cost**, about 0.1 s per clip, 239 MB of model.

> **More mature options in the same space**: the desktop-microphone kind (for example
> [likhonmain/voice-input](https://github.com/likhonmain/voice-input)) is far more mature — if what you want
> is "talk at my computer and get text", install one of those.
> This one is for **voice messages other people send in QQ / WeChat**: SILK decoding, the extra byte at the
> front of QQ's files, and local SenseVoice transcription, end to end.
> One fact worth stating plainly: **the official QQ bot route does not need any of this** — the platform
> hands you the recognized text itself (`asr_refer_text`); only the personal-account route (OneBot: NapCat /
> Lagrange / SnowLuma) needs it.

---

## Why you need it

**The problem it really solves is not "transcription".**

Anyone can call a model. The hard part is **the step before it**, especially in the
Chinese IM ecosystem:

### Pitfall 1: voice messages from QQ / WeChat are SILK, not amr

A real human voice message in QQ has the file header `#!SILK_V3`, but the file name
often says `.amr`. You think feeding it to ffmpeg is enough — **it is not**.

### Pitfall 2: most ffmpeg builds have no SILK decoder at all

```bash
$ ffmpeg -decoders | grep silk
# （空）
$ ffmpeg -i voice.amr out.wav
# Invalid data found when processing input
```

It looks like a broken file, but really nobody recognizes the format. So you start
wondering whether the file was truncated, whether you need QQ's private library —
neither is true.

### Pitfall 3: files saved by QQ often carry one extra byte at the front

In hex:

```
00000000: 0223 2153 494c 4b5f        .#!SILK_
```

That `02` is stuffed in by QQ itself (`03` has been seen too). **If you do not strip it,
even a dedicated SILK decoding library will reject your file.**

All three are handled inside `py/silk.py` — it decodes to PCM with `pilk` (pure Python,
nothing to compile) and wraps a standard WAV itself. You can throw a plain `.amr` at it
as well; `is_silk()` will tell you what it actually is.

### Which engine?

Anybody can call whisper, and that is where we started. On one real 1.7-second group
voice message: whisper-medium tried five parameter sets (beam 1/5 x vocabulary hint /
colloquial / no hint) and got it wrong **every** time; SenseVoice got it right on the
first try, **character for character identical** to the official transcription. Speed is
not the same ballpark either: about 0.1 s per clip (whisper takes 5-8 s) and a 239 MB int8
model (whisper medium is about 1.5 GB).

So this plugin has **exactly one path: SenseVoice**. The whisper switches, parameters and
dependencies are all gone — no half-dead switch left behind to make you think you can
still go back. The numbers are in "Tests (measured data)" below.

## Install

**1. Install the plugin**

```bash
dsh plugin --profile web add github:JackZo400/dsh-voice-transcribe
```

**2. Install the Python half** (into the interpreter that `pythonPath` points at)

```bash
pip install -r py/requirements.txt      # sherpa-onnx + numpy + pilk
```

**Mirror warning: the Tsinghua mirror (`pypi.tuna.tsinghua.edu.cn`) does not carry `sherpa-onnx`**
and answers `No matching distribution found` (we hit it). If the install fails, switch mirrors:
`pip install -i https://mirrors.aliyun.com/pypi/simple -r py/requirements.txt`

**3. ffmpeg must be on the system** (non-SILK audio/video is transcoded with it)

**4. Download the SenseVoice model** (about 163 MB compressed, 239 MB int8 once unpacked)

```bash
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
tar xjf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
```

The unpacked `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/` directory is the
model directory; it must contain `model.int8.onnx` and `tokens.txt`. **Put it anywhere you
like**, but write the path into the `modelDir` config (or the `AILIN_ASR_SV_DIR`
environment variable) — nothing is hard-coded in the source. This model covers five
languages: Chinese (Mandarin) / English / Japanese / Korean / Cantonese. For other
versions see the [sherpa-onnx model releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models).

## Configuration

```yaml
- insert:
    - id: voice-transcribe
      name: dsh-voice-transcribe
      config:
        pythonPath: python3        # 装了上面那些包的解释器
        # scriptPath: ''           # 留空 = 用包内自带的 py/transcribe.py
        modelDir: /path/to/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
        maxSeconds: 120            # 只转前 N 秒
        language: auto             # auto / zh / en / ja / ko / yue
        fix: ''                    # 专有名词纠错表，见下
        maxFiles: 4                # 一次最多转几个（模型只加载一次）
```

**`modelDir` (or the `AILIN_ASR_SV_DIR` environment variable)**: where the model lives is
your call; the code never guesses a path. If it is missing, transcription fails loudly
with "no SenseVoice model directory" instead of failing silently.

**`fix` (or the `AILIN_ASR_FIX` environment variable)**: the proper-noun correction table.
SenseVoice has **no vocabulary interface** (the `initial_prompt` whisper had), so a
misheard name can only be fixed on the result. Format is `wrong=right,wrong=right`:

```yaml
fix: '小张=张三,星海=星海项目'
```

The table is applied **left to right**, and a short entry eats the prefix of a longer one —
**put the long ones first**. The default is an **empty table**: everyone fills in their
own names.

## Usage

### Inside dsh (the Agent calls it itself)

Once installed the Agent gains one more tool, `transcribe_media`:

```
把 /tmp/voice.amr 转成文字
```

It returns one result per file: `file` / `ok` / `text` / `language` / `duration`.

You can pass several files at once — **the model is loaded only once**, which is far
faster than transcribing them one by one.

Other plugins can use it by calling the service:

```js
const svc = ctx.get('voiceTranscribe')
const { results } = await svc.transcribe(['/tmp/voice.amr'])
```

### Standalone (no dsh needed)

If you only need SILK decoding:

```bash
python py/silk.py --check voice.amr        # 先看看是不是 SILK
python py/silk.py voice.amr --out-dir out/ # → out/voice.wav（16k 单声道）
```

To transcribe:

```bash
python py/transcribe.py zh.wav --language zh --model-dir /path/to/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
# {"file": "zh.wav", "dur": 5.59, "ok": true, "text": "开饭时间早上9点至下午5点。", "engine": "sensevoice", "lang": "zh", "sec": 1.0, "load_sec": 0.7, "total_sec": 1.0}
```

(That output line is a real run on `test_wavs/zh.wav` from the model archive — reproduce it
yourself.)

`transcribe.py` prints **a single line of JSON**, so any program can call it (that is how
the dsh plugin hooks in): on error the exit code is still 0, and the error sits in the
JSON `error` field.

## Tests

```bash
python py/test_silk.py          # SILK 识别的逻辑（不需要真语音样本，秒级）
python py/test_transcribe.py    # 转写那条路：参数/纠错/懒加载；没模型会自动跳过
python py/test_silk_real.py     # 用 pilk 现造真 SILK，验解码和 QQ 那个 0x02 前缀
node test/plugin-selftest.mjs   # 插件接线（用假转写脚本，不需要模型）
```

`py/test_silk.py` covers exactly those three pitfalls: clean header, `0x02`/`0x03`
prefix, empty file, mp3 header, short file does not crash, raises when it is not SILK.

`py/test_transcribe.py` needs no model and no network: first it checks argument parsing
and the correction table, then it stubs out `sherpa_onnx` and runs the whole flow (which
also proves the import really is lazy), and only **if sherpa-onnx is installed and a model
directory is configured** does it run the real model once. On a machine without the model
or the package it only prints `SKIP` with a reason — it **never fakes a pass**.

`py/test_silk_real.py` closes the gap the other two cannot reach: the repo ships no audio, so
it **builds** a real SILK file on the spot with the `pilk` encoder (the source is preferably
the public test clip shipped with the SenseVoice model archive, and a synthesized tone
otherwise), then checks the QQ pitfall — after a `0x02` byte is prepended by hand, the decoded
PCM is byte-for-byte identical to the run without it. When sherpa-onnx is installed and a
model directory is configured, it also transcribes the decoded audio for real. Missing
dependencies only produce `SKIP` with the reason, and the exit code stays 0.

To see the real thing, run `python py/silk.py 你的文件` on any QQ/WeChat voice message you
have at hand.

## Tests (measured data)

On this machine (14-core CPU, int8), real voice samples:

| Item | Number |
| --- | --- |
| Model size | 239 MB (int8 onnx; whisper medium is about 1.5 GB) |
| Model load | about 0.9 s (loaded once per process; whisper medium about 1.6 s) |
| The same 1.7-second group voice message | about 0.1 s to text, right on the first try (whisper-medium missed with all five parameter sets) |
| SILK decode | about 0.2 s each |
| 3.6-second voice (real sample) | output: 「那样人太刷屏了 我直接给踢了不是说了吗」 |

On that 1.7-second clip SenseVoice was **character for character identical** to the
official QQ transcription, while none of the five whisper-medium parameter sets got it
right. The Chinese output above means: "that guy spams too much, I just kicked him, did I
not say so".

**Zero API spend.** The stronger the CPU the faster.

## Known limitations

- **Local files only.** Download URLs yourself first — the plugin makes no network
  requests for you (one less SSRF surface).
- **It relies on a subprocess**: if the model crashes or times out you can still kill it,
  but it does not share memory, so every call pays a Python startup cost.
- **Video: only the audio track is transcribed**, no frames are extracted — understanding
  the picture is a different job.
- **Pure silence / pure music is not guaranteed to be an empty string**: SenseVoice
  hallucinates far less than whisper, but on a purely digital silence it still occasionally
  emits one or two meaningless characters. Empty stays honestly empty; just do not read
  "there is text" as "there was speech".
- **SenseVoice has no vocabulary interface**: proper nouns can only be caught by the `fix`
  table, and you have to build that yourself.
- **Long audio is chunked**: past 28 seconds it is cut at the quietest spot and stitched
  back together; however carefully the cut point is chosen, a word is occasionally split.
- **Only five languages**: Chinese (Mandarin) / English / Japanese / Korean / Cantonese —
  look for another model for anything else.

## License

MIT © 2026 JackZo400
