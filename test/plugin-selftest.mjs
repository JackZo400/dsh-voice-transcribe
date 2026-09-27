// 插件级自检：不开 dsh，用假 ctx 把 apply() 跑一遍。
// 转写脚本换成 test/fake-asr.py，所以**不需要模型、不用等几十秒、不花一分钱**。
//   node test/plugin-selftest.mjs
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const FAKE_ASR = join(HERE, 'fake-asr.py')
const DIR = join(tmpdir(), 'dsh-voice-transcribe-selftest')
rmSync(DIR, { recursive: true, force: true })
mkdirSync(DIR, { recursive: true })
for (const name of ['a.silk', 'b.mp3', 'c.fail', 'd.fatal']) writeFileSync(join(DIR, name), 'not really media')

const registered = { tools: [], provides: [] }
const ctx = {
  logger: { info: () => {} },
  tools: {
    register: (def) => {
      registered.tools.push(def)
      return () => {}
    },
  },
  provide: (name, value) => {
    registered.provides.push({ name, value })
  },
}

apply(ctx, { scriptPath: FAKE_ASR, pythonPath: 'python3', maxFiles: 3 })

const fail = []
const ok = (label, condition) => {
  console.log(`${condition ? '✓' : '✗'} ${label}`)
  if (!condition) fail.push(label)
}

const tool = registered.tools.find((t) => t.name === 'transcribe_media')
const service = registered.provides.find((p) => p.name === 'voiceTranscribe')?.value

ok('注册了 transcribe_media 工具', Boolean(tool))
ok('暴露了 voiceTranscribe 服务', Boolean(service))
ok('服务认得出脚本在不在', service?.ready() === true)

// ---- 正常路径：脚本的 JSON 在最后一行，前面还有一行警告要跳过 ----
const value = await tool.execute({ paths: [join(DIR, 'a.silk'), join(DIR, 'b.mp3')] }, { signal: undefined })
ok(`一次转两个文件（拿到 ${value.results.length} 条）`, value.results.length === 2)
ok('文字是从脚本里出来的（说明参数真传下去了）', value.results.every((r) => r.ok && /假转写/.test(r.text || '')))
ok('文件名对得上', value.results.map((r) => r.file).join(',') === 'a.silk,b.mp3')
ok('时长带回来了', value.results.every((r) => r.duration === 1.5))
ok('语言带回来了', value.results.every((r) => r.language === 'zh'))

// ---- 输出声明与渲染 ----
ok('工具声明了输出 schema', Boolean(tool.output?.schema?.properties?.results))
const rendered = tool.output.render({}, value)
ok('render 出的是文本块', rendered[0]?.type === 'text')
ok('render 里有正文和文件名', rendered[0].text.includes('假转写') && rendered[0].text.includes('a.silk'))

// ---- 错误路径 ----
const failed = await tool.execute({ paths: [join(DIR, 'c.fail')] }, { signal: undefined })
ok(
  '单个文件失败只影响那一条（整批照样返回）',
  failed.results.length === 1 && failed.results[0].ok === false && Boolean(failed.results[0].error),
)

const fatal = await tool.execute({ paths: [join(DIR, 'd.fatal')] }, { signal: undefined })
ok('脚本级失败时抛异常也被兜住（如实报错，不炸整轮）', fatal.results.length === 1 && fatal.results[0].ok === false)

const missing = await tool.execute({ paths: [join(DIR, 'nope.silk')] }, { signal: undefined })
ok('文件不存在时直接说不在（不用去跑脚本）', missing.results[0]?.ok === false && /不存在/.test(missing.results[0]?.error || ''))

const empty = await tool.execute({ paths: [] }, { signal: undefined })
ok('没给路径时不炸', empty.results.length === 0 && Boolean(empty.note))

const overflow = await tool.execute(
  { paths: [join(DIR, 'a.silk'), join(DIR, 'b.mp3'), join(DIR, 'a.silk'), join(DIR, 'b.mp3')] },
  { signal: undefined },
)
ok('超过 maxFiles 时给出说明', Boolean(overflow.note) && /最多/.test(overflow.note || ''))

// ---- 服务出口（给别的插件用）----
const viaService = await service.transcribe([join(DIR, 'a.silk')])
ok('服务也能转（别的插件走这条路）', viaService.results.length === 1 && viaService.results[0].ok)

// ---- 脚本找不到时要给得出人话 ----
const broken = { tools: [], provides: [] }
apply(
  {
    logger: { info: () => {} },
    tools: { register: (def) => { broken.tools.push(def); return () => {} } },
    provide: (name, value) => { broken.provides.push({ name, value }) },
  },
  { scriptPath: '/nonexistent/asr.py', pythonPath: 'python3' },
)
const brokenValue = await broken.tools[0].execute({ paths: [join(DIR, 'a.silk')] }, {})
ok('脚本路径不对时给得出人话', brokenValue.results.length === 0 && /找不到转写脚本/.test(brokenValue.note || ''))

console.log(fail.length === 0 ? '\n全过' : `\n挂了 ${fail.length} 项：${fail.join(' / ')}`)
process.exit(fail.length === 0 ? 0 : 1)
