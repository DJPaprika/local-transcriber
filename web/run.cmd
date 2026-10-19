@echo off
chcp 65001 >nul
cd /d "%~dp0.."

rem sherpa-onnx-node 没装在项目里时，指向本机的 DSH 内置副本
if "%SHERPA_ONNX_NODE%"=="" (
    set "SHERPA_ONNX_NODE=C:\Users\13414\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\node_modules\sherpa-onnx-node"
)

python web\serve.py
if errorlevel 1 pause
