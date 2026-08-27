# ADR 0001: Ship a bounded planning and approval slice first

- Status: accepted
- Date: 2026-08-24

## Context

长期方案包含仓库同步、Agent 编排、代码修改、测试、Docker、Trace 和 PR 发布。如果第一步同时实现这些能力，就无法区分计划质量、执行安全和发布权限问题，也会过早建立大量浅接口。

本次目标只要求小型 Python 仓库的第一个 FastAPI 纵向切片，并明确排除执行、任意 Shell、Docker 与 PR 创建。

## Decision

1. HTTP 只接受 `github.com` HTTPS 仓库 URL 和调用方提供的 Issue 内容。
2. 使用 GitHub tree/blob REST 端点只读检查仓库，不 clone，不下载归档，不执行内容。
3. 仓库检查有硬性文件、字节、响应和超时上限；超出小仓库边界时明确失败。
4. 生产 GitHub 适配器和固定根目录夹具适配器实现同一个 `RepositoryInspector` 接口，使该 seam 同时具备真实实现和测试实现。
5. 第一版使用确定性规划器。计划的价值来自可复现结构和证据约束，而不是未经验证的模型输出。
6. SQLite 是本地权威存储。计划在构造、持久化和读取时执行 Pydantic runtime 校验；
   Schema endpoint 暴露结构性 JSON Schema 和 `x-repopilot-semantic-constraints` 清单，
   但标准 JSON Schema 本身不执行 custom evidence/state validator。SQL 行的 plan ID、
   Schema version、status/version 必须与文档 envelope 一致。
7. canonical repository URL/owner/name/ref/tree 形成一个身份；创建与批准时间必须带时区、
   规范化为 UTC，批准不能早于创建。
8. 状态机只有无审批的 `proposed/version 1 → approved/version 2/from_version 1`；转换要求
   调用方提供正确期望版本，其他 status/version/approval 组合均非法。
9. 不建立执行路由。批准和 `verification_readiness` 只是 planning-only 信号；当前
   `ImplementationPlan` 1.0 即使 approved/ready 也必须被未来执行与 PR 发布请求拒绝。
   后续执行需要新的 ADR、execution-sealed 类型和真实 adapter。

## Consequences

该切片可以在没有 GitHub 写权限、Docker 和模型密钥的情况下端到端验收，失败也能归因到输入、检查、规划、持久化或审批中的一个阶段。

代价是计划仍是启发式初稿；当前没有认证，`approved_by` 只是声明；SQLite 也不是多用户
生产数据库。POSIX no-follow/owner/link/mode/inode/sidecar 门禁是静态路径 hardening，
不声称防住恶意 same-UID rename/swap、完整 macOS ACL 或 non-POSIX 等价保证。这些限制
必须在进入执行阶段前解决，不能因为接口已经存在而视为完成。
