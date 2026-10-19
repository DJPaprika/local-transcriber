@echo off
setlocal
cd /d "%~dp0.."

rem SHERPA_ONNX_NODE is located automatically by transcribe.py and the Node
rem bridge. Run "python tools\setup-sherpa.py" to check that it is found.
python web\serve.py
if errorlevel 1 pause
