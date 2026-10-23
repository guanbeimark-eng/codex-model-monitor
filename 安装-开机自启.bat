@echo off
chcp 65001 >nul
setlocal

set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
  echo 没找到 Python。请先安装 Python 3（安装时勾选 Add to PATH），再双击本文件。
  pause
  exit /b 1
)

set "SCRIPT=%~dp0codex_model_monitor.pyw"
if not exist "%SCRIPT%" (
  echo 没找到 codex_model_monitor.pyw，请把它和本文件放在同一个文件夹。
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w=New-Object -ComObject WScript.Shell;" ^
  "foreach($d in @([Environment]::GetFolderPath('Startup'),[Environment]::GetFolderPath('Desktop'))){" ^
  "$s=$w.CreateShortcut((Join-Path $d 'Codex Models.lnk'));" ^
  "$s.TargetPath=$env:PYW; $s.Arguments=[char]34+$env:SCRIPT+[char]34; $s.WorkingDirectory=(Split-Path $env:SCRIPT); $s.Save()}"

if errorlevel 1 (
  echo 创建快捷方式失败。
  pause
  exit /b 1
)

start "" "%PYW%" "%SCRIPT%"
echo 完成：已加入开机自启，桌面也有 "Codex Models" 图标，小窗已打开。
timeout /t 3 >nul
