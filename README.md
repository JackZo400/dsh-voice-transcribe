# dsh-voice-transcribe

[English](README.en.md) | 简体中文

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）用的**本地语音/视频转写**插件：
让 Agent「听得见」——把一个音频或视频文件交给它，它把里面的话转成文字。

引擎是 **SenseVoice**（sherpa-onnx 跑 int8 onnx）。**全程本地跑，不花 API 钱**，
约 0.1 秒 / 条，模型 239 MB。

---

## 为什么需要它

**它真正解决的问题，不是「转写」。**

调模型谁都会。麻烦的是**前面那一步**，尤其是中文 IM 生态里：

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

### 那用哪个引擎？

whisper 谁都会调，我们一开始也是它。同一条 1.7 秒的真实群语音：
whisper-medium 试了 5 组参数（beam 1/5 × 词表提示 / 口语 / 无提示），**全部**听岔；
SenseVoice 一次就对，和官方转写**一字不差**。速度也不是一个量级：约 0.1 秒 / 条（whisper 5-8 秒），
模型 239 MB int8（whisper medium 约 1.5 GB）。

所以这个插件里**只有 SenseVoice 一条路**：whisper 的开关、参数、依赖都删干净了，
不留半截开关让人以为还能切回去。数字都在下面「测试（实测数据）」里。

## 安装

**1. 装插件**

```bash
dsh plugin --profile web add github:JackZo400/dsh-voice-transcribe
```

**2. 装 Python 那半边**（装在 `pythonPath` 指的那个解释器里）

```bash
pip install -r py/requirements.txt      # sherpa-onnx + numpy + pilk
```

**3. 系统里要有 ffmpeg**（非 SILK 的音频/视频靠它转码）

**4. 下 SenseVoice 模型**（约 163 MB 的压缩包，解开是 239 MB 的 int8 模型）

```bash
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
tar xjf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
```

解出来的 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/` 就是模型目录，
里面要有 `model.int8.onnx` 和 `tokens.txt`。**放哪都行**，但要写进配置的 `modelDir`
（或环境变量 `AILIN_ASR_SV_DIR`）——代码里没有任何写死的路径。
这个模型认中文（普通话）/ 英文 / 日文 / 韩文 / 粤语五种语言，
想换别的版本看 [sherpa-onnx 的模型发布页](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)。

## 配置

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

**`modelDir`（或环境变量 `AILIN_ASR_SV_DIR`）**：模型目录在哪由你说了算，代码不猜路径。
没配的话转写会明确报「没给 SenseVoice 模型目录」，不会静默失败。

**`fix`（或环境变量 `AILIN_ASR_FIX`）**：专有名词纠错表。SenseVoice **没有词表接口**
（whisper 的 `initial_prompt` 那种），名字听岔了只能在结果上纠一道。格式 `错=对,错=对`：

```yaml
fix: '小张=张三,星海=星海项目'
```

表是**从左往右**替换的，短词会吃掉长词的前缀——**长的写前面**。
默认是**空表**：谁的名字谁自己填。

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
python py/transcribe.py zh.wav --language zh --model-dir /path/to/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
# {"file": "zh.wav", "dur": 5.59, "ok": true, "text": "开饭时间早上9点至下午5点。", "engine": "sensevoice", "lang": "zh", "sec": 1.0, "load_sec": 0.7, "total_sec": 1.0}
```

（上面这行输出是模型自带测试音频 `test_wavs/zh.wav` 的真实结果，你可以自己复现。）

`transcribe.py` 的输出是**一行 JSON**，方便被任何程序调用（dsh 插件就是这么接的）：
出错时退出码仍是 0，错误放在 JSON 的 `error` 字段里。

## 测试

```bash
python py/test_silk.py          # SILK 识别的逻辑（不需要真语音样本，秒级）
python py/test_transcribe.py    # 转写那条路：参数/纠错/懒加载；没模型会自动跳过
node test/plugin-selftest.mjs   # 插件接线（用假转写脚本，不需要模型）
```

`py/test_silk.py` 覆盖的就是那三个坑：干净头、`0x02`/`0x03` 前缀、空文件、
mp3 头、短文件不崩、不是 SILK 时抛错。

`py/test_transcribe.py` 不需要模型也不需要联网：先验参数解析和纠错表，
再拿桩替掉 `sherpa_onnx` 走一遍完整流程（顺便证明它真的是**用到才 import**），
最后**装了 sherpa-onnx 且配了模型目录才会**再跑一遍真模型。
没有模型、没装包的环境下它只打 `SKIP` 并说明原因——**不会假装通过**。

想看真效果，拿手上任意一条 QQ/微信语音跑 `python py/silk.py 你的文件`。

## 测试（实测数据）

本机（14 核 CPU、int8）真实语音样本：

| 项目 | 数字 |
| --- | --- |
| 模型体积 | 239 MB（int8 onnx；whisper medium 约 1.5 GB） |
| 模型加载 | 约 0.9 秒（一个进程只加载一次；whisper medium 约 1.6 秒） |
| 同一条 1.7 秒真实群语音 | 约 0.1 秒出字，一次就对（whisper-medium 试了 5 组参数全听岔） |
| SILK 解码 | 约 0.2 秒 / 条 |
| 3.6 秒真实语音 | 出字：「那样人太刷屏了 我直接给踢了不是说了吗」 |

那条 1.7 秒的语音，SenseVoice 和 QQ 官方转写**一字不差**；whisper-medium 的 5 组参数一组都没对。
**零 API 花费**，费的是 CPU。CPU 越强越快。

## 已知局限

- **只吃本地文件**。URL 请先自己下载——插件不替你做网络请求（少一个 SSRF 面）。
- **靠子进程**：模型崩了、超时了都杀得掉，但它不共享内存，每次调用有一次 Python 启动开销。
- **视频只转音轨**，不抽帧——画面理解是另一件事。
- **纯静音 / 纯音乐不保证是空串**：SenseVoice 的幻觉比 whisper 收敛得多，但实测给一段纯数字静音，
  它偶尔还是会冒一两个没意义的字。空的就如实是空的，但别把「有字」当成一定有话。
- **SenseVoice 没有词表接口**：专有名词只能靠 `fix` 那张表兜，得自己攒。
- **长音频靠切片**：超过 28 秒会在最安静的地方切片再拼；切点挑得再小心也偶尔会把词切开。
- **只认 5 种语言**：中文（普通话）/ 英文 / 日文 / 韩文 / 粤语，别的语言请另找模型。

## License

MIT © 2026 JackZo400

---

## English

→ Full English README: [README.en.md](README.en.md)
