# STT 2026.10.22 优化试用版

Windows 屏幕时间追踪、Caps Lock 自动切换、时间流与番茄倒计时。

## 下载与运行

从 [v2026.10.22 发布页](https://github.com/chonger1209/stt_new/releases/tag/v2026.10.22) 下载 portable ZIP，解压到可写目录后运行 stt_new.exe。不需要安装 Python。也可以只下载 EXE，首次运行会自动生成中文配置、日志目录和两份数据库。

当前提供 Windows x64 程序，已在 Windows 本地环境验证。MiSans 字体不随软件分发，系统没有时使用等线。

## 功能和变更

- 保留 Caps Lock 状态、按软件自动切换大小写、窗口置顶、拖动和自动隐藏标题栏。
- 保留应用时长统计、彩色时间流、悬停提示和倒计时。
- 时间流共同累计短片段，满 grid_second_per_block 秒才生成一格，颜色取该格结束时的应用。
- 数据库由专用后台线程读写。事务成功后才清除待保存记录，失败保持原批次并重试。
- 退出等待保存结果，数据库忙时窗口仍能响应；保存失败时保持程序运行并提示。
- 同一数据目录增加单实例保护，避免重复统计。其他目录和旧版程序需由用户避免同时运行。
- 默认配置有完整中文注释，未列出的程序也正常统计；进程规则不是白名单。

## 配置与升级

修改 config.txt 后点击刷新。默认时间流为 30 秒/格；grid_second_per_block = 60 表示 60 秒/格。默认倒计时 25 分钟。

旧版升级前正常退出并备份原目录，再替换 EXE、图标；保留自己的 config.txt、screen_time_history.db 和 time_stream_history.db。不要用压缩包内的默认 config.txt 覆盖自己的配置，也不要同时启动新旧版本。本版延续两份 SQLite 数据库格式。

## 可选的本地千问管理

千问管理依赖另外安装并配置的 qwen-collaboration 工具包（manager.py、lifecycle.py）及 Strata 环境；它们和模型权重不包含在本发布包内。未安装时显示“千问未配置”，STT 的统计和倒计时仍可使用。此发布不是千问的一键安装器，也不会为其他电脑自动配置服务或防火墙。

已具备兼容环境时：左按钮“休眠服务 / 启动服务”；右按钮第一次点击变为“确认退出”，5 秒内再次点击才强制终止服务与任务，超时恢复。默认闲置 120 分钟卸载模型，提前 1 分钟提醒；配置项为 qwen_idle_timeout_minutes。卸载保留接收服务；再次加载需要时间。状态文字前使用小中点，按钮为等线 9 磅常规字重。

配套的 qwen_service.py 适配器支持局域网范围限制、仅本机管理接口和免 API Key 访问，但需要用户自行配置兼容服务。网络地址、安装路径和允许网段应使用用户自己的值；本包不含任何个人服务配置、密钥或统计记录。

## 从源码运行和构建

在 optimized 目录中使用带 tkinter 的 Windows Python 3.13：

```powershell
python -m pip install -r requirements.txt
python stt_new.py
python build_exe.py
```

构建结果在 optimized/dist。源码入口与 EXE 都把数据保存在各自程序目录。

## 验证范围

开发机已通过 90 项逻辑测试、8 项 Strata 模拟 HTTP 测试及打包 EXE 的真实 Tk 检查。包含数据库锁冲突、事务回滚、异步保存期间 UI 心跳、跨日合并、单实例、强退确认和字体回退。

可移植核心测试：

```powershell
python -m unittest test_regressions test_async_storage test_single_instance test_qwen_force -q
python stt_new.py --smoke-test --fresh-config --smoke-output smoke-result.json
```

完整 test_qwen 的部分用例需要已安装工具包；test_qwen_service 需要兼容 Strata 的 server.py 与依赖。模拟测试不会向真实模型发送生成请求。

真实模型推理、其他电脑局域网唤醒和真实工作中强退未作为本次公开发布的通过项；因此本版先标为试用版。长期运行和极端数据量仍有后续优化空间。

## 旧版仍然保留

[旧版 2.0 下载](https://github.com/chonger1209/stt_new/releases/tag/stt_new)。仓库根目录的 stt_new.py 和 dist/stt_new.exe 保留旧版，优化版源码在 optimized/。
