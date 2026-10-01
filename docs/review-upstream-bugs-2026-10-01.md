# 上游更新 Bug 审阅与修复交接

审阅基线：`origin/main`，提交 `9d3d8e7025e5966c4b279601bc09b1500ec895a5`（2026-09-30）。本地 `main` 当时为 `2bcdd20`，落后 19 个提交。本文是对上游快照的审阅，工作区没有合并或修改业务代码。

本轮重点检查最新提交增加的长任务停止能力，并补充评估把 115 整理执行从 MoviePilot 切换为 115 开放平台（下文称 Open115）的可能性。

## 已发现问题

### P2：媒体库扫描在停止信号到达收尾阶段时仍会删除未扫描记录

相关位置：

- [scanner.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/scanner.py#L493)：全库扫描完成遍历后直接调用 `_delete_unseen()`。
- [scanner.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/scanner.py#L523)：单库扫描完成遍历后直接调用 `_delete_unseen()`。
- [scanner.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/scanner.py#L567)：局部扫描完成遍历后直接调用 `_delete_unseen()`。

扫描开始时会把目标记录的 `seen_scan` 清零，扫描每个条目时由 `_upsert()` 检查停止信号。但最后一个条目写完后，如果用户在清理未见记录之前按停止，收尾没有再检查停止状态，仍会删除 `seen_scan=0` 的数据库记录。此时停止接口仍可能返回 `stopped: true`，与“没扫到的不删除”的界面承诺不一致。

修复要求：每次执行 `_delete_unseen()` 以及移除已不存在媒体库的清理前确认本轮没有被取消；被取消的扫描不能执行依赖“扫描完整”的删除收尾。扫描已完成的独立范围可以保留其既有结果，但不能把未完成范围当成完整扫描。

验收标准：停止发生在扫描最后一个条目之后、删除未见记录之前时，原数据库记录仍保留；未停止的正常扫描仍能删除确实已不存在的项目。

### P2：115 全量同步在最后一项之后收到停止，仍会删除旧 strm 并保存进度

相关位置：[strm_sync.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/strm_sync.py#L733) 到 [strm_sync.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/strm_sync.py#L760)。

全量同步在处理每个目录项前检查停止信号，但最后一项完成后会继续执行 `_remove_stale()`、`index.replace_all()` 和 `_save_state()`。如果停止请求在最后一项处理完成后到达，同步仍可能删除本机旧 strm、替换索引并推进进度；这违反了停止时“不删 strm、不存本轮进度”的承诺。

修复要求：把目录遍历与破坏性/进度收尾视为取消边界，在删除旧文件、替换索引、保存同步状态之前再次检查停止信号。取消时保留本机旧 strm、旧索引和旧同步游标。

验收标准：停止发生在最后一个目录项处理后、旧文件清理前时，不删除旧 strm，不替换索引，不保存当前任务的同步游标；下一次同步仍能补齐。

### P2：停止“找重复”可能仍会完成版本分析并覆盖上次结果

相关位置：

- [dupes.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/dupes.py#L347)：读取 115 文件清单后开始整理结果。
- [dupes.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/dupes.py#L367)：调用 `_find_versions()`。
- [dupes.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/dupes.py#L370)：事务内清空并替换旧结果表。
- [dupes.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/dupes.py#L417)：`_find_versions()` 遍历媒体库记录时没有检查停止信号。

115 文件列表阶段和大文件路径构造阶段有停止检查，但版本匹配循环没有检查。如果用户在 `_find_versions()` 运行期间停止，代码仍可能完成计算并进入事务，覆盖旧的重复项和大文件结果。尤其是本次没有大文件时，后续 `_big_rows()` 也不会提供额外的停止检查。

修复要求：版本分组循环中响应停止信号，并在写事务前再次检查。扫描被取消时，不得替换 `dup_files`、`dup_versions`、`big_files` 或扫描时间元数据。

验收标准：停止发生在版本匹配进行中时，任务标记为已停止，数据库仍保留上一次完整扫描的结果；未停止时继续按原有逻辑原子替换结果。

## Open115 是否会让 115 整理更快

这里的 Open115 指 115 开放平台 API。结论是：**有机会减少批量移动的请求数，但不能据此断定整个整理流程会更快；改名仍需要逐文件处理，且现有 MoviePilot 流程承担了识别和执行语义。建议另做受控基准，不与上述 Bug 修复混在一起。**

当前实现中：

- [p115_open.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/p115_open.py#L252) 的 Open115 客户端封装了列目录、查路径、取下载地址等能力，没有供整理使用的改名/移动方法。
- [moviepilot.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/moviepilot.py#L584) 的 `transfer()` 通过 MoviePilot 的整理 API 执行 `transfer_type=move`；整理预览也由 MoviePilot 返回目标路径。
- [reorganize.py at reviewed upstream](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/reorganize.py#L410) 按预览结果把文件批次交给 MoviePilot 执行，之后再进行来源目录清理和增量同步。
- 项目 Wiki 说明整理时 115 有请求频率限制，数百集可能需要较长时间；MoviePilot 整理还处理媒体识别、目录格式、覆盖规则、历史整理记录和后台重试。

上游 Open115 客户端资料展示了两个不同粒度的接口：`/open/ufile/move` 接受多个文件/目录 ID，但它们共用一个目标目录；`/open/ufile/update` 的封装参数是单个 `file_id` 和 `file_name`。这意味着把多集移动到同一个季目录时，可能把多个 move 请求合成一个；不同文件的重命名仍然需要逐文件调用。OpenList 的 115 SDK 将这两个接口链接到 115 官方文档；本轮浏览工具无法打开官方 Yuque 页面，所以接口参数以 SDK 暴露的请求结构为依据，官方文档当前的批次上限和限流数字仍需实施前核实。

即使保留 MoviePilot 预览，改用 Open115 执行也需要谨慎保留以下行为：不覆盖/覆盖策略、目标同名文件判断、已经整理过的 MoviePilot 历史记录、复制或链接模式下清理旧目标、大小写与扩展名处理、部分失败后的进度记录、文件移动后的同步和本机附属文件迁移。直接把当前执行 API 替换掉，可能产生重复文件或本机索引与 115 不一致。

建议的性能验证路径：

1. 先保留 MoviePilot 负责识别和预览，只对预览后完全确定的计划做 Open115 执行原型。
2. 在隔离的测试网盘目录、可恢复文件集上分别测 MoviePilot 执行与 Open115 执行，记录请求数、总耗时、限流/冲突、部分失败恢复、增量同步后本机 strm 和附属文件状态。
3. 优先验证“多个文件改名后进入同一个目标目录”的典型剧集场景；分别统计重命名请求与批量移动请求，避免用总耗时掩盖失败或同步成本。
4. 只有端到端耗时确实下降、失败恢复及目录状态一致时，再考虑作为可选执行通道；不要先假设它全面替代 MoviePilot。

## 修复交接给 Opus

请针对上面三个 P2 问题进行最小范围修复，基于当前 `origin/main` 的 `9d3d8e7`。重点保证取消信号在清理/提交边界生效：扫描停止时不删除未扫描记录；115 全量同步停止时不删除旧 strm、不更新索引或游标；找重复停止时不覆盖上一次完整结果。

遵守仓库根目录 `Agent.md`：不要新增测试案例或测试文件，不要改写已有断言来绕过问题。可以运行现有测试、静态检查和已有页面脚本检查；结果需区分实际通过的检查和代码推理。Open115 性能方向仅做评估或单独基准，不属于这三个 Bug 的必需修复范围，也不要在没有测量前直接切换整理执行通道。

## 验证边界

审阅时已在 `9d3d8e7` 快照运行：

- `uv run --with pytest pytest -q`：295 passed，1 条 Starlette/httpx 弃用警告。
- `uv run --with pytest pytest -q tests/test_admin_page.py tests/test_stop.py tests/test_webdav.py tests/test_offline115.py`：13 passed，1 条同类警告。
- Python `compileall`：通过。
- `git diff --check 97f9093..origin/main`：通过。

这些现有测试没有覆盖上述三个“最后一项处理完成后、破坏性收尾之前收到停止”的时序。没有真实 115 账号、MoviePilot 实例和大规模网盘目录的端到端性能数据；Open115 是否能让真实整理任务更快仍需实测。

## 上游资料

- Mi302 整理流程说明：[115-Cloud-Sync.md](wiki/115-Cloud-Sync.md#organising-115)
- Mi302 Open115 客户端：[p115_open.py](https://github.com/MiCat-S/Mi302/blob/9d3d8e7/embyserver/p115_open.py)
- 115 官方移动 API（由 SDK 注释链接；浏览工具无法打开）：[移动文件](https://www.yuque.com/115yun/open/vc6fhi2mrkenmav2)
- 115 官方更新 API（由 SDK 注释链接；浏览工具无法打开）：[更新文件名](https://www.yuque.com/115yun/open/gyrpw5a0zc4sengm)
- 社区客户端的请求结构：[OpenList 115 SDK `Move` / `UpdateFile`](https://github.com/OpenListTeam/115-sdk-go/blob/main/fs.go)
