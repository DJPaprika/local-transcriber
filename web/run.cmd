@echo off
setlocal
cd /d "%~dp0.."

if "%SHERPA_ONNX_NODE%"=="" set "SHERPA_ONNX_NODE=C:\Users\13414\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\node_modules\sherpa-onnx-node"

python web\serve.py
if errorlevel 1 pause
