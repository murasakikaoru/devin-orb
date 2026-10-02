# Devin Orb

一个常驻屏幕边缘的悬浮球，实时显示你的 **Devin 账户用量**：日配额 / 周配额 / 额外余额 / 计费周期，黑曜石玻璃球内嵌一枚走动的时钟，外圈是配额环。

![platform](https://img.shields.io/badge/platform-Windows-blue)
![python](https://img.shields.io/badge/python-3.10+-blue)
![license](https://img.shields.io/badge/license-MIT-green)

## 预览

- **悬浮球**：黑色玻璃球 + 白色时钟指针 + 配额环（绿→琥珀→红按余量变色），呼吸/悬停/涟漪动效
- **用量面板**：单击悬浮球弹出玻璃卡片，弹簧入场动画
- **Pin 模式**：双击悬浮球 → 面板钉在最前端，点别处不收起
- **右键悬浮球**：直接打开设置抽屉

## 数据来源

读取 Devin 客户端自己使用的只读接口（`SeatManagementService/GetUserStatus`），不需要企业 service key。凭证来自本机已有的登录态，按顺序探测：

1. `%APPDATA%\devin\credentials.toml`（`devin auth login` 生成）
2. Devin Desktop 的 `state.vscdb`（`windsurfAuthStatus`）

**隐私**：所有数据只在本机读取与展示，API key 不写入任何本项目文件、不进入日志、不外传。配置存于 `%APPDATA%\devin-orb\config.json`。

## 运行

需要 Python 3.10+：

```bash
pip install PySide6
python app.py        # 或 pythonw app.py 无窗口后台运行
```

## 功能

| 功能 | 说明 |
|---|---|
| 单击 | 打开/收起用量面板 |
| 双击 | 打开面板并钉住（可在设置关闭） |
| 拖拽 | 移动悬浮球，位置自动记忆 |
| 右键 | 打开设置抽屉 |
| 配额环 | 剩余/已用可选，数据源 自动/每日/每周 |
| 主题 | 跟随 OpenUsage / 浅色 / 深色（Devin 黑白配色） |
| 语言 | 中文 / English 一键切换 |
| 自动刷新 | 5 / 10 / 50 分钟 |
| 随 Devin 启动 | 自动注入 Devin SessionStart hook（可在设置中移除） |
| 开机自启 | 可选 |

## 项目结构

```
app.py       # Qt 悬浮球 + 面板窗口 + 生命周期
core.py      # 凭证发现 + GetUserStatus RPC + 数据归一化
config.py    # 设置持久化 / OpenUsage 同步 / 自启 / Devin hook 注入
web/panel.html  # 面板 UI（QWebEngineView + QWebChannel）
```

## License

MIT — 见 [LICENSE](LICENSE)。
