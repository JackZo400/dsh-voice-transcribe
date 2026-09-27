# dsh-voice-transcribe

[English](README.en.md) | 简体中文

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）用的**本地语音/视频转写**插件：
让 Agent「听得见」——把一个音频或视频文件交给它，它把里面的话转成文字。

**全程本地跑，不花 API 钱**，代价是 CPU 几秒到几十秒。

---

## 为什么需要它

**它真正解决的问题，不是「转写」。**

whisper 谁都会调。麻烦的是**前面那一步**，尤其是中文 IM 生态里：

### 坑 1：QQ / 微信发出来的语音是 SILK，不是 amr

QQ 里真人语音的文件头是 `#!SILK_V3`，但文件名常写着 `.amr`。
你以为喂给 ffmpeg 就行——**不行**。

### 坑 2：大多数 ffmpeg 构建根本没有 SILK 解码器

```bash
$ ffmpeg -decoders | grep silk
# （空）
$ ffmpeg -i voice.amr out.wav
# Invalid data found when processing input
```

看着像文件坏了，其实是没人认这个格式。于是你开始怀疑是不是文件截断了、
是不是要用 QQ 的私有库——都不是。

### 坑 3：QQ 存下来的文件常常在最前面多一个字节

用十六进制看：

```
00000000: 0223 2153 494c 4b5f        .#!SILK_
```

那个 `02` 是 QQ 自己塞的（`03` 也见过）。**不剥掉它，连专门的 SILK 解码库都会拒绝你的文件。**

这三件事都在 `py/silk.py` 里处理掉了——用 `pilk`（纯 Python，不需要编译任何东西）解成 PCM，
自己封标准 WAV。顺手把一个 `.amr` 丢进去也能用，`is_silk()` 会告诉你它到底是什么。

## 安装

**1. 装插件**

```bash
dsh plugin --profile web add github:JackZo400/dsh-voice-transcribe
```

**2. 装 Python 那半边**（装在 `pythonPath` 指的那个解释器里）

```bash
pip install -r py/requirements.txt      # faster-whisper + pilk
```

**3. 系统里要有 ffmpeg**（非 SILK 的音频/视频靠它转码）

**4. 首次用会下模型**：默认 `medium` 大约 1.5 GB（faster-whisper 自动下载）。
嫌大就换成 `small` / `base`，越大越准也越慢。

## 配置

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

**`prompt` 怎么用**：专有名词最容易听岔。把你关心的人名/项目名用顿号连起来塞进去：

```yaml
prompt: '小王、李工、星海项目'
```

**别写成句子**——whisper 会顺着你的句子往下编，把没说的话也「补」出来。

## 用法

### 在 dsh 里（Agent 自己调）

装好之后 Agent 多一个工具 `transcribe_media`：

```
把 /tmp/voice.amr 转成文字
```

返回每个文件一条结果：`file` / `ok` / `text` / `language` / `duration`。

一次也可以给多个文件——**模型只加载一次**，批量比一个个转快得多。

别的插件想用，可以调服务：

```js
const svc = ctx.get('voiceTranscribe')
const { results } = await svc.transcribe(['/tmp/voice.amr'])
```

### 单独用（不需要 dsh）

只要 SILK 解码：

```bash
python py/silk.py --check voice.amr        # 先看看是不是 SILK
python py/silk.py voice.amr --out-dir out/ # → out/voice.wav（16k 单声道）
```

要转写：

```bash
python py/transcribe.py voice.amr --language zh
# {"ok": true, "file": "voice.amr", "text": "…", "lang": "zh", "dur": 1.92, "sec": 6.7}
```

`transcribe.py` 的输出是**一行 JSON**，方便被任何程序调用（dsh 插件就是这么接的）：
出错时退出码仍是 0，错误放在 JSON 的 `error` 字段里。

## 测试

```bash
python py/test_silk.py         # SILK 识别的逻辑（不需要真语音样本，秒级）
node test/plugin-selftest.mjs  # 插件接线（用假转写脚本，不需要模型）
```

`py/test_silk.py` 覆盖的就是那三个坑：干净头、`0x02`/`0x03` 前缀、空文件、
mp3 头、短文件不崩、不是 SILK 时抛错。

想看真效果，拿手上任意一条 QQ/微信语音跑 `python py/silk.py 你的文件`。

## 测试（实测数据）

本机（14 核 CPU、int8、`medium` 模型）真实 QQ 语音样本：

| 项目 | 数字 |
| --- | --- |
| SILK 解码 | 约 0.2 秒 / 条 |
| 模型加载 | 约 2.3 秒（一个进程只加载一次） |
| 7 秒语音转写 | 约 10 秒（加载时间摊进第一批） |
| 3.6 秒语音（真样本） | 出字：「那样人太刷屏了 我直接给踢了不是说了吗」 |

**零 API 花费**。CPU 越强越快；`small` 模型大约快一倍、准头差一点。

## 已知局限

- **只吃本地文件**。URL 请先自己下载——插件不替你做网络请求（少一个 SSRF 面）。
- **靠子进程**：模型崩了、超时了都杀得掉，但它不共享内存，每次调用有一次 Python 启动开销。
- **视频只转音轨**，不抽帧——画面理解是另一件事。
- **纯音乐/静音会转出空字符串**（开了 VAD 就是为了压住 whisper 的「谢谢观看」式幻觉）。
  空的就如实是空的，不硬编。
- **`medium` 中文效果够用但不算好**，要更准上 `large-v3`（慢很多）。

## License

MIT © 2026 JackZo400

---

## English

→ Full English README: [README.en.md](README.en.md)
