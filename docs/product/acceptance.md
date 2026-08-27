# First vertical-slice acceptance

## Automated acceptance

`make check` 必须重复证明：

- 格式、Lint 和严格类型检查通过；
- FastAPI 创建计划返回 `201 proposed/version 1`；
- 最小 Python 夹具的 README、项目配置、测试配置、源码和测试均形成证据；
- 每个步骤中的文件引用只引用计划内存在的证据 ID；
- 修改一个引用为未知证据后，Pydantic runtime validator 拒绝计划；
- Schema endpoint 返回结构性 JSON Schema 与
  `x-repopilot-semantic-constraints`（`enforced_by=pydantic-runtime`）清单；测试必须直接
  构造语义反例证明 runtime validator 生效，不能用标准 JSON Schema 响应代替该证据；
- `inspect`、`modify` 和 `verify` 只接受快照中存在且有同路径证据的文件；`create`
  只接受快照中不存在且没有同路径观测证据冲突的文件；
- analysis/implementation/test/verification 步骤只接受各自允许的 inspect、
  create-or-modify、create-or-modify、verify 动作组合；
- 验证意图只引用 README、项目配置或测试配置证据，且保持 `executed=false`；
- README 中的严格 pytest/ruff/mypy 命令和根级规范配置文件中的合法 pytest section
  按契约形成意图；依赖项名称、工作流中的未解析 YAML 文本、错误文件语法、ruff/mypy
  配置段本身、URL/普通 prose 以及否定语境中的命令提及不得生成伪命令；
- `verification_readiness="ready"` 至少需要一个有证据的 pytest 意图；ruff-only、
  mypy-only 或没有可运行测试意图必须为 `needs_human_input`，且该字段不能成为执行授权；
- inspected repository 的 canonical GitHub URL、owner/name、受支持 ref 与 40/64 位小写
  tree SHA 必须形成一致身份；创建与批准时间必须带时区、规范化为 UTC，批准不能早于创建；
- SQLite 中的计划可由新 FastAPI 实例重新读取；
- SQL 行的 plan ID、Schema version、status 与 version 必须和文档 envelope 精确匹配；
  `table_xinfo`、canonical raw DDL、外键和 schema object 清单必须精确匹配，附加
  constraint/index/UNIQUE/trigger/generated column 等 schema 漂移必须在启动时作为存储
  损坏 fail closed；envelope 篡改必须在读取或批准该记录时 fail closed；初始化 probe
  必须完成并回滚一次合法 `proposed/1 → approved/2` 转换；
- POSIX SQLite 路径在构造时规范化并冻结；相对路径、父目录 symlink alias、alias
  后续重定向和 `cwd` 变化不能改变最终数据库目标；
- POSIX 数据库及 `-journal`、`-wal`、`-shm` sidecar 必须经过 `lstat`、`O_NOFOLLOW`、
  owner=euid、regular、`nlink=1`、`0600` 以及 open 前后 path/fd device+inode 一致性检查；
  symlink/hardlink/替换输入 fail closed，WAL 返回值必须实际为 `wal`；
- 只有 `proposed/version 1/approval=null` 与从 version 1 得到的
  `approved/version 2/approval present` 合法；正确 `expected_version=1` 才能完成转换，
  `approval.from_version` 必须为 1；显式 `approved_at` 必须是真正的 `datetime`，只有
  `None` 才可请求生成当前 UTC 时间；
- Issue 编号和所有权威版本字段必须是严格 JSON 整数；`true`、数字字符串与浮点数返回
  `422` 或在直接存储边界 fail closed，且不得改变持久化状态；
- 过期版本和重复批准返回 `409`；
- Issue URL 与仓库不一致时返回 `422`；
- 非 GitHub 主机的仓库 URL 在网络请求前返回 `422`；
- 固定根目录适配器超过文件树上限时停止并报告 `413` 对应错误；
- GitHub 适配器只接受 `2xx`，将 `403` 配额耗尽、`429` 和超时映射为稳定错误，
  不跟随重定向，也不会把认证材料发送给非固定上游主机；
- metadata/tree/blob 响应、文件树、单 blob、内容总量和文件选取均受独立上限约束；
  截断或 malformed tree、大小不一致、无效 JSON/base64 与非 UTF-8 内容按契约失败
  或明确标记选取不完整；
- 整次检查超时或任一 blob 失败时，会取消并 drain 同级 blob 任务；并发读取仍受界；
- golden Issue 要求 `divide()` 在零除数时抛出
  `ValueError("divisor must not be zero")`，同时保持非零商并要求回归测试；夹具基线
  不得预先满足该精确契约；
- golden plan 的 implementation/test 路径必须分别精确为
  `src/tinycalc/calculator.py` 与 `tests/test_calculator.py`，分析和 evidence window 必须
  指向相关行为；噪声文件不能胜过显式路径/文件名/符号，变异 Issue 必须改变选取；
- 低信号 Issue 只能确定性回退到已检查的常规 source/test 路径并记录
  low-confidence 风险；
- Issue 中带目录的完整裸 token，或由反引号、引号、成对括号/中文引号、Markdown
  label 完整界定的安全 source/test 路径，视为显式目标；根级文件必须使用完整界定
  形式，URL/URI token 与 Markdown destination 不得成为目标；
- 无分隔 CJK 动作/定位前缀与 path 形成的二义 token（operand 可以包含 ASCII 或
  非 ASCII 字符，无论根文件或带目录路径）不得删除前缀或回退到其他文件，规划 API
  必须返回 `422 ambiguous_issue_path`，并提示分隔前缀与 operand 或完整界定 literal
  path；`@path` 紧凑标签必须在 title/body 长度上限处仍可原样回放，并保留同行
  动作后缀；带礼貌词/连接词的冒号标签必须保留精确 operand；
- 识别出的显式路径若已存在于 `tree/all_paths`，只有对应 document 已取证时才允许
  `MODIFY`；同类别其他文件的 evidence 不能代替它；
- 已存在的显式路径未取证时，规划 API 必须 fail closed 为 `413`、错误码
  `inspection_limit_exceeded`；tree 中不存在的显式路径必须按原路径生成 `CREATE`；
- 同类别多个显式路径必须全部完成 missing/ambiguous evidence 审计；M0 只输出首目标，
  但必须记录 deferred multi-file 风险，后续路径不得绕过 fail-closed 门禁；
- 没有显式路径时，只有文件树本身确实没有对应类别才允许推断常规 `CREATE` 路径；
  不得用推断的新文件掩盖受界检查缺失；
- smoke bootstrap 对实际 repository top-level、精确 commit/tree 和 clean 状态 fail
  closed；无关 clean repository、伪造 tree、环境污染和不安全 archive member 不能
  冒充候选来源；
- claimed commit 的独立 re-archive 必须与 snapshot manifest 一致；运行后 manifest
  漂移或 live commit/tree/clean 漂移必须把 Evidence Capsule 降级为失败；
- Git metadata stdout 以 1 MiB、archive stdout 以 32 MiB、所有 bounded subprocess
  stderr 以 256 KiB 为上限；每个 Git 命令 15 秒，archive 最多 4,096 members、单文件
  8 MiB、regular-file 总量 24 MiB，secure read 以 64 KiB chunk 进行；
- snapshot orchestrator 的 stdout/stderr 上限为 1 MiB/256 KiB、该 subprocess deadline
  为 210 秒（不是整个 `make smoke-m0` 的 deadline）；单 HTTP
  请求 5 秒，Uvicorn ready/health 各 10 秒，graceful/TERM/KILL/observed-port-close 各
  10 秒，snapshot orchestrator original-group TERM/KILL 各 2/5 秒；任一越界必须 fail closed；
- runtime child 只接收架构文档列出的固定最小环境与 snapshot `PYTHONPATH`，Git child
  只接收固定无凭据 Git 环境；Uvicorn stdout/stderr 不得进入 Evidence Capsule；
- Capsule 的进程边界必须精确为
  `managed_direct_children_original_posix_process_group_and_observed_ports`：直接子进程、
  仍在原 POSIX process group 的成员和已观测端口必须有界清理；超时/输出越界后原
  process group 必须为空。该 gate 不得声称阻止主动 `setsid()` 脱离的后代；
- OpenAPI 中不存在执行或 PR 路由。

## Manual smoke acceptance

1. bootstrap 验证实际 repository top-level，记录精确 `HEAD` commit/tree，并要求
   tracked/untracked 状态均为空；
2. 从该 commit 通过 Git archive 在固定 32 MiB/4,096 member/8 MiB 单文件/24 MiB
   总文件上限内物化受限源码 snapshot，拒绝不安全 member，记录版本化 manifest
   hash、文件数、harness hash 与 lockfile hash；
3. 内层 orchestrator 独立重物化 claimed commit 并匹配 manifest，使用固定无凭据 Git
   环境和固定最小 runtime child 环境，只从 snapshot 启动第一个真实 Uvicorn 进程；
4. `GET /healthz` 返回 `200`，创建、读取、结构 Schema 加语义清单、runtime 语义状态、
   批准和冲突路径通过；
5. 停止第一个进程，从同一 snapshot 和临时 SQLite 启动第二个进程，批准状态与版本
   在重启后保持可读；
6. SQLite 数据库通过 lstat/no-follow/euid/regular/nlink/`0600`/inode 门禁，优雅停止
   后三个 sidecar 均不存在；header magic 与 bytes 18:20=`02 02` 证明 WAL，再以
   `mode=ro&immutable=1`、query-only、1 秒 connection/busy timeout 验证 integrity `ok`
   和恰好一行 `approved/version 2`；检查前后数据库 inode 与 sidecar absence 不变，且
   snapshot manifest 与 live commit/tree/clean 身份保持不变；
7. `managed_direct_children_original_posix_process_group_and_observed_ports` 边界、临时
   数据库和 snapshot 均完成有界清理，随后只输出脱敏 JSON Evidence Capsule；这不
   扩张为对主动 `setsid()` 逃逸者的保证。

可复现 smoke 使用固定根目录夹具作为只读检查数据，因此不会访问 GitHub，也不会
import 或执行夹具中的仓库代码。它运行的是 snapshot 中的 RepoPilot harness：

```bash
make smoke-m0
```

Capsule 的 source Git commit/tree 证明 harness 来源；计划中的 `repository.tree_sha`
证明固定根目录夹具快照，两者不能混用。该 JSON Evidence Capsule 不等价于实时
GitHub adapter 验收；若要更新实时验收记录，必须另行记录日期、仓库、ref 与 tree SHA。

实时 GitHub smoke 会消耗外部 API 配额，并受仓库状态与网络影响；自动化 CI 不依赖它。任何实时验证结果都必须单独记录日期、仓库/ref 和 tree SHA。

### Historical live-GitHub result — non-gating

On 2026-08-24, a real Uvicorn process used the production GitHub adapter against `pallets/markupsafe` `main`, tree `b2e4d9c7687be25695fffbe93a37622302b24fb1`. Creation returned a bounded, evidence-backed proposed plan; approval returned version 2; a process restart preserved and returned the approved record. The run made only GitHub metadata/tree/blob reads.

This historical run predates the final M0 integration commit. It demonstrates the production
read-only adapter path, but it is not a release gate and does not replace the reproducible
fixed-root Evidence Capsule or hosted CI on the final commit.

## Publication acceptance

M0 发布候选必须在一个干净、不可变的最终 commit 上依次通过 cold local gate、上述
snapshot smoke 和 security diff review。cold gate 与 hosted CI 必须先以
`uv sync --locked --all-groups --no-install-project` 安装锁定 backend/依赖，再以
`uv sync --locked --all-groups --no-build-isolation` 安装项目；sdist/wheel 必须使用
`uv build --no-build-isolation`，不得另行解析 build backend。PR、Linear Evidence Capsule 与 hosted GitHub
Actions run 必须全部引用同一个精确 final SHA；仅存在 workflow 文件或已启动 run 都不
算 hosted evidence，必须实际观察该 SHA 的 required checks 为 green。发布只创建供人
审查的 PR，不自动 merge。

## Deferred acceptance

本切片不把以下项目视为产品 API 或规划运行时已实现的能力：修改或执行被检查
仓库代码/测试、用户可控 Shell、Docker、由产品创建 Git commit/branch/PR、认证、
并发多用户隔离、PostgreSQL、模型规划和部署。approved/ready 的
`ImplementationPlan` 1.0、execution-sealed 类型、执行 adapter 和 publisher adapter
也均不在当前已验收能力中。

POSIX 存储门禁不声称防御恶意同 UID 进程在校验与 SQLite connect 间主动
rename/swap，也不提供完整 macOS ACL 判定或 non-POSIX 等价的 inode/link 保证。
smoke 的 process-group 门禁也不声称捕获主动调用 `setsid()` 脱离原 session/group 的后代。
