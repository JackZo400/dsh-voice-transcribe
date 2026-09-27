#!/usr/bin/env python3
"""自检用的假转写器：不加载模型、不读音频，按参数吐固定 JSON。

它模拟真实脚本的三个外部行为：
  1. 会在 stdout 前面打别的行（真实脚本可能有 python 警告）→ 调用方必须取**最后一行**
  2. 文件名以 `.fail` 结尾 → 那条结果 `ok:false`，但**整批仍然是 ok:true**
     （真实的 transcribe.py 就是这样：一个坏文件不该让别的文件白转）
  3. 文件名以 `.fatal` 结尾 → 整个脚本级失败 `{ok:false,error}`（比如没装 faster-whisper）

真转写请用 py/transcribe.py。
"""
import json
import os
import sys

VALUED = {'--model', '--max-seconds', '--language', '--prompt', '--device', '--compute'}

files = []
args = sys.argv[1:]
i = 0
while i < len(args):
    arg = args[i]
    if arg in VALUED:
        i += 2
        continue
    if arg.startswith('--'):
        i += 1
        continue
    files.append(arg)
    i += 1

print('warning: 这是自检用的假脚本，别当真')

if any(f.endswith('.fatal') for f in files):
    print(json.dumps({'ok': False, 'error': '假的脚本级失败（比如没装 faster-whisper）'}, ensure_ascii=False))
else:
    results = []
    for f in files:
        name = os.path.basename(f)
        if name.endswith('.fail'):
            results.append({'file': name, 'ok': False, 'error': '假的失败'})
        else:
            results.append({'file': name, 'ok': True, 'text': f'假转写：{name}', 'lang': 'zh', 'dur': 1.5})
    print(json.dumps({'ok': True, 'results': results}, ensure_ascii=False))
