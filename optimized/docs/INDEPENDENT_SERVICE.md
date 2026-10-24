# 千问独立启动（2026.10.24 发布版）

本说明对应当前源码与配套工具包。Release v2026.10.22.3 是历史构建，不包含本轮独立启动改动，不能与新工具包混用。本次 v2026.10.24 已提供配套 EXE 和工具包，全部历史版本保留。

## 行为

接收服务由 Windows 计划任务托管；客户端只请求启动任务，任务未配置时明确报错，没有直接 Popen 接收服务的降级路径。关闭 STT 不主动关闭服务，会话启动与状态查询不预热模型；自动准备只启动接收服务，明确点击启动才加载模型。

普通停止持久保存 desired_state=stopped，宿主拒绝新工作并排空后退出；强退先保存停止状态，再核验进程身份并终止。重新登录不清除手动停止，明确启动才恢复。startup 锁、共享预算、generation、instance_id 与 load-pending 防止竞争、旧启动请求与重复加载。

## 换机配置步骤

1. 准备已能运行模型的 Strata、模型文件和对应 Python 环境。工具包不包含模型或 GPU 环境。
2. 将完整工具包放到固定目录，例如 E:/AI/qwen-management。保留 scripts 全目录、service.json、state.json、register-task.ps1。
3. 编辑 service.json：base_url 填本机 API 地址，model 填真实 ID；launch.command 填 Strata 的 python.exe；cwd 填 Strata 根目录；args 填实际参数数组。首项使用工具包 scripts/qwen_service.py，后接 --strata-server 和 Strata serve/server.py 的路径，再加实际的 --engine、--config、--host、--port、--idle-unload 0、--lazy 参数；需要局域网时用 --allow-subnet 指定网段。管理接口仅本机访问。不要照搬别人的绝对路径。
4. scheduled_task 填 Qwen-Independent-Receiver；startup_timeout_sec 设900；lifecycle.enabled 保持false、owner为stt、unload_on_codex_exit为false。
5. 当前用户在 PowerShell 中运行以下命令，并替换路径。脚本拒绝覆盖同名任务，已有任务要先核对用途。

```powershell
& 'E:/AI/qwen-management/register-task.ps1' -Root 'E:/AI/qwen-management' -Python 'E:/AI/Strata/.venv/Scripts/python.exe' -TaskName 'Qwen-Independent-Receiver' -AtLogOn
```

任务使用 python.exe 同目录的 pythonw.exe、普通权限、单实例、无运行时限；登录后延迟30秒运行。它是登录用户任务，不是无人登录的系统服务，也不会给 STT 添加登录启动项。

6. STT config 的 qwen_toolkit_path 填工具包根目录。地址、模型、Python、工作目录、启动参数必须与 service.json 一致；qwen_launch_args 为 JSON 数组，首项同样使用 scripts/qwen_service.py。不一致会拒绝启动，不再自动覆盖共享配置。qwen_startup_timeout_seconds 与共享 startup_timeout_sec 都设900，仅改 STT 不会同步 Runtime 预算。
7. qwen_idle_timeout_minutes 自行选择。当前用户部署为300分钟，公共 STT 模板仍保留原默认值；闲置管理依赖 STT 持续运行。保留自己的颜色、位置、进程规则与数据库。
8. 启动 STT，先检查状态，再决定是否点击启动加载模型。查询不会发送真实生成请求。可选 MCP 桥接为 scripts/local_model_mcp.py，需要另装 mcp Python 依赖；若使用 Codex，该服务器 tool_timeout_sec 可设1200、startup_timeout_sec 保留25，重启后刷新配置。

## 验证与边界

2026-10-24 正式部署记录确认 D盘EXE、窗口响应和10个文件哈希通过；服务祖先为计划任务宿主而非 Codex/ChatGPT。假服务通过 Windows Job 终止存活、并发加载去重与超时边界验证。

未实际关闭当前 Codex 或重启电脑，不能宣称两项真实验收完成；未执行真实模型推理。超时加载结果不明且服务仍存活时保守保留标记，可能需要明确停启恢复。

契约测试（在 optimized 目录）：

```powershell
python -m unittest discover -s '配套资源/千问管理工具包/tests' -p 'test_*.py'
```

升级前正常退出 STT，并备份程序和工具包；不要覆盖个人 config、数据库、运行时状态。回退前停止服务、退出 STT，核对并移除本次任务后恢复备份，避免两套服务争用端口。
