# 开发、构建与验证

[返回使用手册](../README.md)

## 环境与运行

应用面向 Windows，依赖 `pywin32`、`psutil` 与 Python 的 `tkinter`。发布 EXE 自带运行依赖；从源码运行需要自己安装。

开发机使用 Windows Python 3.13。请使用带 tkinter/Tcl/Tk 的完整 Python 安装；仅能运行命令行的裁剪解释器可能无法创建界面。

在仓库根目录进入新版目录：

```powershell
cd optimized
python -m pip install -r requirements.txt
python stt_new.py
```

源码运行的数据目录为 `optimized/`。请勿将个人 `config.txt`、数据库、日志、模型文件或密钥提交到 Git。

## 模块职责

| 模块 | 职责 |
|---|---|
| `stt_new.py` | 程序入口、正式实例互斥、诊断/模拟测试入口 |
| `single_instance.py` | 按规范化数据目录建立 Windows 内核互斥，不依赖易残留的锁文件 |
| `app.py` | Tk 窗口、前台检测、图表、配置应用、按钮、提醒与退出流程 |
| `tracking.py` | 使用区间、跨日切分、待写确认、格子累计与单调时钟倒计时 |
| `storage.py` | SQLite 表结构、两库事务、单线程数据库任务队列、UI 缓存与提交回执 |
| `settings.py` | 默认值、中文模板、旧配置升级、读取校验与坐标保存 |
| `qwen_control.py` | 后台管理队列、状态查询、独立强退与取消旧操作 |
| `qwen_backend.py` | 兼容外部管理工具包、启动/加载/卸载、进程身份核验和强退 |
| `qwen_status.py` | 指标白名单、阶段优先级、速度归零和文字对比度 |
| `qwen_service.py` | Strata 接收层适配：网段准入、本机管理与请求/卸载竞争保护 |
| `smoke_test.py` | 临时数据目录、模拟千问、真实 Tk 控件检查 |

根目录 `stt_new.py` 是旧版入口，`optimized/stt_new.py` 是新版入口，不要混淆。

## 保存链路与线程边界

Tk 采样/切换应用 → 结算不可变批次 → 数据库工作线程事务写两库 → UI 轮询有序回执 → 清理已提交前缀。

失败批次留在后台队列原位重试，成功事务不会因为 UI 轮询较晚而重复执行。UI 使用缓存读取，不跨线程共享 SQLite 连接。退出先停止新采样、提交尾段，等待待写记录、后台任务与连接关闭后销毁窗口；失败则提示并继续运行。

千问普通操作在管理线程执行，强退独立处理；UI 不应 `join()` 等待模型任务。后台线程不直接操作 Tk 控件。

## 构建 EXE

```powershell
python build_exe.py
```

输出 `optimized/dist/stt_new.exe`，构建辅助文件位于 `optimized/build/`。构建解释器必须能导入 tkinter，否则可能得到启动后缺少 Tk 模块的程序。此命令不自动发布 GitHub Release、不打包个人数据。

便携包另加入图标、由默认配置生成的 `config.txt`、使用说明及 `docs/`。千问适配器可放在 `配套资源/`，但外部工具包、模型与机器专用配置需单独部署。

## 可移植核心测试

在 `optimized/` 执行：

```powershell
python -m unittest test_regressions test_async_storage test_single_instance test_qwen_force -q
```

发布时为 49 项，通过范围包括：区间守恒、跨日、倒计时、配置保留、格子累计、锁等待、事务回滚、异步提交回执、跨日读写顺序、实例互斥以及强退取消竞争。

实例互斥测试使用 Windows API，因此这里的“可移植”指不依赖作者的模型环境，不表示支持 Linux。

## 完整工具包与 Strata 模拟测试

```powershell
python -m unittest test_regressions test_async_storage test_single_instance test_qwen test_qwen_force test_qwen_status -q
```

发布开发环境为 104 项。`test_qwen` 中的部分用例动态加载外部工具包，不具备工具包时不能把失败解释为普通 STT 无法使用。

```powershell
python -m unittest test_qwen_service -q
```

这一组为 8 项，加载本地 Strata 的真实 HTTP 处理代码，但使用模拟引擎，不调用真实千问生成。当前测试夹具默认查找 `D:\Program Files\Strata\serve\server.py`；其他环境需调整夹具路径并安装 Strata 的依赖，不应盲目运行。

## 真实 Tk 与打包程序检查

```powershell
python stt_new.py --smoke-test --fresh-config --smoke-output smoke-result.json
python stt_new.py --smoke-test --fresh-config --fallback-font --smoke-output smoke-fallback.json
```

对 EXE 可使用相同参数。`--fresh-config` 使用临时目录中的全新配置，模拟测试不会改动实际历史数据库，也不发送真实模型生成请求。

覆盖按钮状态、强退首次/二次确认/超时重置、两列居中、字体回退、提醒重置、重绘绑定增长、60 秒格子规则、数据库锁住时 Tk 心跳继续及解锁后保存退出。

## 发布和文档维护原则

- 用明确文件清单构建发布包，不把整个开发目录压缩上传。
- EXE 校验、ZIP 校验与发布附件校验分开记录。
- 程序只改文档时保持 EXE 不变；本次文档更新不移动已发布源码标签。
- Release 中的独立源码 ZIP 和便携 ZIP 可更新随包文档；GitHub 自动生成的 tag 源码档案保留当时快照。最新文档以 `main` 为准。
- 已有旧版 Release、标签和旧程序不因文档更新被删除或覆盖。
- 测试参数、尺寸断言通过不等于视觉美观；不同 DPI、字体和多显示器仍需实际查看。

未作为本次通过项：真实千问推理、真实任务中强退、其他电脑端到端访问和长期实机耐久验证。不要把模拟测试或短时容量测量写成这些项目已通过。
