// 调 Python 转写脚本的薄壳：拼参数 → 跑子进程 → 取最后一行 JSON。
//
// 为什么是"子进程 + 一行 JSON"而不是把 python 嵌进来：
//   · faster-whisper 是 Python 生态里最省事的那条路（模型加载、VAD、int8 都是现成的）
//   · 分成两个进程，模型崩溃 / 超时都杀得掉，不会把宿主一起带走
//   · 那一行 JSON 是唯一的契约，脚本可以单独用、单独测
import { execFile } from 'node:child_process'

/** 跑一条命令，拿 stdout。超时、非零退出、被 abort 都算失败。 */
export function run(cmd, args, { timeoutMs, signal, maxBuffer = 8 * 1024 * 1024 } = {}) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { timeout: timeoutMs, maxBuffer, signal }, (err, stdout, stderr) => {
      if (err) {
        const detail = String(stderr || '').trim() || err.message
        reject(new Error(detail.slice(0, 300)))
      } else {
        resolve(String(stdout || ''))
      }
    })
  })
}

/**
 * 从脚本输出里解出 JSON。
 * 脚本正常只吐一行；但可能夹着 python 的警告，所以取**最后一个非空行**。
 */
export function parseOutput(stdout) {
  const lines = String(stdout || '').trim().split('\n').filter((l) => l.trim() !== '')
  const last = lines[lines.length - 1] || '{}'
  return JSON.parse(last)
}

/**
 * 一批文件 → 转写结果数组。
 * 脚本契约：`{ok:true, results:[...]}` 或 `{ok:true, file, text, ...}`（单文件）。
 */
export async function transcribeFiles(files, opts) {
  const args = [
    ...files,
    '--model', String(opts.model),
    '--max-seconds', String(opts.maxSeconds),
    '--language', String(opts.language),
    '--device', String(opts.device),
    '--compute', String(opts.compute),
  ]
  if (opts.prompt) args.push('--prompt', String(opts.prompt))
  const stdout = await run(opts.pythonPath, [opts.scriptPath, ...args], {
    timeoutMs: opts.timeoutMs,
    signal: opts.signal,
  })
  const json = parseOutput(stdout)
  if (json?.ok === false) throw new Error(json.error || '转写失败')
  return Array.isArray(json?.results) ? json.results : [json]
}
