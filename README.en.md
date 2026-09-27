# dsh-voice-transcribe

[简体中文](README.md) | English

A **local speech/video transcription** plugin for
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (dsh):
it lets the Agent "hear" — hand it one audio or video file and it turns the speech
inside into text.

**Everything runs locally — zero API cost.** The price is a few to a few dozen seconds
of CPU.

---

## Why you need it

**The problem it really solves is not "transcription".**

Anyone can call whisper. The hard part is **the step before it**, especially in the
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

## Install

**1. Install the plugin**

```bash
dsh plugin --profile web add github:JackZo400/dsh-voice-transcribe
```

**2. Install the Python half** (into the interpreter that `pythonPath` points at)

```bash
pip install -r py/requirements.txt      # faster-whisper + pilk
```

**3. ffmpeg must be on the system** (non-SILK audio/video is transcoded with it)

**4. The first run downloads a model**: the default `medium` is about 1.5 GB (downloaded
automatically by faster-whisper). If that is too big, switch to `small` / `base`; bigger
means more accurate and slower.

## Configuration

```yaml
- insert:
    - id: voice-transcribe
      name: dsh-voice-transcribe
      config:
        pythonPath: python3        # 装了上面那些包的解释器
        # scriptPath: ''           # 留空 = 用包内自带的 py/transcribe.py
        model: medium              # tiny/base/small/medium/large-v3，或本地模型目录
        maxSeconds: 120            # 只转前 N 秒
        language: auto             # zh / en …；auto = 自动判断
        prompt: ''                 # 词表提示，见下
        maxFiles: 4                # 一次最多转几个（模型只加载一次）
```

**How to use `prompt`**: proper nouns are what gets misheard most. Join the person and
project names you care about with the enumeration comma used in Chinese and put them in
(the sample below is exactly the kind of value the correction table expects):

```yaml
prompt: '小王、李工、星海项目'
```

**Do not write it as a sentence** — whisper will follow your sentence onward and "fill
in" things that were never said.

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
python py/transcribe.py voice.amr --language zh
# {"ok": true, "file": "voice.amr", "text": "…", "lang": "zh", "dur": 1.92, "sec": 6.7}
```

`transcribe.py` prints **a single line of JSON**, so any program can call it (that is how
the dsh plugin hooks in): on error the exit code is still 0, and the error sits in the
JSON `error` field.

## Tests

```bash
python py/test_silk.py         # SILK 识别的逻辑（不需要真语音样本，秒级）
node test/plugin-selftest.mjs  # 插件接线（用假转写脚本，不需要模型）
```

`py/test_silk.py` covers exactly those three pitfalls: clean header, `0x02`/`0x03`
prefix, empty file, mp3 header, short file does not crash, raises when it is not SILK.

To see the real thing, run `python py/silk.py 你的文件` on any QQ/WeChat voice message you
have at hand.

## Tests (measured data)

On this machine (14-core CPU, int8, `medium` model), real QQ voice samples:

| Item | Number |
| --- | --- |
| SILK decode | about 0.2 s each |
| Model load | about 2.3 s (loaded only once per process) |
| 7-second voice transcription | about 10 s (load time spread across the first batch) |
| 3.6-second voice (real sample) | output: 「那样人太刷屏了 我直接给踢了不是说了吗」 (Chinese, translated below) |

The Chinese transcription result above means: "that guy spams too much, I just kicked
him, did I not say so".

**Zero API spend.** The stronger the CPU the faster; the `small` model is about twice as
fast with a little less accuracy.

## Known limitations

- **Local files only.** Download URLs yourself first — the plugin makes no network
  requests for you (one less SSRF surface).
- **It relies on a subprocess**: if the model crashes or times out you can still kill it,
  but it does not share memory, so every call pays a Python startup cost.
- **Video: only the audio track is transcribed**, no frames are extracted — understanding
  the picture is a different job.
- **Pure music / silence comes out as an empty string** (VAD is enabled precisely to hold
  down whisper's "thanks for watching" style hallucinations). Empty stays honestly empty;
  nothing is invented.
- **`medium` on Chinese is usable but not good**, go to `large-v3` for more accuracy
  (much slower).

## License

MIT © 2026 JackZo400
