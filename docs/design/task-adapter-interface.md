# 任务执行器适配接口（#20 冻结）

2026-10-18：#20 实现分类任务完整流程时冻结的接口。后续 #21（校验／保存与重试）与
#22（完整等待树与预算）在不改动本接口语义的前提下并行扩展。文件按关注点拆开，避免同时
修改同一文件。

## 文件与归属

| 文件 | 关注点 | 主要归属 |
|---|---|---|
| `src/xhs_to_opencook/task_engine.py` | 阶段机（transitions）、tick、重建、暂停 | #20 冻结；后续尽量不改 |
| `src/xhs_to_opencook/task_wait.py` | 等待策略（观察、稳定判定） | #22 扩展 |
| `src/xhs_to_opencook/task_adapters.py` | 浏览器／日志真实适配、校验与提交装配 | #21 扩展 |
| `src/xhs_to_opencook/storage.py` | SQLite 存储、`import_tasks`、原子提交 | 存储变更集中于此 |

存储与等待策略分属 `storage.py` 与 `task_wait.py`；#21 改校验／提交只动
`task_adapters.py`（必要时 `storage.py`），#22 改等待只动 `task_wait.py`。

## 引擎（`TaskExecutor`）

- 构造：`TaskExecutor(identity, browser, journal, *, clock, validate=None, finalize=None,
  bootstrap=None, stable_seconds=1.0)`。
- 阶段集合固定为 `PHASES`：preparing、submitting、waiting、validating、saving、completed、
  manual_review、blocked；`TERMINAL_PHASES` 为后三者。存储中阶段不在 `PHASES` 内直接失败关闭。
- `tick()`：仅浏览器属主线程可调用；不睡眠；每次最多推进一个动作并返回可持久化快照。
- `pause()`：属主线程立即生效；其他线程仅写入 `pause_requested`，由属主线程在下一次
  `tick()` 内中断观察，绝不跨线程触碰浏览器。
- `resume()`、`snapshot()`：任意线程安全；`resume()` 会把 submitting／waiting 标记为需核对。
- 重建只读存储事实（`journal.load`），不序列化框架对象。

## 适配器协议（由 `browser` 实现）

- `submit(request)`：保存发送意图并确认提交。返回 `{"submitted": bool}`；未确认可返回
  `{"submitted": False, "unsent": True}` 表示已确证未发送。
- `reconcile(request)`：核对原请求。返回 `{"submitted": True}`、`{"submitted": False,
  "unsent": True}`（确证未发送，允许按原请求身份重发）或无法确认。
- `observe(request)`：有界单次观察。返回 `{"matched": bool, "finished": bool, "answer": str}`；
  `matched` 要求唯一归属。
- `cancel_observation()`：仅停止本地观察，不推断远端生成结束。

## 日志协议（由 `journal` 实现）

- `load(identity) -> dict | None`、`save(identity, record) -> None`。
- 真实实现 `StoreJournal` 落在 `storage.py` 的 `import_tasks` 表。

## 注入钩子

- `validate(answer, record) -> result`：成功后返回结果载荷，写入 `record["result"]`；抛
  `TaskError` 时按 `reference_mismatch`（归属冲突 → 阻塞）或其他（→ 人工复核）处理。
- `finalize(record)`：原子且幂等地提交业务结果、任务完成标记与必要队列变化；#21 在此扩展
  保存重试，`storage.py` 的 `commit_classification` 即其一例。
- `bootstrap(identity) -> record`：为全新任务提供初始记录（请求身份、来源、原始目录等）。

## 存储接口（`Store`）

- `task_get(identity)` / `task_save(identity, record)`：`import_tasks` 读写。
- `commit_classification(identity, classification, task_record, *, enqueue_job=None)`：在同一
  事务内写分类行、任务完成标记与教程提取入队（`INSERT OR IGNORE`），可重复执行。
- 既有 `stage_get`/`stage_save`/`enqueue`/`claim` 语义不变。

## 调度入口

`Workflow.process(stage, notes)` 在 `stage == "classify"` 且 `args.task_engine == "v2"` 时转入
`process_classification`；其余一律走既有路径。新执行路径仅接管显式选择的新任务；存在旧未决
请求且无 v2 任务记录时跳过，历史未决请求不自动迁移。
