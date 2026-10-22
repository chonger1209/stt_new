# 千问换机配置教程（2026.10.22.3）

## 先确认你有什么

本教程针对 Windows、兼容版本的 Strata 和已经能运行的千问模型。下载模型文件不等于服务已经安装好。Ollama、LM Studio 等目前没有对应的 STT 管理适配，不能承诺仅改地址就支持所有按钮和速度。

本教程对应 v2026.10.22.3 及以后版本；更早发布版不支持这些新增字段。

## 1. 放好文件

1. 解压 STT 到固定、可写的目录，例如 `E:/Apps/STT`。
2. 把配套的“千问管理工具包”放到固定目录，例如 `E:/AI/qwen-management`。里面应有 `scripts/manager.py`、`scripts/lifecycle.py`、`service.json`、`state.json` 和 `qwen_service.py`。不要只复制两个同名空文件，也不要只复制 STT EXE。
3. 模型、Strata、显卡驱动和模型运行环境不在 STT 包中。先使用它们原有的启动方式生成一次回答，确认环境正常。这一步由你主动测试；STT 刷新配置不会发送生成请求。
4. 管理工具包里的 JSON 是共享配置模板。首次接入不需要手工编辑它；填写 STT config 后点击启动，STT 会校验并自动同步。运行时身份、任务与锁文件仍由工具包维护。

## 2. 打开 STT 的 config.txt

点击设置按钮。新增字段位于第一个 `[进程名称.exe]` 之前。保留自己的颜色、窗口、软件列表和进程隐藏规则。

| 字段 | 填什么 | 示例 |
|---|---|---|
| qwen_enabled | 是否启用管理，关闭后不查询或控制服务 | true |
| qwen_prepare_service_on_start | STT 启动时准备接收服务，不主动加载模型 | true |
| qwen_toolkit_path | 完整工具包根目录，不是 scripts 子目录 | E:/AI/qwen-management |
| qwen_base_url | 本机 API 地址，端口与启动参数一致 | http://127.0.0.1:8765/v1 |
| qwen_model_id | 从服务的 `/v1/models` 或模型信息中取得真实 ID | 不要凭文件夹名称猜 |
| qwen_python_path | Strata 运行环境里的解释器 | E:/AI/Strata/.venv/Scripts/python.exe |
| qwen_working_directory | Strata 根目录 | E:/AI/Strata |
| qwen_launch_args | 已验证可用的启动参数，JSON 字符串数组 | 见下一节 |
| qwen_startup_timeout_seconds | 整个启动的等待预算，秒 | 300 |
| qwen_idle_timeout_minutes | 最后一次请求后连续闲置多久卸载模型，分钟 | 120 |

工具包路径留空会自动查找用户目录 `.agents/skills/qwen-collaboration`，再查 `.codex/skills/qwen-collaboration`。不需要安装 Codex 才能使用显式工具包路径。

连接地址、模型、解释器、工作目录和参数留空时沿用工具包的现有值；初次使用空模板时，这些值必须填写完整。`[]` 表示明确清空启动参数，不是“自动”。布尔值只能使用 true/false 等合法值，拼错会警告并使用默认值。

## 3. 最容易填错的启动参数

假设你已经验证原启动命令能运行，那么 `qwen_python_path` 填命令中的 python.exe，`qwen_working_directory` 填执行命令时所在目录，`qwen_launch_args` 填 python.exe 后面的参数数组。路径带空格没有关系，每个参数使用独立双引号包围。

下面是**虚构路径示例**，不是给所有模型通用的命令：

```ini
qwen_python_path = E:/AI/Strata/.venv/Scripts/python.exe
qwen_working_directory = E:/AI/Strata
qwen_launch_args = ["E:/AI/qwen-management/qwen_service.py", "--strata-server", "E:/AI/Strata/serve/server.py", "--engine", "strata", "--config", "E:/AI/Strata/my-model.json", "--host", "127.0.0.1", "--port", "8765", "--idle-unload", "0", "--lazy"]
```

- `my-model.json` 是你已经验证可用的 Strata 引擎配置，不是模型目录。显存、模型文件路径等依然由该引擎配置负责。
- 用配套 `qwen_service.py` 包装兼容的 Strata 服务，才能使用适配器管理接口；把脚本放在旁边不会自动接入。
- 示例监听本机，不自动开放局域网，不要求 API Key。已有电脑迁移时保留原网络限制和免密策略，不擅自修改防火墙。
- `--lazy` 使接收服务启动后暂不加载模型。STT 的“启动服务”按钮会请求加载；“休眠服务”只卸载模型。
- JSON 里建议用 `/` 写路径；如果用反斜杠，需要按 JSON 规则写成 `\\`。不要在配置值中放分号，分号后按注释处理。
- 参数及开关必须以所安装的兼容 Strata 版本为准。其他版本或其他引擎不保证可用，配置无法凭空增加服务接口。

## 4. 保存以后点什么

1. 保存 config，点击 STT“刷新”。休眠时间和管理开关立即更新，不重启模型、不打断任务。
2. 地址、模型、路径、参数或工具包位置有改动时会提示“待生效”。STT 继续按原服务配置管理现有进程。
3. 等现有任务完成，正常关闭旧服务；也可在明确接受中断时，用“强制退出”两次确认退出旧服务。
4. 点击“启动服务”。STT 校验配置、备份并更新工具包的共享 service.json，再启动或加载。
5. 如果你只有改超时时间等非服务身份参数，无需为了配置同步强退模型。

启动等待超时不等于服务已经停止：服务端可能仍在加载。查看后续状态，避免反复点击。强制退出仍使用独立通道。

## 5. 按顺序检查

- 启动成功：看到“千问就绪”。
- 自己发送一次测试请求：看到预读/生成，生成时显示 T/s；结束后显示 0.0 T/s。指标接口缺失时显示 — T/s，不冒充零速度。
- 点击休眠：模型卸载，接收服务仍在；再点击启动应能加载。
- 闲置时间默认 120 分钟，提前 1 分钟提醒，可点击重置。STT 退出后不再执行闲置管理。
- 在空闲时测试强退二次确认，确认之后服务确实停止，再测试重新启动。

## 6. 出问题先看这里

| 显示或提示 | 检查方法 |
|---|---|
| 千问未配置 | 工具包根目录是否正确、脚本和 service.json 是否完整 |
| qwen_python_path 不存在 | 找到模型环境真正使用的 python.exe，别填 STT EXE |
| qwen_launch_args 格式无效 | 必须是 JSON 字符串数组，每个参数加双引号；注意反斜杠和逗号 |
| 模型或服务不匹配 | 核对模型 ID、端口和接口，不能结束不明服务 |
| 提示先退出原服务 | 旧服务仍在监听，等待任务结束后关闭，再应用新参数 |
| 能聊天但没有细分速度 | 检查 Strata 的 /metrics 是否兼容；普通 OpenAI 接口不保证提供此指标 |
| 保存记录失败 | 检查磁盘空间和目录权限，保持 STT 运行等待重试；别强杀以免未保存记录丢失 |

日志在 `logs/stt.log`，单文件约 2 MiB，最多保留 5 份轮转备份。原有日期日志保留，不会自动删除。

## 当前电脑升级：不用重新填写

本机升级会在正常退出后备份原 EXE、config 和两份数据库，从当前已验证 service.json 补入缺失字段。已有字段、注释、窗口位置和进程规则优先保留。数据库文件不替换，迁移前后检查 SHA256。冲突或不完整信息会报出，不填虚构示例。
