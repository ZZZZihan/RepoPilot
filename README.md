# RepoPilot

RepoPilot 的第一个端到端切片已经落到一个可运行的 FastAPI 后端：提交 GitHub 仓库地址和 Issue 内容后，系统通过受限的只读适配器检查小型 Python 仓库，生成带文件证据的结构化实施计划，将计划持久化到 SQLite，并等待显式人工批准。

当前切片故意止步于批准。它不会克隆仓库、应用 Patch、运行仓库命令、访问宿主 Shell、创建 Docker 沙箱、提交代码或创建 Pull Request。

批准只是当前规划记录的审查状态，不会把 `ImplementationPlan` 1.0 转换为执行或
发布授权。即使计划同时为 `approved` 且 `verification_readiness="ready"`，当前
planning-only 类型仍不能
进入执行或 PR 发布请求；后续必须通过独立 ADR 定义并引入 execution-sealed 计划类型。

## 当前链路

```text
GitHub repository URL + supplied Issue
                    ↓
fixed-host, bounded GitHub REST inspection
                    ↓
README / project config / test config / Python files
                    ↓
schema-validated evidence graph and implementation plan
                    ↓
authoritative SQLite plan record (proposed, version 1)
                    ↓
explicit approval with expected_version
                    ↓
approved plan record (approved, version 2)
```

仓库输入只能是 `https://github.com/OWNER/REPO`。服务端固定访问 `api.github.com`，不会使用请求中的任意主机；GitHub token 也只能来自服务端环境变量。仓库检查存在文件数、响应大小、单文件大小、总字节数、超时和选取文件数上限。

计划由当前的确定性规划器生成。它优先使用 Issue 中的显式路径、文件名和函数
符号，再使用经过停用词过滤的普通词，为相关 Python 源码和测试排序，但不会调用
外部模型；每个计划文件引用必须关联一个存在于计划中的证据 ID。当前 M0 每类只
选择一个最强 source/test 路径，仍需人工审查，不代表通用程序语义理解。完整计划
会在生成、写入数据库以及从数据库读出时分别经过同一个 Pydantic runtime model
校验。`GET /v1/schemas/implementation-plan` 暴露标准的结构性 JSON Schema，以及
`x-repopilot-semantic-constraints` 扩展清单；证据图、动作、readiness 和状态机等语义
仍由 Pydantic runtime validator 单独执行，不能把标准 JSON Schema 本身说成会执行
全部语义约束。
若 Issue 没有仓库特定信号，规划器会确定性选择已检查的常规 source/test 路径并
记录 low-confidence 风险。显式引用可以是带目录的完整裸 token，或由反引号、引号、
成对括号/中文引号、Markdown label 完整界定的路径；仓库根文件必须使用后一种形式。
URL/URI token 与 Markdown destination 永远不是仓库目标。若这种安全的 source/test
路径以无分隔 CJK 动作/定位前缀开头（例如 `创建new.py`、`请修改功能/模块.py` 或
`请于src/pkg/a.py中修改`），规划器不会猜测前缀是否属于真实路径，而会返回
`422 ambiguous_issue_path`；可用空格分隔前缀与 operand，或用
`路径:创建new.py` 明确完整 literal path。`@new.py` 是紧凑的显式 operand
标签；当增加空格会超过 Issue title/body 长度上限时，错误响应会给出这种
不增加字段长度的可回放形式。冒号形式同样接受礼貌词和连接词，例如
`请修改:src/pkg/a.py` 与 `并创建:src/pkg/new.py`。
路径已存在于 `tree/all_paths`，规划器只有在取得该路径证据后才能生成 `MODIFY`；
否则请求会 fail closed 为 `413 inspection_limit_exceeded`。若该显式路径不在 tree 中，
则按 Issue 原路径生成 `CREATE`。没有显式路径时，只有文件树本身确实没有对应类别
才会推断常规 `CREATE` 路径；受界检查缺失不能伪装成新文件。同类多路径都会先
完成 missing/ambiguous evidence 审计，但 M0 只输出首目标并记录 deferred multi-file 风险。

文件引用还受 Schema 级动作不变量约束：`inspect`、`modify` 和 `verify` 必须指向快照
中已存在的路径，并由同路径证据支持；`create` 必须指向快照中不存在的路径，不能与
任何同路径观测证据冲突。

验证意图只从 README、项目配置或测试配置中的仓库证据推导，并始终保持
`executed=false`。规划器不会发明可运行命令：README 中的严格命令声明可以形成
pytest、ruff 或 mypy 意图；只有位于对应根级规范配置文件中的合法 pytest section
才能形成 pytest 配置意图。依赖项名称、工作流中的未解析 YAML 文本、错误文件语法、
单独出现的 ruff/mypy 配置段或否定语境中的命令提及都不能冒充命令。
只有至少一个有证据的 pytest 意图才能得到
`verification_readiness="ready"`；ruff-only、mypy-only 或没有可运行测试意图都会
保持 `needs_human_input`。verification readiness 只是规划完整度信号，不是执行许可。

## 本地运行

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。依赖由 `uv.lock` 固定。

```bash
make sync
cp .env.example .env
uv run --env-file .env uvicorn repopilot.api:app --reload
```

打开 <http://127.0.0.1:8000/docs> 查看交互式接口文档。公开仓库不需要 token；读取私有仓库时，在未提交的 `.env` 中设置只读的 `REPOPILOT_GITHUB_TOKEN`。

创建计划：

```bash
curl --fail-with-body \
  --request POST http://127.0.0.1:8000/v1/plans \
  --header 'Content-Type: application/json' \
  --data '{
    "repository": {
      "url": "https://github.com/OWNER/REPOSITORY",
      "ref": "main"
    },
    "issue": {
      "number": 17,
      "url": "https://github.com/OWNER/REPOSITORY/issues/17",
      "title": "Give divide() an explicit zero-divisor error",
      "body": "In calculator.py, make divide() raise ValueError(\"divisor must not be zero\") when divisor is zero. Preserve non-zero quotients and add a regression test in test_calculator.py."
    }
  }'
```

`issue.title` 和 `issue.body` 是本次请求提供的权威输入；当前切片不会再向 GitHub 获取或认证 Issue。若提交 Issue URL，它必须属于同一个仓库，且编号必须一致。

批准计划：

```bash
curl --fail-with-body \
  --request POST http://127.0.0.1:8000/v1/plans/PLAN_ID/approval \
  --header 'Content-Type: application/json' \
  --data '{"approved_by":"local-reviewer","expected_version":1}'
```

`approved_by` 目前只是本地审计标签，不是经过认证的身份。重复批准或过期的
`expected_version` 会返回 `409`；批准不会触发后续执行、改变 `verification_readiness`
或生成 execution seal。Issue 编号与 `expected_version` 必须使用 JSON 整数；布尔值、
数字字符串和浮点数不会被转换为整数，而是返回 `422`。

## HTTP 接口

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| `GET` | `/healthz` | 检查进程和 SQLite 可用性 |
| `POST` | `/v1/plans` | 检查仓库并持久化 `proposed` 计划 |
| `GET` | `/v1/plans/{plan_id}` | 读取权威计划记录 |
| `POST` | `/v1/plans/{plan_id}/approval` | 执行 `proposed → approved` 转换 |
| `GET` | `/v1/schemas/implementation-plan` | 返回结构性 JSON Schema 与 runtime 语义约束清单 |

不存在执行、Shell、沙箱或 PR 路由。

## 验证

```bash
make check
make smoke-m0
```

`make check` 依次检查格式、Lint、类型并运行自动化测试。`make smoke-m0` 先把干净
checkout 的精确 Git commit/tree 物化为受限源码 snapshot，并独立核对版本化 manifest；
随后只从该 snapshot 启动两个真实 Uvicorn 子进程，验证 HTTP 创建、读取、审批冲突、
进程重启和 SQLite 完整性/WAL/POSIX 权限。它的进程边界精确记录为
`managed_direct_children_original_posix_process_group_and_observed_ports`：覆盖受管直接
子进程、仍留在原 POSIX process group 的成员和运行时观测到的端口；不声称阻止主动
调用 `setsid()` 新建 session 的逃逸后代。运行后还会重算 manifest、复核 live checkout
身份，再输出脱敏 JSON Evidence Capsule。固定根目录夹具只作为
`RepositoryInspector` adapter 的数据输入，不会被 import 或执行。GitHub 生产 adapter
另有模拟 REST 契约测试；可复现 smoke 不依赖实时网络。该 smoke 要求 POSIX process
group 与 `O_NOFOLLOW` 支持。完整资源上限、固定最小子进程环境与 SQLite
no-follow/inode/sidecar 门禁见[当前架构](docs/architecture.md)。

日常 `make sync` 是开发便利命令，不是发布候选的 cold-install 证据。cold local gate 与
GitHub Actions 都先以 `uv sync --locked --all-groups --no-install-project ...` 安装锁定的
`hatchling` 及依赖，再以第二次 `uv sync --locked --all-groups --no-build-isolation ...`
安装项目；分发包也以 `uv build --no-build-isolation ...` 构建。这样 build backend 复用
锁定环境，不再创建一个独立解析 backend 的隔离构建环境；完整固定参数见
[开发流程与工具路线](docs/development-workflow.md)。

历史 checkpoint `61641fe` 曾通过 37/37 自动化测试和双进程 smoke；该结果不代表
任何后续 HEAD。每个发布 HEAD 的精确 SHA、本地 cold gate、two-stage snapshot smoke 和 hosted
CI 证据都存入 PR 与关联的 Linear Evidence Capsule；hosted checks 必须实际运行在同一
SHA 上并 observed green。PR 只供人工审查，不自动 merge；仓库正文不自引用
“当前发布 SHA”。

## 项目文档

- [产品上下文](CONTEXT.md)
- [验收标准](docs/product/acceptance.md)
- [开发状态与证据](docs/product/development-status.md)
- [开发流程与工具路线](docs/development-workflow.md)
- [Linear 项目与首批工单记录](docs/linear-bootstrap.md)
- [当前架构](docs/architecture.md)
- [ADR 0001：第一个规划切片](docs/adr/0001-planning-vertical-slice.md)
- [后续执行与 PR 接口](docs/contracts/future-stages.md)
- [长期愿景与早期项目方案（非当前执行路线）](docs/project-proposal.md)
