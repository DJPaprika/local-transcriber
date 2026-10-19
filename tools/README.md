# tools/

两个「环境体检 + 一键准备」脚本。都不是运行时的依赖，只在第一次配置时用。

## setup-sherpa.py

检查 SenseVoice 引擎要的三样东西：node、`sherpa-onnx-node`、模型文件。

```bash
python tools/setup-sherpa.py            # 只检查
python tools/setup-sherpa.py --persist  # 把 SHERPA_ONNX_NODE 写进用户环境变量
python tools/setup-sherpa.py --print-path  # 只打印探测到的路径（给脚本用）
```

探测顺序和 `transcribe.py` / `bridge/sv-bridge.js` 保持一致：

1. 环境变量 `SHERPA_ONNX_NODE`
2. `<项目>/node_modules/sherpa-onnx-node`
3. `<项目>/bridge/node_modules/sherpa-onnx-node`
4. `%APPDATA%\npm\node_modules\sherpa-onnx-node`（全局 npm 根）
5. `%APPDATA%\npm\node_modules\@deepseek-ai\dsh\node_modules\sherpa-onnx-node`
6. `$DSH_HOME/node_modules/sherpa-onnx-node`

所以「npm 装到项目里」「npm -g 装」「被别的工具装在它自己的依赖树里」三种情况
都能直接跑，不用手动设环境变量。`--persist` 只是把它固化下来，省得依赖探测。

## prepare-llm.py

检查 llama.cpp 的 `llama-server` 和 Qwen GGUF 模型，缺了就下。

```bash
python tools/prepare-llm.py             # 检查
python tools/prepare-llm.py --download  # 下载（约 4.5 GB）
python tools/prepare-llm.py --download --proxy http://127.0.0.1:7890
```

| 东西 | 来源 | 默认落点 |
|---|---|---|
| llama-server | GitHub release 的 `llama-*-bin-win-vulkan-x64.zip` | `D:\toolchains\llama.cpp\` |
| Qwen2.5-7B-Instruct q4_k_m | Hugging Face 的分片 GGUF（2 个） | `D:\workspace\tools\llm\models\` |

默认路径读的是和 `translate.py` 同一组环境变量（`LLAMA_SERVER_EXE`、`LLM_MODEL`），
也可以直接改 `translate.py` 文件头部的常量。

下载器是标准库 `urllib`，带进度条、失败重试三次、先写 `.part` 再改名；
中途断掉不会留下半个「看起来正常」的模型文件。

## 为什么不做成 pip 包

项目原则是零 pip 依赖。这两个脚本只用标准库，跑完就结束，不需要常驻服务。
