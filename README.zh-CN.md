<p align="center">
  <img src="icon.png" width="128" alt="Devin Orb">
</p>

<h1 align="center">Devin Orb</h1>

<p align="center">
  一个常驻屏幕边缘的悬浮球，实时显示你的 Devin 账户用量。
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="LICENSE">MIT 许可证</a> ·
  <a href="README.md">English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows-blue" alt="platform">
  <img src="https://img.shields.io/badge/python-3.10+-blue" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license">
</p>

黑曜石玻璃球常驻屏幕边缘，实时显示 **Devin 账户用量**：日配额 / 周配额 / 额外余额 / 计费周期。球内嵌一枚走动的时钟，外圈是配额环。

## 功能

- **悬浮球**：黑色玻璃球 + 白色时钟指针 + 配额环（绿→琥珀→红按余量变色），呼吸/悬停/涟漪动效
- **用量面板**：单击悬浮球弹出玻璃卡片，配额条与余额一目了然，弹簧入场动画
- **Devin 通知接管**：Devin 发 Windows 通知（会话完成 / 需要确认权限 / 提问）时，原生横幅被压制，消息以气泡对话框形式显示在悬浮球上方；球体弹跳并自动从屏幕边缘拉出；点击气泡把 Devin 提到前台
- **边缘停靠**：把悬浮球拖到屏幕边缘，空闲后自动缩入只露半圆（配额环仍在露出边缘可见）；悬停或点击拉出
- **面板跟随**：拖拽、停靠、缩入全程实时跟随悬浮球
- **设置**：配额环数据源（自动/每日/每周）、显示模式（剩余/已用）、主题（跟随/浅色/深色）、语言（中文/English）、刷新间隔（5/10/50 分钟或自定义分钟数）、随 Devin 启动、开机自启

## 数据来源

读取 Devin 客户端自己使用的只读接口（`SeatManagementService/GetUserStatus`），不需要企业 service key。凭证来自本机已有的登录态，按顺序探测：

1. `%APPDATA%\devin\credentials.toml`（`devin auth login` 生成）
2. Devin Desktop 的 `state.vscdb`（`windsurfAuthStatus`）

**隐私**：所有数据只在本机读取与展示，API key 不写入任何本项目文件、不进入日志、不外传。配置存于 `%APPDATA%\devin-orb\config.json`。

## 快速开始

从 [Releases](../../releases) 下载 `DevinOrb-*-win-x64.zip`，解压到任意目录，运行 `DevinOrb.exe`。

或从源码运行（需要 Python 3.10+）：

```bash
pip install -r requirements.txt
python app.py        # 或 pythonw app.py 无窗口后台运行
```

## 操作

| 操作 | 结果 |
|---|---|
| 单击 | 打开/收起用量面板 |
| 双击 | 面板钉在最前端 |
| 拖拽 | 移动悬浮球，位置自动记忆；拖到边缘松手即停靠 |
| 右键 | 打开设置抽屉 |
| 点击气泡 | 收起气泡并把 Devin 提到前台 |
| 托盘图标 | 左键开关面板；右键菜单（打开面板 / 设置 / 退出） |

## 项目结构

```
app.py       # Qt 悬浮球 + 面板 + 气泡 + 动效 + 生命周期
core.py      # 凭证发现 + GetUserStatus RPC + 数据归一化
config.py    # 设置持久化 / OpenUsage 主题同步 / 自启 / Devin hook 注入
notify.py    # 事件驱动通知拦截（WinEvent + UI Automation），零轮询
```

## License

MIT — 见 [LICENSE](LICENSE)。
