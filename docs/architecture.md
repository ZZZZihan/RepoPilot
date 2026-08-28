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

两种适配器共享前四项选择限制，GitHub 适配器另有响应与时间限制。下列数值是默认配置，服务端可在 `Settings` 规定的边界内通过 `REPOPILOT_*` 环境变量调整：

- 最多查看 2,000 个 tree 条目（regular file、目录、符号链接和子模块都计数）；
- 最多选取 32 个证据文件；
- 单文件最多 64 KiB；
- 选取内容合计最多 384 KiB；
- GitHub 单响应最多 2 MiB；
- GitHub 每个请求默认 10 秒超时；
- 整次 GitHub 检查有独立的默认 30 秒 deadline；
- 只选 README、Python 项目/测试配置、`.py` 源码与测试；
- 不克隆、不导入、不执行仓库内容。

超过当前配置的文件树上限或 GitHub 返回截断树时，系统拒绝生成可能误导用户的计划。代表性文件选取达到较软的内容上限时，计划会记录 `inspection.selection_truncated=true` 风险。

## Evidence and plan schema

每条证据包含稳定 ID、仓库相对路径、类别、行范围和内容 SHA-256，但不持久化仓库
原文摘录。计划文件引用只能使用已存在的证据 ID；Pydantic runtime validator 还会校验
步骤顺序、证据图、文件动作以及状态/版本/审批记录的一致性。`inspect`、`modify` 和
`verify` 必须指向快照中已存在的路径，并由同路径证据支持；`create` 必须指向快照中
不存在的路径，且不能与任何同路径观测证据冲突。步骤种类和动作也必须匹配：analysis
只允许 `inspect`，implementation/test 只允许 `create` 或 `modify`，verification 只允许
`verify`。

`GET /v1/schemas/implementation-plan` 返回
`ImplementationPlan.model_json_schema(mode="validation")`：标准 JSON Schema 部分描述
可由普通 JSON Schema consumer 读取的结构约束；顶层
`x-repopilot-semantic-constraints` 只是 RepoPilot 的 runtime 语义清单。其
`enforced_by="pydantic-runtime"` 明确表示证据图、步骤/动作、验证声明/readiness、
repository identity 和计划状态等 custom validator 必须在 Pydantic 构造、持久化与
读取路径中另行执行，不能声称标准 JSON Schema 单独执行了这些语义。

当前 `PlanBuilder` 是确定性的。Issue 中出现的标识符会提高包含相同文本的源码和测试文件优先级。它可以给出可审查的第一份计划，但不等同于语义完整的代码理解。
当 Issue 没有仓库特定信号时，它会确定性选择已检查的常规 source/test 路径并写入
low-confidence 风险。词法器只把带目录的完整裸 token，或由反引号、引号、成对
括号/中文引号、Markdown label 完整界定的路径视为显式目标；根级文件必须被完整
界定，URL/URI token 与 Markdown destination 会被隔离。识别出的安全 source/test
路径若以无分隔 CJK 动作/定位前缀开头且可同时解释为真实路径（operand 可以包含
ASCII 或非 ASCII 字符），会 fail closed 为 `422 ambiguous_issue_path`；调用方必须
用空格分隔前缀与 operand，用 `@path` 紧凑标签显式指定 operand，或用
wrapper/`路径:` 显式界定完整 literal path。紧凑标签在长度上限附近保持替换后的
title/body 不增长，同时完整保留路径后的动作文本。
路径采用 fail-closed 解析：regular-file tree 中存在且已取证时 `MODIFY`；存在但未取证
时返回 `413 inspection_limit_exceeded`；不存在时还要检查完整 tree namespace。目标精确
位置若已是目录、符号链接或子模块，或任一祖先已是 regular file、符号链接或子模块，
返回 `422 conflicting_issue_path`，只有可实际创建的显式路径才生成 `CREATE`。没有显式
路径时，只有文件树本身确实没有对应类别才推断由合法 Python 标识符组成的常规
`CREATE`，并执行相同 namespace 检查，不会用新文件掩盖受界检查缺失。同一类别的
额外显式路径也必须先完成 missing/ambiguous evidence 审计；M0 仍只输出首目标，并把
其余路径记录为 deferred multi-file 风险。

验证意图只从 README、项目配置和测试配置证据推导，并始终记录
`executed=false`。README 中的严格命令声明可以形成 pytest、ruff 或 mypy 意图；
pytest 配置声明只有位于对应根级规范配置文件并使用该格式的合法 section 时才能形成
pytest 意图。依赖项名称、工作流中的未解析 YAML 文本、错误文件语法、单独的
ruff/mypy 配置段和否定语境中的命令提及不能形成可运行命令。
`verification_readiness="ready"` 至少需要一个有证据的 pytest 意图；否则为
`needs_human_input`。该字段只是规划完整度信号，不是 execution authority。

## Persistence and state

SQLite 保存整个版本化计划 JSON，同时保留可查询的身份、状态和版本列。写入前、
读出后和状态转换后都重新执行 `ImplementationPlan` runtime 校验。仓库身份要求 canonical
GitHub HTTPS URL 与 owner/name 精确一致、ref 使用受支持 grammar，tree SHA 是 40 或 64 位
小写十六进制对象 ID。`created_at` 与 `approved_at` 都必须带时区并规范化为 UTC，且批准
时间不得早于创建时间。

唯一合法转换为：

```text
proposed (version 1, approval=null)
  -- approve(expected_version=1) -->
approved (version 2, approval present)
```

这也是 `ImplementationPlan` 1.0 唯一允许的两个状态/版本 envelope：不能构造其他
proposed/approved 版本组合，`approval.from_version` 必须为 1。转换使用 SQLite
`BEGIN IMMEDIATE` 和条件更新。过期版本与重复转换均返回冲突，不会悄悄覆盖状态。
Issue 编号、计划版本、`expected_version` 与 `approval.from_version` 都是严格 JSON 整数；
布尔值、数字字符串和浮点数不会被归一为整数。直接存储调用也在开启事务前执行相同的
正整数检查，因此无效乐观锁 token 不会改变计划状态。

读取时不仅验证 JSON 文档；SQL 行的 `plan_id`、`schema_version`、`status` 和 `version`
还必须与文档内对应字段逐项相同。初始化要求 `table_xinfo`、canonical raw DDL、外键清单
和 schema object 清单精确匹配；除主键 autoindex 外，不接受附加 index、UNIQUE、trigger、
generated/hidden column 或额外 constraint。可回滚 probe 还会执行一次与生产相同的
`proposed/version 1` 到 `approved/version 2` 条件 UPDATE，并验证非法状态和 version 0 被拒绝。
任一关系行、document envelope 或 schema 漂移都作为存储损坏 fail closed。

该转换只改变 planning-only 记录的审查状态。它不改变 `verification_readiness`，不生成
execution seal，也不会使 `ImplementationPlan` 1.0 成为执行或发布 Interface 的合法输入。

### POSIX storage hardening

在 POSIX 上，`SQLitePlanStore` 在构造时把调用方路径冻结为规范化的绝对父目录目标，
因此相对路径、父目录 symlink alias、后续工作目录变化以及 alias 后续重新指向不会
重定向数据库。由该 Module 新建的数据库目录使用 `0700`；最终父目录必须是当前 UID
拥有的目录，且不得 group-writable 或 other-writable。

数据库以及 `-journal`、`-wal`、`-shm` sidecar 都先以 `lstat`/no-follow 元数据检查，
再用 `O_NOFOLLOW` 打开；打开前后和路径重查的 device/inode 必须与已打开 fd 一致。
这些文件必须是当前 effective UID 拥有、`nlink=1` 的普通文件，并被收紧和复核为
`0600`；symlink、hardlink、owner/type/mode 漂移或命名路径替换均 fail closed。初始化
还会检查 `PRAGMA journal_mode=WAL` 的实际返回值为 `wal`，不会把发送 PRAGMA 当作
已经进入 WAL 的证据。运行中的合法 SQLite sidecar 受到同一 owner/type/link/mode/inode
检查；可复现 smoke 在两次 Uvicorn 都已优雅停止后还要求三个 sidecar 全部不存在，
并复核数据库与 sidecar 的 absence/identity 没有在检查期间变化。post-stop smoke 还从
同一 no-follow fd 读取 SQLite magic 与 header bytes 18:20=`02 02` 来确认 WAL，然后以
`mode=ro&immutable=1`、`query_only`、1 秒 connection/busy timeout 运行
`integrity_check=ok`，并要求 `plans` 恰好一行 `approved/version 2`。这些 post-stop
检查不替代自动化测试对 frozen parent/path alias 等构造期负向契约的证明。临时 SQLite
数据库没有单独的 byte-size cap；当前固定工作负载及其 post-stop 检查受 snapshot
orchestrator 的 210 秒 deadline 约束。

该保证有明确范围：它是受限的 POSIX pathname/file hardening，不是通用抗竞态或
ACL sandbox。它不声称阻止恶意同 UID 进程在校验与 SQLite connect 之间主动
rename/swap；POSIX mode bits 不等同于完整 macOS ACL 判定；非 POSIX 平台也不提供
同等 owner、inode、link-count 和 sidecar 保证。

## Reproducible smoke identity chain

`make smoke-m0` 是验收 harness，不是产品执行能力。它使用两阶段身份链：

1. live bootstrap 验证实际 repository top-level，记录精确 `HEAD` commit 与
   `HEAD^{tree}`，并要求 tracked/untracked 状态均为空；
2. bootstrap 从该 commit 仅归档 smoke harness、`src/repopilot`、固定夹具和 `uv.lock`，
   物化受限源码 snapshot；绝对路径、Windows drive/backslash、父目录 traversal、
   duplicate/non-canonical path、链接/特殊文件或 allowlist 外 member 均 fail closed；
3. 内层 orchestrator 在启动前独立重新归档 claimed commit 并比对 manifest；Git 命令
   使用固定无凭据环境，运行子进程只接收固定最小环境，只从 snapshot 启动两个真实
   Uvicorn 直接子进程；
4. 完成创建、读取、结构 Schema、语义状态、批准、冲突与重启持久化验证后，harness
   重算 snapshot manifest，复核 live commit/tree/clean 身份与 SQLite identity，再输出
   唯一的脱敏 JSON Evidence Capsule，并清理其精确定义的受管边界。

受管进程边界在 Capsule 中固定命名为
`managed_direct_children_original_posix_process_group_and_observed_ports`。它保证回收受管
直接子进程、仍留在 snapshot orchestrator 原 POSIX process group 的成员，并验证已观测
loopback 端口关闭；该 group 超时或输出越界时执行有界 TERM/KILL 并要求原 group
为空。该边界不是 host-wide descendant containment：后代可以主动调用 `setsid()` 创建
新 session，harness 不声称阻止或回收这种脱离者。

运行子进程不继承调用方环境。固定 allowlist 只有 `LANG=C.UTF-8`、
`LC_ALL=C.UTF-8`、`PATH=os.defpath`、`PYTHONDONTWRITEBYTECODE=1`、
`PYTHONHASHSEED=0`、`PYTHONNOUSERSITE=1`、`PYTHONSAFEPATH=1`、`PYTHONUTF8=1`，再加入
指向 snapshot `src` 的 `PYTHONPATH`。因此 Git/Python context、token、凭据和 debug
override 不进入 child；Git metadata/archive 命令另用固定的 `GIT_CONFIG_GLOBAL=/dev/null`、
`GIT_CONFIG_NOSYSTEM=1`、`GIT_TERMINAL_PROMPT=0`、C.UTF-8 locale 与 `os.defpath`。
managed Python child 均以 `sys.executable -s`、argv、`shell=False` 启动，且
`REPOPILOT_GITHUB_TOKEN`、`GITHUB_TOKEN`、`GH_TOKEN` 都不在环境中。Uvicorn
stdout/stderr 直接丢弃，snapshot orchestrator 的输出采用 streaming bounded capture，
最终 stdout 只允许一份脱敏 Capsule。

### Fixed smoke resource ceilings

| Surface | Fixed ceiling |
| --- | --- |
| 每个 Git metadata/archive 命令 | 15 秒；stderr 256 KiB |
| Git metadata stdout | 1 MiB |
| Git archive stdout | 32 MiB |
| archive member 数 | 4,096（目录也计数） |
| 单个 snapshot regular file | 8 MiB |
| snapshot regular-file 总字节 | 24 MiB |
| snapshot secure-read chunk | 64 KiB |
| snapshot orchestrator captured stdout / stderr | 1 MiB / 256 KiB |
| 最终 pretty-printed Evidence Capsule | 1 MiB |
| snapshot orchestrator subprocess | 210 秒（不是整个 `make smoke-m0` 的总 deadline） |
| 单个 HTTP 请求 | 5 秒 |
| 每个 Uvicorn ready signal / health wait | 各 10 秒 |
| 每个 Uvicorn graceful / SIGTERM / SIGKILL / observed-port-close wait | 各 10 秒 |
| outer POSIX process-group TERM / KILL wait | 2 秒 / 5 秒 |

archive 由 harness 逐 member 手工提取，不调用通用 extract；每个 snapshot regular file
在 manifest 读取时都经过 `lstat`、`O_NOFOLLOW` 与 fd/path 重查，并要求 regular、
single-link 以及 device/inode/mode/nlink/size/mtime 在有界读取前后稳定。任何 member、
字节、输出或时间上限被触发都会 fail closed 并进入同一有界清理路径。

Capsule 的 source Git commit/tree 证明 RepoPilot harness 来源；计划内的
`repository.tree_sha` 证明 `FixedRootRepositoryInspector` adapter 看到的夹具快照。
这两个身份不能混用。固定根目录夹具只作为检查数据被读取，不会被 import 或执行。

## Cold installation and build identity

日常 `make sync` 不承担发布候选的 cold provenance 证明。cold local gate 与 hosted
`ci`/`check` job 使用同一两阶段安装：第一阶段在 official PyPI、无缓存/无配置/无
editable/无 source override 条件下执行
`uv sync --locked --all-groups --no-install-project`，先安装 `uv.lock` 中的 `hatchling`
及全部依赖而不安装项目；第二阶段增加 `--no-build-isolation` 再安装 RepoPilot。
质量门禁与 smoke 使用 `UV_NO_SYNC=1`，不会悄悄重做解析。随后
`uv build --no-build-isolation --no-sources --no-cache --no-config` 在同一锁定环境构建
sdist/wheel，因此不会另建隔离环境独立解析 build backend。hosted checkout 还必须显式
检出 PR head SHA、验证实际 `HEAD` 等于事件的 expected SHA、记录 tree 并要求 worktree
clean；最终 SHA/run/review 证据仍外置在 PR 与 Linear，且 M0 不自动 merge。
