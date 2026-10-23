# Codex 模型监测小猫 🐱

Windows 桌面置顶小窗，实时显示 Codex 当前账号哪些模型能用。小猫的表情代表整体状态。只用 Python 自带库，无需安装依赖。

## 安装

1. 安装 Python 3（`winget install -e --id Python.Python.3.13`，或从 python.org 安装并勾选 **Add python.exe to PATH**）。
2. 在 PowerShell 运行（下载到 `%USERPROFILE%\codex-model-monitor-main` 并安装）：
   ```
   iwr https://github.com/guanbeimark-eng/codex-model-monitor/archive/refs/heads/main.zip -OutFile "$env:TEMP\cmm.zip"; Expand-Archive "$env:TEMP\cmm.zip" $env:USERPROFILE -Force; Start-Process "$env:USERPROFILE\codex-model-monitor-main\安装-开机自启.bat"
   ```
   或手动 Download ZIP 解压后双击 `安装-开机自启.bat`：加入开机自启、桌面生成 "Codex Models" 图标，并打开小窗。

## 更新（与 GitHub 同步）

右键小窗 → **从 GitHub 更新**：自动下载最新版、检查无误后替换并重启（旧版本备份为 `.bak`）。有新版本时小窗底部也会提示。

## 小猫表情

| 表情 | 含义 |
|---|---|
| 😊 眯眼 + 腮红 | 实测的模型全部可用 |
| 👀 眼睛看向一边 | 正在检测 |
| 😟 皱眉 + 汗珠 | 有模型不可用 / 限流 |
| 😐 普通 | 只有目录，还没实测 |
| 😴 闭眼 | 没有数据 |

小猫模式下右下角数字 = 实测可用的模型数（绿 = 全部正常，黄 = 有限流，红 = 有不可用）。

## 列表颜色

🟢 实测可用 · 🔵 在目录中尚未实测 · 🔴 账号不能用该模型 · 🟡 限流 / 超时 / 网络问题

模型新增、下线、变为不可用时，底部黄字提示并响一声（10 分钟后自动消失）。

## 窗口

紧凑小窗：标题行（小猫 + 可用数/总数）+ 每个模型一行"灯 + 名字"。鼠标停在某个模型上可看到详细原因和测试时间。

鼠标悬停时标题行右侧出现三个按钮（像桌宠一样）：

| 按钮 | 作用 |
|---|---|
| — | 缩成小猫（单击小猫展开） |
| 斜杠眼睛 | 隐身：窗口消失，按 **Ctrl+Alt+M** 或双击桌面 "Codex Models" 图标唤回 |
| × | 关闭（双击桌面图标重新打开） |

鼠标离开时窗口自动变淡（`IDLE_ALPHA`），悬停恢复。

## 检测频率

| 项目 | 默认 | 说明 |
|---|---|---|
| 目录检测 | 每 2 分钟 | `codex debug models`，免费；失败时读 `%USERPROFILE%\.codex\models_cache.json` |
| 全部实测 | 每 15 分钟 | 每个模型运行一次 `codex exec`，3 个并行，消耗极少额度 |
| 失败重测 | 每 5 分钟 | 只重测不可用 / 限流的模型 |

在 `codex_model_monitor.pyw` 顶部修改；`PROBE_INTERVAL_MIN = 0` 表示只手动实测。

## 操作

- 拖动标题栏或小猫移动；双击标题栏 = 缩成小猫
- 右键菜单：刷新目录 / 实测全部 / 缩成小猫 / 隐身 / 显示隐藏模型 / 选择 codex 位置 / 从 GitHub 更新 / 打开日志 / 关闭
- 日志：`%APPDATA%\codex_model_monitor\monitor.log`
- 取消开机自启：Win+R 输入 `shell:startup`，删除 "Codex Models"

## 常见问题

- **"找不到 codex"**：会自动在 npm、pnpm、scoop、WinGet 等常见位置查找；仍找不到时右键 → 选择 codex 位置，选中 `codex.cmd` 或 `codex.exe`。
- **移动了文件夹**：重新双击 `安装-开机自启.bat`。
