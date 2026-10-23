# Codex 模型监测小窗

Windows 桌面置顶小窗，实时显示 Codex 当前账号哪些模型能用。只用 Python 自带库，无需安装依赖。

## 安装

1. 安装 Python 3（python.org，安装时勾选 **Add python.exe to PATH**）。
2. 下载本仓库（绿色 **Code** 按钮 → **Download ZIP**），解压到一个固定文件夹。
3. 双击 `安装-开机自启.bat`：加入开机自启、桌面生成 "Codex Models" 图标，并立即打开小窗。

## 颜色说明

| 颜色 | 含义 |
|---|---|
| 🟢 | 实测可用 |
| 🔵 | 在模型目录中，尚未实测 |
| 🔴 | 账号不能用该模型 |
| 🟡 | 限流 / 超时 / 网络问题 |

模型新增或下线时，底部会黄字提示并响一声。

## 检测方式

- **目录检测**（免费，每 10 分钟）：`codex debug models`；失败时读 `%USERPROFILE%\.codex\models_cache.json`。
- **实测**（每 60 分钟，消耗极少额度）：对每个模型运行一次 `codex exec`，能回复即可用。

间隔等设置在 `codex_model_monitor.pyw` 顶部修改，`PROBE_INTERVAL_MIN = 0` 表示只手动实测。

## 操作

- 拖动标题栏移动，双击标题栏折叠
- 右键菜单：立即刷新 / 实测全部 / 显示隐藏模型 / 打开日志 / 退出
- 日志：`%APPDATA%\codex_model_monitor\monitor.log`
- 取消开机自启：Win+R 输入 `shell:startup`，删除 "Codex Models"

## 常见问题

- **显示"找不到 codex"**：在脚本顶部填写 `CODEX_PATH`，通常为 `C:\Users\用户名\AppData\Roaming\npm\codex.cmd`。
- **移动了文件夹**：重新双击 `安装-开机自启.bat`。
