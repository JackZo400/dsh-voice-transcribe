/**
 * dsh-voice-transcribe —— 让 Agent「听得见」。
 *
 * 给 dsh 加一个工具：把一个本地音频/视频文件转成文字。整条链路都在本地跑，
 * 不花 API 钱，代价是 CPU 几秒到几十秒。
 *
 * 它解决的其实不是「转写」（whisper 谁都会调），而是**前面那一步**：
 *   · QQ / 微信发出来的真人语音是 **SILK**（文件名却写着 .amr）
 *   · 常见 ffmpeg 构建**没有 silk 解码器**，喂过去只会得到 "Invalid data found"
 *   · 而且 QQ 存下来的文件常常在最前面**多一个字节**（0x02 / 0x03），不剥掉连解码库都不认
 * 这三件事都在 `py/silk.py` 里处理掉了。
 *
 * 出口：
 *   · `transcribe_media` 工具 —— Agent 自己调
 *   · `voiceTranscribe` 服务 —— 别的插件也能调（比如某个通道插件收到语音附件时）
 *
 * 单独要 SILK 解码、不需要 dsh？直接用 `py/silk.py`，它是独立可跑的。
 * @module dsh-voice-transcribe
 */
import { existsSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { transcribeFiles } from './src/transcribe.js'

const HERE = dirname(fileURLToPath(import.meta.url))

/** Cordis 插件名。 */
export const name = 'dsh-voice-transcribe'

/** 需要的能力：注册工具。 */
export const inject = ['tools']

const DEFAULTS = {
  // 跑脚本的解释器 —— 得是装了 faster-whisper 的那个 python
  pythonPath: process.env.VOICE_TRANSCRIBE_PYTHON || 'python3',
  // 转写脚本位置。留空 = 用包内自带的 py/transcribe.py
  scriptPath: '',
  // faster-whisper 模型：模型名（会自动下载）或本地模型目录
  model: process.env.WHISPER_MODEL || 'medium',
  // 只转前 N 秒。再长的语音宁可截断，也不要把调用方卡在那里
  maxSeconds: 120,
  // 语言：zh / en …；auto = 自动判断
  language: 'auto',
  // 词表提示：专有名词最容易听岔，把名字塞进来能明显掰回来（别写成句子，会顺着编）
  prompt: '',
  device: 'cpu',
  compute: 'int8',
  // 一次最多转几个文件（模型只加载一次，批量比一个个转快得多）
  maxFiles: 4,
  timeoutMs: 180_000,
}

export function apply(ctx, rawConfig) {
  const config = { ...DEFAULTS, ...(rawConfig ?? {}) }
  const scriptPath = config.scriptPath ? resolve(String(config.scriptPath)) : join(HERE, 'py', 'transcribe.py')

  // 直接写 stderr，不走 ctx.logger —— cordis 的 logger 默认只挂一个内存环形缓冲，
  // info 既不落文件也不上屏，插件看着像根本没加载。
  const log = (...args) => {
    try {
      process.stderr.write(`${new Date().toISOString()} [dsh-voice-transcribe] ${args.join(' ')}\n`)
    } catch {
      // stderr 都没了就算了
    }
  }

  const resolveFile = (p) => {
    const s = String(p || '').trim()
    if (!s) return ''
    return isAbsolute(s) ? s : resolve(s)
  }

  /** 真干活：一批文件 → 结果数组。工具和服务都走它。 */
  const runFiles = async (paths, overrides = {}) => {
    const files = (Array.isArray(paths) ? paths : [paths]).map(resolveFile).filter(Boolean)
    if (files.length === 0) return { results: [], note: '没给文件路径。' }
    if (!existsSync(scriptPath)) {
      return { results: [], note: `找不到转写脚本：${scriptPath}（可用 scriptPath 配置指定）` }
    }
    const picked = files.slice(0, config.maxFiles)
    const note = files.length > picked.length ? `（一次最多转 ${config.maxFiles} 个，剩下的没转）` : undefined
    const results = await transcribeFiles(picked, {
      pythonPath: config.pythonPath,
      scriptPath,
      model: overrides.model || config.model,
      maxSeconds: overrides.maxSeconds || config.maxSeconds,
      language: overrides.language || config.language,
      prompt: config.prompt,
      device: config.device,
      compute: config.compute,
      timeoutMs: config.timeoutMs,
      signal: overrides.signal,
    })
    return { results, note }
  }

  // ---------------- 出口一：transcribe_media 工具 ----------------
  ctx.tools.register({
    name: 'transcribe_media',
    description:
      '把本地音频或视频文件转成文字（本地 whisper，不联网、不花钱）。支持 QQ/微信那种 SILK 语音，也支持 mp3/wav/m4a/amr 和视频的音轨。想知道"这段语音说了什么"就用它。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        paths: {
          type: 'array',
          items: { type: 'string' },
          description: '一个或多个本地文件路径（绝对路径，或相对工作目录）。视频会转它的音轨。',
        },
        language: { type: 'string', description: '语言代码，比如 zh、en；不给就自动判断。' },
        maxSeconds: { type: 'integer', description: '只转前多少秒，默认跟配置走（通常 120）。' },
      },
      required: ['paths'],
    },
    output: {
      // dsh 只收 JSON Schema 的一个子集：required 必须写成 object 上的字符串数组
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          results: {
            type: 'array',
            description: '每个文件一条结果。',
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                file: { type: 'string', description: '文件名。' },
                ok: { type: 'boolean', description: '这个文件转成功没有。' },
                text: { type: 'string', description: '转出来的文字（可能是空串：纯音乐或没人说话）。' },
                language: { type: 'string', description: '识别到的语言。' },
                duration: { type: 'number', description: '音频时长（秒）。' },
                error: { type: 'string', description: '失败原因。' },
              },
              required: ['file', 'ok'],
            },
          },
          note: { type: 'string', description: '需要我知道的额外说明。' },
        },
        required: ['results'],
      },
      render: (args, value) => {
        const lines = [`转写 ${value.results.length} 个文件：`]
        for (const r of value.results) {
          if (r.ok && r.text) lines.push('', `${r.file}（${r.duration ?? '?'}s）：${r.text}`)
          else if (r.ok) lines.push('', `${r.file}：没听出话（纯音乐或没人说话）`)
          else lines.push('', `${r.file}：没转成 —— ${r.error || '不知道为啥'}`)
        }
        if (value.note) lines.push('', value.note)
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const paths = Array.isArray(args?.paths) ? args.paths : args?.path ? [args.path] : []
      if (paths.length === 0) return { results: [], note: '没给文件路径。' }
      const missing = paths.map(resolveFile).filter((p) => p && !existsSync(p))
      if (missing.length === paths.length) {
        return { results: missing.map((file) => ({ file, ok: false, error: '文件不存在' })), note: '给的文件一个都不在。' }
      }
      try {
        const { results, note } = await runFiles(paths, {
          language: args?.language,
          maxSeconds: args?.maxSeconds,
          signal: exec?.signal,
        })
        return { results: results.map(normalize), ...(note ? { note } : {}) }
      } catch (error) {
        // 转写失败不能把整轮带下水：如实报错就够
        log('转写失败：', String(error))
        return { results: paths.map((p) => ({ file: basenameOf(p), ok: false, error: String(error.message || error).slice(0, 200) })) }
      }
    },
  })

  // ---------------- 出口二：给别的插件用的服务 ----------------
  try {
    ctx.provide('voiceTranscribe', {
      transcribe: (paths, options) => runFiles(paths, options),
      scriptPath,
      ready: () => existsSync(scriptPath),
    })
  } catch (error) {
    log('暴露 voiceTranscribe 失败（不影响工具本身）：', String(error))
  }

  log(`就绪：脚本=${scriptPath} 模型=${config.model} 解释器=${config.pythonPath}`)
}

/** 脚本给的是文件名；补一个兜底，别让 file 变成空串。 */
function basenameOf(p) {
  const s = String(p || '')
  return s.split('/').pop() || s
}

function normalize(item) {
  return {
    file: String(item?.file || ''),
    ok: Boolean(item?.ok),
    ...(item?.text ? { text: String(item.text) } : {}),
    ...(item?.lang ? { language: String(item.lang) } : {}),
    ...(typeof item?.dur === 'number' ? { duration: item.dur } : {}),
    ...(item?.error ? { error: String(item.error) } : {}),
  }
}
