# Case Workbench · 案例工作台

面向短视频带货团队的本地工作台：把案例、用户评论、内容方向、脚本文案、制作记录、发布登记和销售复盘串在一起。数据保存在自己的电脑上，浏览器作为操作界面。

当前以 **Windows + Python 3.12** 为使用基线。基础界面无需 AI 账号；写稿、配音、自动剪辑、生图等功能需要各自的本机软件、授权或服务。

## 可以做什么

- **案例库**：保存原始资料、当前结论、版本历史和关联证据。
- **用户声音**：汇总多平台评论，查看来源与评论者身份，整理主题、疑问与内容线索。
- **方向与创作**：连接评论证据、测试方向、待写计划和采用的脚本版本。
- **制作与发布**：记录配音、视频/图文制作、成品来源和实际发布版本。
- **数据与复盘**：导入销售表、登记观察结论，把经验用于后续内容。
- **备份**：导出完整数据 ZIP，设置本地自动备份，预览后恢复。

本仓库发布应用源码与通用运行资料。个人案例、原片、评论数据、销售报表、授权信息和个人 AI 技能库需要自行准备，不随源码分发。界面中的产品识别与部分业务口径沿用项目原有场景，可按自己的业务继续改造。

## 让 AI 帮你安装

如果你不熟悉命令行，可以将下面整段复制给能操作本机文件和终端的 AI 助手（如 Codex 或 Claude Code），让它完成安装：

```text
请帮我在这台电脑上安装并启动这个项目：
https://github.com/w393312401-afk/case-workbench

自动检查 Git 和 Python 3.12，下载项目、创建虚拟环境、安装 requirements.txt 中的依赖，并启动 server.py。个人配置写入 config.local.json；如果已经安装，请保留现有配置和业务数据。

完成后打开工作台，确认“用户声音”页面正常，并告诉我以后如何启动、关闭和更新。请直接执行，确实需要我手动操作时再提示我。
```

也可以按下面的「快速开始」自行安装。

## 快速开始

先安装 Git 和 **Python 3.12（64 位）**。Python 安装时启用 Python Launcher；以下命令在 Windows PowerShell 中逐行运行。

```powershell
git clone https://github.com/w393312401-afk/case-workbench.git
cd case-workbench
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

终端显示 `Case workbench: http://127.0.0.1:8767` 后，在浏览器打开 [本机工作台](http://127.0.0.1:8767)。保持这个终端运行；关闭服务可在终端按 `Ctrl+C`。首次使用看到空列表属于正常情况。

无需激活虚拟环境，也无需运行 `npm install` 就能启动基础界面。首次安装如果下载缓慢，可稍后重试安装依赖。

需要更换端口或数据位置时：

```powershell
Copy-Item config.json config.local.json
notepad config.local.json
```

只在 `config.local.json` 中保存个人配置；它会覆盖默认配置，已被 Git 忽略。默认案例库位于 `.local/case-library`，运行资料位于 `.local/runtime`。这两个目录必须独立，不能相同或互相嵌套。完整路径说明、启动验收和可选功能配置见 [安装与配置](docs/安装与配置.md)。

## 新手先读

1. [安装与配置](docs/安装与配置.md)：从装环境、启动，到数据位置、备份与常见错误。
2. [用户声音保姆级教程](docs/用户声音保姆级教程.md)：从评论入库到整理、判断和后续创作。
3. [完整使用说明](使用说明.md)：工作台各模块与业务流程。

“用户声音”指用户评论与反馈研究。生成配音在制作流程里配置，两者是不同功能。

## 功能依赖

| 功能 | 额外准备 |
| --- | --- |
| 基础界面、案例与评论整理、手工记录、销售表处理 | `requirements.txt` 中的 Python 依赖 |
| AI 写稿、审稿、复盘 | 可用的本机 Codex / Claude 调用程序、账号授权；选择技能时还需自己的技能资料 |
| TikTok 公开指标与评论推送 | 兼容的 TK 看板浏览器插件，配置工作台地址与连接码；本仓库不包含插件安装包 |
| 原片抽帧、音频转换、部分成品检查 | 本机 `ffmpeg`、`ffprobe` |
| 配音 | ElevenLabs 账号及本机环境变量 `ELEVENLABS_API_KEY` |
| 达芬奇时间线与 AI 剪辑 | DaVinci Resolve Studio、脚本组件；AI 剪辑还需可用的 Resolve MCP 与自己的剪辑技能 |
| AI 图文制作 | Cockpit 本机图片接口、个人参数配置及对应账号；主体保护另需 Node/Sharp 图像组件 |

外部服务的账号、额度和联网条件由使用者自行配置；仅启动工作台不会自动开启这些功能。详细边界见 [可选功能](docs/安装与配置.md#6-可选功能按需配置)。

## 开发与贡献

后端使用 Python，本地 HTTP 服务与 JSON/SQLite 数据文件；前端为 `static/` 下的原生 JavaScript 和 CSS。`scripts/` 提供案例存储等运行脚本，`deployment/` 提供独立部署工具。

改动前请使用临时测试资料。可以先运行一组后端测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_review_store.py -v
```

浏览器测试额外使用 Playwright；现有测试通过 `channel="chrome"` 启动本机 Google Chrome。详见 [开发验证](docs/安装与配置.md#开发验证)。提交问题时写明操作步骤、预期结果、实际结果和系统版本；附日志前先去除密钥、评论者信息和业务内容。

## 开源许可

应用源码按 [MIT License](LICENSE) 发布。依赖软件各自遵循其许可证。用户导入的数据、素材及第三方平台内容的权利归其原权利人；应用许可证不代替这些内容的使用授权。
