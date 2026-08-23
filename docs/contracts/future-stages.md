# Future execution and publication interfaces

`src/repopilot/future.py` 定义两个尚无适配器、尚未接入 HTTP 的未来 seam。它们用于提前固定安全前置条件，不提供当前能力。

## ApprovedPlanExecutor

未来执行模块只接收完整的 `ApprovedPlanExecutionRequest`，其中计划必须：

- 状态为 `approved`；
- 带审批记录和不可变版本；
- 固定到仓库 tree SHA；
- 文件引用全部通过证据校验。

接口没有 `shell: str` 或宿主路径参数。执行适配器必须在其实现内部负责隔离工作区、命令策略、网络策略、资源上限、Patch 范围、超时和审计，并返回结构化 `ExecutionReport`。Docker 可以成为一种适配器实现，但不是接口本身，也不在当前切片中。

在实现该模块前至少需要另一个 ADR 明确：身份与权限、仓库获取和凭据隔离、允许的工具与 argv 策略、非 root 沙箱、网络默认关闭、文件写入范围、补丁大小、重试和恢复、日志脱敏及工作区销毁。

## PullRequestPublisher

未来发布模块只接收 `PullRequestPublicationRequest`。Schema 要求：

- 计划已经批准；
- 执行报告成功；
- plan ID、plan version 和 tree SHA 完全一致；
- 发布内容来自已验证执行结果。

未来 GitHub App 适配器必须使用最小权限凭据，并在调用前增加第二次 PR 审批。当前没有 publisher adapter、GitHub 写 token、分支/提交逻辑或 PR 路由。

## Why no store interface yet

当前只有一个 SQLite 实现，测试也使用真实临时 SQLite 文件，因此额外的存储 port 只会增加转发层。等 PostgreSQL 或另一种实际适配器进入范围时，再在迁移点建立存储 seam。

