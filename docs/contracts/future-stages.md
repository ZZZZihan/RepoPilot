# Future execution and publication interfaces

`src/repopilot/future.py` 定义两个尚无 adapter、尚未接入 HTTP 的未来 seam。它们只
保留后续 Interface 的 fail-closed 形状，不提供当前执行或发布能力。

## Planning-only authority

当前 `ImplementationPlan` 1.0 只是规划文档。批准记录只证明有人审查过该版本，
`verification_readiness` 只说明计划是否具有最低限度、有证据的 pytest 验证意图；
两者都不证明身份、权限、策略、凭据或隔离工作区已经封印。

`ApprovedPlanExecutionRequest` 会拒绝每一个当前 `ImplementationPlan`，即使计划已经
`approved` 且 `verification_readiness="ready"`。`PullRequestPublicationRequest` 也会
拒绝同一 planning-only 类型。当前模型唯一的 approved 形状仍只是
`approved/version 2/approval.from_version 1`；改变内存中的字段、伪造版本或绕过普通
构造流程不能把旧 planning-only runtime type 升格为权限。

## ApprovedPlanExecutor

未来执行模块必须通过独立 ADR 引入不同的 execution-sealed 计划类型。该类型至少要
不可变地绑定：

- 已认证审批者、权限和审批时刻；
- plan ID、plan version、plan hash 和精确 repository tree SHA；
- 允许读取、修改和创建的仓库相对路径；
- 结构化 argv、验证命令、网络与凭据策略；
- CPU、内存、时间、Patch 大小和输出工件上限；
- 重放/过期策略、日志脱敏、失败恢复与工作区销毁要求。

在该类型和真实 adapter 存在前，`ApprovedPlanExecutor` 没有可构造的合法当前输入。
未来 Interface 也不会暴露 `shell: str` 或宿主绝对路径；隔离、策略、超时和审计属于
执行模块的 Implementation。Docker 或托管 sandbox 只能成为经过验收的 adapter，
不是 Interface 本身。

`ExecutionReport` 的预留 Pydantic runtime model 要求 repository tree SHA 为精确 40 或 64 位小写
十六进制对象 ID。状态为 `succeeded` 时，报告必须包含至少一个合法 changed path、
至少一个 verification result，并且所有结果均 passing。这些 runtime constraints 只是
未来 Interface 的防御性约束，不是运行已经发生的证据；导出的标准 JSON Schema 也不能
替代实际 runtime validator 或执行证据。

## PullRequestPublisher

未来发布模块只能接收 execution-sealed lineage、成功且经过验证的执行报告，以及
独立的发布审批。plan ID、plan version 和 repository tree SHA 必须在封印、执行报告
与发布请求之间完全一致；发布内容只能来自已验证的隔离执行结果。

未来 GitHub App adapter 必须使用最小权限凭据，并在发布前执行第二次 PR 审批。当前
没有 publisher adapter、GitHub 写 token、分支/提交 Implementation 或 PR 路由。

## Why no store seam yet

当前只有一个 SQLite Implementation，测试也使用真实临时 SQLite 文件。一个 adapter
不足以证明存储变化已经形成真实 seam；提前增加转发 Interface 只会扩大调用方要学习
的表面。等 PostgreSQL 或另一种实际 adapter 进入范围时，再在迁移点建立存储 seam。
