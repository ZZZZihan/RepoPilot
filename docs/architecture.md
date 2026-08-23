# Current architecture

RepoPilot 把当前链路压缩在一个较深的规划模块后面。HTTP 调用方只需要理解“创建、读取、批准”三个计划动作；仓库选择、字节上限、证据生成、Schema 验证和持久化细节都留在实现内部。

```text
FastAPI interface
  └─ PlanningService interface
       ├─ RepositoryInspector seam
       │    ├─ GitHubRepositoryInspector adapter (production)
       │    └─ FixedRootRepositoryInspector adapter (fixture tests only)
       ├─ PlanBuilder implementation
       └─ SQLitePlanStore implementation + approval transition
```

## Repository inspection seam

`RepositoryInspector.inspect(repository)` 是当前真正发生变化的 seam：生产环境通过固定 GitHub REST 主机读取 Git tree/blob；自动化测试通过预先注入的固定根目录读取夹具。HTTP 请求不能选择适配器，也不能传入本地路径。

两种适配器共享相同的选择策略和上限：

- 最多查看 2,000 个文件条目；
- 最多选取 32 个证据文件；
- 单文件最多 64 KiB；
- 选取内容合计最多 384 KiB；
- GitHub 单响应最多 2 MiB；
- GitHub 请求默认 10 秒超时；
- 只选 README、Python 项目/测试配置、`.py` 源码与测试；
- 不克隆、不导入、不执行仓库内容。

超过文件树硬上限或 GitHub 返回截断树时，系统拒绝生成可能误导用户的计划。代表性文件选取达到较软的内容上限时，计划会记录 `inspection.selection_truncated=true` 风险。

## Evidence and plan schema

每条证据包含稳定 ID、仓库相对路径、类别、行范围和内容 SHA-256，但不持久化仓库原文摘录。计划文件引用只能使用已存在的证据 ID；顶层 Schema validator 还会校验步骤顺序、证据图以及状态/版本/审批记录的一致性。

当前 `PlanBuilder` 是确定性的。Issue 中出现的标识符会提高包含相同文本的源码和测试文件优先级。它可以给出可审查的第一份计划，但不等同于语义完整的代码理解。

## Persistence and state

SQLite 保存整个版本化计划 JSON，同时保留可查询的状态和版本列。写入前、读出后和状态转换后都重新执行 `ImplementationPlan` 校验。

唯一合法转换为：

```text
proposed (version 1, approval=null)
  -- approve(expected_version=1) -->
approved (version 2, approval present)
```

转换使用 SQLite `BEGIN IMMEDIATE` 和条件更新。过期版本与重复转换均返回冲突，不会悄悄覆盖状态。

