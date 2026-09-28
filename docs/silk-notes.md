# QQ / 微信语音（SILK）踩坑笔记

[English](silk-notes.en.md) | 简体中文

这套东西不是设计出来的，是**在真跑的个人号 QQ 机器人上踩出来的**。写在这里，省得下一个人再踩一遍。

---

## 先说适用范围（免得白折腾）

- **官方 QQ 机器人**（扫码 / AppID + AppSecret 那条路）：语音消息的附件里**平台直接带了识别文字**
  （`asr_refer_text`），照着用就行，**不需要**自己解码，也不需要这份笔记剩下的大部分内容。
- **个人号路线**（OneBot：NapCat / Lagrange / SnowLuma 这类，用你自己的 QQ 号当机器人）：
  你收到的是**原始语音文件**，平台不给文字。**不解就是聋的**——有人在群里发一条语音，
  你的 agent 只能回一句「听不清」。

本仓库的 `py/silk.py` + `py/transcribe.py` 是给**后者**用的。

## 三个坑，一个都躲不过

**坑 1：它假装自己是 amr。**
文件名写着 `.amr`，真正的文件头却是 `#!SILK_V3`。SILK 是腾讯在 QQ / 微信里用的语音编码，跟 amr 不是一个东西——
名字骗人，内容才是真的。

**坑 2：ffmpeg 基本都不带 SILK 解码器。**
直接丢给 ffmpeg，只会得到一句：

```bash
$ ffmpeg -decoders | grep silk
# （空）
$ ffmpeg -i voice.amr out.wav
# Invalid data found when processing input
```

看着像文件坏了，其实是没人认这个格式。于是你会开始怀疑文件被截断、怀疑要装腾讯的私有库——都不是。

**坑 3：QQ 存下来的文件最前面常常多一个字节**（`0x02` / `0x03` 都见过）。
用十六进制看长这样：

```
00000000: 0223 2153 494c 4b5f        .#!SILK_
```

不把这一个字节剜掉，**正经的 SILK 解码器也会拒收**。`py/silk.py` 的 `strip_lead()` 处理的就是它。

## 能跑的做法

```
SILK 文件（可能多一个字节）
   └─ strip_lead() 剜掉那个字节
   └─ pilk 解码 → PCM（16 kHz 单声道）
   └─ 本地 ASR 转文字
```

- `pilk`：纯 Python 实现，**不用编译任何东西**。
- ASR 我们用的是 **SenseVoice**（sherpa-onnx 跑 int8 onnx，模型 239 MB，约 **0.1 秒/条**，全程本地、零 API 钱）。

## 自检怎么写才算数

SILK 这条分支最容易变成"假绿"——手边没有真 SILK 文件时，测试只有桩，证明不了任何事。做法是
**用 pilk 自带的编码器现造一个真 SILK**（音源用公开可分发的音频，**不需要任何私人语音**），然后：

1. 断言它是真 SILK：`is_silk()` 为真、头部逐字节是 `#!SILK_V3`、解码时长和源对得上；
2. 断言**坑 3**：手工在最前面加一个 `0x02`，`strip_lead()` + 解码出来的 PCM 必须和没加那次**逐字节一致**；
3. 把带 `0x02` 的文件直接喂 `transcribe.py`，走完整流程拿到文本。

见 `py/test_silk_real.py`。没装依赖时它会明确打 `SKIP` 并喊一句"真 SILK 一个字也没验"——**跳过不等于通过**。

## 顺带两个小坑

- `pilk.SilkEncoder` 的 `silk_rate` **必须显式给**（默认 `None` 会直接 `TypeError`）；`encode()` 收的是**文件路径**，不是 bytes。
- 装依赖时**清华源（pypi.tuna）没有 `sherpa-onnx`** 这个包，换阿里源或官方源。

## 边界（写清楚比假装全能强）

- 这份东西只解决**个人号路线**的语音。官方机器人那条路用平台自带的识别文字更省事。
- SILK 解码只解决"**听见**"。听岔了怎么办是另一个问题：SenseVoice **没有词表接口**
  （没法像 whisper 那样用 prompt 塞名字），专有名词只能自己攒一张纠错表在结果上纠一道（`--fix` / `AILIN_ASR_FIX`）。
