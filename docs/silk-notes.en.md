# QQ / WeChat voice messages (SILK): notes from the field

[简体中文](silk-notes.md) | English

None of this was designed on paper — it was learned the hard way, **on a real personal-account QQ bot**.
It is written down here so the next person does not have to repeat it.

---

## Scope first (so you do not waste your time)

- **Official QQ bots** (the QR-code / AppID + AppSecret route): voice attachments **already ship the
  recognized text** (`asr_refer_text`). Use that; you do **not** need to decode anything, and you do not
  need most of this note.
- **Personal-account route** (OneBot: NapCat / Lagrange / SnowLuma — your own QQ account as the bot):
  what you receive is the **raw voice file**, and the platform gives you no text. **Without decoding you
  are deaf** — someone sends a voice message in the group and your agent can only answer "I could not
  make it out".

`py/silk.py` + `py/transcribe.py` in this repository are for the **latter** case.

## Three traps you will hit

**Trap 1: it pretends to be amr.**
The file is named `.amr`, but the actual header is `#!SILK_V3`. SILK is the codec Tencent uses for voice
in QQ / WeChat — it is not amr at all. The name lies; the content does not.

**Trap 2: most ffmpeg builds have no SILK decoder.**
Hand the file to ffmpeg and you get exactly this:

```bash
$ ffmpeg -decoders | grep silk
# （空）
$ ffmpeg -i voice.amr out.wav
# Invalid data found when processing input
```

It looks like a corrupt file. It is not: nobody recognizes the format. You then start suspecting a
truncated download or a proprietary Tencent library — neither is the problem.

**Trap 3: QQ's saved file often carries one extra leading byte** (`0x02` / `0x03` have both been seen).
In hex it looks like this:

```
00000000: 0223 2153 494c 4b5f        .#!SILK_
```

Leave that byte in place and **a correct SILK decoder will still reject the file**. `strip_lead()` in
`py/silk.py` exists for exactly this.

## What actually works

```
SILK file (possibly with one extra leading byte)
   └─ strip_lead() removes that byte
   └─ pilk decodes → PCM (16 kHz mono)
   └─ local ASR turns it into text
```

- `pilk`: a pure-Python implementation — **nothing to compile**.
- For ASR we use **SenseVoice** (sherpa-onnx running int8 onnx, 239 MB of model, about **0.1 s per
  clip**, entirely local, zero API cost).

## How to write a self-test that means something

The SILK path is the easiest place to end up with a **fake green**: with no real SILK file at hand, the
tests only exercise stubs and prove nothing. The way out is to **build a real SILK file on the fly with
pilk's own encoder** (audio source: publicly distributable sample audio — **no private voice needed**),
then:

1. assert it really is SILK: `is_silk()` is true, the header is byte-for-byte `#!SILK_V3`, and the decoded
   duration matches the source;
2. assert **trap 3**: prepend a single `0x02` by hand; what `strip_lead()` + decode produces must be
   **byte-for-byte identical** to the run without it;
3. feed the `0x02`-prefixed file straight into `transcribe.py` and get text through the whole pipeline.

See `py/test_silk_real.py`. Without the dependencies installed it prints an explicit `SKIP` and says
"not one byte of real SILK was verified" — **skipping is not passing**.

## Two smaller traps

- `pilk.SilkEncoder` needs `silk_rate` **passed explicitly** (the default `None` raises `TypeError`), and
  `encode()` takes **file paths**, not bytes.
- When installing dependencies, the **Tsinghua mirror (pypi.tuna) does not carry `sherpa-onnx`** — switch
  to the Aliyun or the official index.

## Boundaries (better stated than pretended away)

- This only covers the **personal-account route**. Official bots should just use the platform's own
  recognized text.
- SILK decoding only gets you the **audio**. Misheard words are a separate problem: SenseVoice has **no
  vocabulary interface** (no whisper-style prompt to feed names), so proper nouns have to go through a
  correction table applied to the result (`--fix` / `AILIN_ASR_FIX`).
