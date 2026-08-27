# RepoPilot development workflow

Status: M0 delivery contract; live release state is tracked in GitHub and Linear

Last reviewed: 2026-08-27

## 1. Outcome

RepoPilot 的首要目标不是立即建设完整平台，而是用可证伪的纵向切片证明：

> 一个冻结的 Python 仓库快照和一份 Issue，经过证据计划、显式批准、
> 受控修改和客观验证后，可以产生一个可供人审查的 Verified Patch
> Candidate。

第一轮开发采用四个闸门：

1. M0 Planning Baseline Sealed
2. M1a Execution Contract Proven
3. M1b First Verified Patch Candidate
4. M2 Pilot Go / Narrow / Stop Decision

M2 得出 Go 结论以前，不建设完整 Web 控制台、GitHub 写入闭环、多用户
数据库、生产监控或公开部署。

## 2. Evidence model and historical checkpoints

### Durable repository evidence

- 恢复基线 e54d5ba4b2ccdca8a4562588476d009d88ced763 保存了 37 文件的 Planning
  Slice；这是来源 checkpoint，不代表实时分支或发布状态。
- 该恢复 checkpoint 的干净检出曾用 CPython 3.12.12、uv 0.10.0 完成锁定安装和
  `make check`：Ruff、严格 MyPy 以及 6/6 Pytest 全部通过。
- 历史 M0-02 checkpoint 已确认 `httpx2` 是 Starlette 1.6 `TestClient` 的直接测试后端，不应
  删除；依赖来源已迁移为仓库内显式的官方 PyPI 配置，未使用的
  `pytest-cov`/`coverage` 已删除，GitHub adapter 负向路径已经补齐。精确提交
  `26942c776a8eceeb0e757f56946ce5f3e87787cf` 在全新 worktree 完成官方源、无缓存冷安装、
  34/34 测试和构建。
- 独立语义审计发现原 golden Issue 已被夹具预先满足。COL-9 已把目标改为
  尚未满足且可观察的 `ValueError` 契约，收紧文件排序与 evidence window，
  并加入噪声文件和变异 Issue 测试。
- 集成候选在精确提交 `61641fe43036d7dcde4b945f4d692fd4e4e1dbc8` 上通过 Ruff、严格
  MyPy、37/37 Pytest，以及两个真实 Uvicorn 子进程的创建、审批、冲突、重启读取和
  SQLite 清理 smoke；
  该 checkpoint 不替代任何后续 HEAD 的发布证据。
- Linear 项目
  [RepoPilot — Verified Issue-to-PR Pilot](https://linear.app/colife/project/repopilot-verified-issue-to-pr-pilot-bf73022b2c42)
  保存实时目标、里程碑、工单状态与依赖；GitHub 保存实时分支、SHA、PR、review
  与 Actions run。仓库文档只保存契约和明确标注的历史 checkpoint。
- 每个发布候选的精确 HEAD、本地 cold gate、two-stage snapshot smoke、security diff、
  hosted run 与 review 证据必须存入 PR 和关联的 Linear Evidence Capsule；缺少这些
  外部记录即不能视为已验收。
- 本次 M0 恢复的声明安全审查/PR 基线是完整提交
  `a3469d43430f8d276174dc25969102aeb33a328b`；最终外部证据必须同时记录该
  `security_base_sha` 与候选的完整 `candidate_head_sha`，覆盖二者之间的整个差异。
- 外部工具的账号、连接器和项目清单在相关阶段实时检查，不在仓库文档中复制为
  持续有效的状态。

### Inference

- 历史证据支持沿用并持续硬化该基线，而不是从零重写；每个发布候选仍须完成
  自身的 cold gate、PR 安全审查和 hosted CI。
- RepoPilot 在 M0 至 M2 不需要托管 Postgres；SQLite 和版本化运行工件足以
  验证核心产品假设。
- Vercel Sandbox 与 RepoPilot 的不可信代码执行场景高度匹配，但在通过
  威胁模型、网络、凭据、资源限制和清理验收前，只能视作 M1a 候选。

### Candidate-specific evidence and future decisions

- 任一发布候选只有在 PR 与 Linear Evidence Capsule 记录其精确 HEAD 的全新无缓存
  cold gate、two-stage snapshot smoke、security diff、hosted GitHub Actions run 和 review
  后，才满足发布证据契约。
- Vercel Sandbox 是否满足 RepoPilot 最终的默认断网、命令策略、Python
  版本、资源限制和工件取回要求。
- M2 后应选择 Neon Postgres 还是 Supabase；该决定取决于是否需要
  Supabase Auth、Storage 和 Realtime。

## 3. Sources of truth

| Information | Authoritative source | Tooling |
| --- | --- | --- |
| Goal, milestone, issue, priority, status, dependency | Linear | Linear connector and linear skill |
| Branch, commit, diff, PR, review, CI | GitHub | Git, gh, GitHub Actions |
| Domain language, architecture, contract, acceptance | Repository documents | CONTEXT, ADRs, acceptance documents |
| One Codex task's temporary working steps | Codex plan | update_plan |
| Execution inputs, outputs and machine evidence | Evidence Capsule | Versioned manifest and bounded artifacts |

Linear must link to GitHub evidence instead of manually duplicating CI state. Chat history is
not a durable project-management source.

## 4. Working flow

The canonical delivery flow is:

~~~text
Linear issue
  -> triage and Agent Brief
  -> inspect repository evidence
  -> all-plan or direct implementation plan
  -> Git branch
  -> apply_patch
  -> targeted checks
  -> make check
  -> security review when triggered
  -> GitHub PR and CI
  -> evidence links back to Linear
  -> issue completed
~~~

The product execution flow is:

~~~text
Anchor -> Inspect -> Propose -> Seal -> Execute -> Prove -> Publish
~~~

## 5. M0 — Planning Baseline Sealed

### Question answered

Can the existing planning-and-approval slice be recovered, reproduced and delivered as a
clean GitHub baseline?

### Default tools

- Linear: milestone, issues, dependency graph and status.
- triage: durable Agent Briefs with testable acceptance and explicit non-goals.
- Git: isolated recovery branch and recovery commit.
- rg and rg --files: read-only discovery and inventory.
- apply_patch: small, reviewable changes only.
- uv: locked Python 3.12 environment.
- Ruff, strict MyPy and Pytest: automated quality layers.
- make check: one local CI contract.
- Git archive and a versioned manifest: materialize and independently verify the exact clean
  source commit/tree used by the acceptance harness.
- Uvicorn and an HTTP client: two snapshot-only processes for
  create/read/approve/restart/read smoke.
- security-diff-scan: baseline PR security review.
- GitHub Actions and gh: remote CI and PR evidence.

### Candidate cold-install and build contract

日常 `make sync` 只服务开发循环。M0 候选的全新本地环境和 hosted `ci`/`check` job 必须
使用同一顺序（output directory 可以是各自的新临时目录）：

~~~bash
uv sync \
  --locked \
  --all-groups \
  --no-install-project \
  --no-cache \
  --no-config \
  --no-editable \
  --no-sources \
  --default-index https://pypi.org/simple
uv sync \
  --locked \
  --all-groups \
  --no-build-isolation \
  --no-cache \
  --no-config \
  --no-editable \
  --no-sources \
  --default-index https://pypi.org/simple
UV_NO_SYNC=1 make check
UV_NO_SYNC=1 make smoke-m0
uv build \
  --no-build-isolation \
  --no-sources \
  --no-cache \
  --no-config \
  --python 3.12.12 \
  --default-index https://pypi.org/simple \
  --out-dir <fresh-temporary-directory>
~~~

第一阶段不安装 RepoPilot 本身，但通过 `--all-groups` 从 `uv.lock` 安装固定的
`hatchling` 及其依赖；第二阶段以 `--no-build-isolation` 使用这个锁定 backend 安装
项目。分发包继续复用同一环境，因此不会由临时隔离 build environment 再独立解析
backend。`UV_NO_SYNC=1` 保证质量与 smoke gate 不在执行时隐式改变该安装。

### Exit criteria

- The candidate tree is preserved by a named recovery commit and branch.
- main is not overwritten during recovery.
- Dependency provenance and secrets are reviewed.
- A clean checkout can install from the lockfile.
- make check passes and records the test count and tool versions.
- A two-stage Git-archive snapshot smoke binds two real Uvicorn processes, the manifest and
  SQLite persistence evidence to the exact clean commit/tree.
- Its exact managed boundary is
  `managed_direct_children_original_posix_process_group_and_observed_ports`; it covers managed
  direct children, members that remain in the snapshot orchestrator's original POSIX process
  group and observed ports, but not a descendant that deliberately escapes with `setsid()`.
- Git/archive/member/file/total/output/read/time caps, the exact minimal child/Git environments
  and the SQLite lstat/no-follow/inode/sidecar checks match
  [Current architecture](architecture.md) and [acceptance](product/acceptance.md).
- POSIX SQLite lstat/no-follow/owner/type/link/mode/inode/sidecar and WAL gates pass within the
  documented same-UID/ACL/non-POSIX threat boundary.
- The documentation distinguishes local, live-GitHub and hosted-CI evidence.
- The branch is pushed through a human-reviewable PR.
- GitHub Actions is observed green.
- The final worktree is clean; the recovery SHA and final commit SHA are recorded in the PR and
  linked Linear Evidence Capsule rather than self-recorded in the candidate tree.

M0 approval and `verification_readiness` remain planning signals only.
`verification_readiness="ready"` requires an evidence-backed pytest intent;
`needs_human_input` remains a valid non-ready planning result, all verification intents remain
unexecuted, and
`ImplementationPlan` 1.0 cannot cross the future execution/publication seam. M1a must introduce
the distinct execution-sealed type and ADR before any execution adapter can exist.

### Out of scope

Model-driven patches, arbitrary shell, untrusted repository execution, Docker claims,
GitHub write tokens, PR creation by the product, frontend, hosted database and deployment.

## 6. M1a — Execution Contract Proven

### Question answered

Can a sealed plan drive a deterministic patch through an isolated execution pipeline
without model nondeterminism?

### Default tools and skills

- all-plan: cross-module execution plan.
- domain-modeling: canonical execution vocabulary.
- codebase-design: deep interfaces for policy, execution and artifacts.
- ADR: execution authority and hard-to-reverse security decisions.
- codex-security threat model: trust boundaries and abuse paths before execution exists.
- Deterministic Patch Producer: supplied patch fixture, not a model.
- Structured path and argv policy: no free-form host shell.
- Clean temporary workspace or approved sandbox adapter.
- Pytest negative tests for traversal, symlink escape, timeout, drift and cleanup.
- Evidence Capsule: immutable manifest plus bounded artifacts.
- security-scan: milestone-level security review.

### Seal

Execution authority is bound to:

~~~text
issue revision
+ repository snapshot SHA
+ plan hash
+ approved capability and path scope
~~~

Any drift invalidates approval. An approved planning-only v1 record is not execution
authority and must fail closed.

### Vercel Sandbox spike

Vercel Sandbox is the first hosted candidate because it is designed for isolated,
ephemeral execution of untrusted or generated code and provides Python and JavaScript
SDKs. It is introduced behind a SandboxRunner seam and must prove:

- fixed Python runtime and reproducible dependency installation;
- bounded CPU, memory and wall-clock time;
- explicit network policy;
- no GitHub, database or deployment secret inside the guest;
- path and command allowlists;
- Patch, stdout, stderr and test-report retrieval;
- unconditional stop and cleanup;
- stable failure classification.

Failure of any hard security criterion keeps Vercel Sandbox out of the default path and
triggers evaluation of another adapter. A successful SDK call alone is not acceptance.

### Exit criteria

- Unapproved, stale, old-schema or snapshot-drifted plans cannot execute.
- Absolute paths, parent traversal and symlink escape are rejected.
- Changed paths are a subset of the sealed scope.
- Only structured, approved verification commands execute.
- Every run starts from a clean immutable snapshot.
- Timeout terminates execution and cleanup is recorded.
- The host repository remains unchanged.
- Success and failure both produce complete machine-readable evidence.

## 7. M1b — First Verified Patch Candidate

### Question answered

Can one model complete one genuine, bounded Python issue using the proven contract?

### Default tools and skills

- openai-docs before choosing the model and API contract.
- One model and one deterministic state machine.
- Four model-visible tools only:
  - search_code
  - read_file
  - apply_patch
  - run_tests
- Initial patch plus at most one repair.
- Trace fields for model, prompt version, tool call, token use, timeout and stop reason.
- Cold Verifier with independent context.
- security-diff-scan on the candidate patch.

### Verified Patch Candidate

A candidate is verified only when:

- snapshot lineage matches the seal;
- the Patch applies successfully;
- baseline behavior is understood;
- import, regression tests and hidden acceptance pass;
- changed paths satisfy policy;
- the Evidence Capsule is complete;
- independent verification finds no blocking mismatch.

Verified does not mean applied to the user's repository, committed, pushed or published.

## 8. M2 — Pilot Go / Narrow / Stop Decision

### Question answered

Does RepoPilot perform well enough on a frozen ten-task corpus to justify productization?

### Default tools

- Git-versioned seeded tasks and hidden acceptance.
- Eval runner producing JSON, JSONL and CSV.
- Pytest and import checks for objective success.
- Baseline A: one-shot Patch from Issue and supplied context.
- Baseline B: Agent loop without retrieval and structured planning.
- RepoPilot: retrieval, plan, seal, bounded tools and verification.
- Exa for external technical research and source discovery, never as a model-visible
  runtime tool during a code task.
- Spreadsheets only for analysis and presentation; canonical results remain versioned
  machine-readable data.

### Metrics

- Task success rate
- Hidden-test pass rate
- Regression-test pass rate
- Valid tool-call rate
- Patch-apply success rate
- Unsafe-operation block rate
- Changed-path precision
- Repair attempts
- Human intervention rate
- P50 and P95 latency
- Token and execution cost
- Failure category

### Decision threshold

| Result | Decision |
| --- | --- |
| 5–10 successes | Go: continue productization |
| 2–4 successes | Narrow: reduce supported task scope and analyze failure classes |
| 0–1 successes | Stop/Redesign: revisit tools, task design and assumptions |

The ten tasks and success definition are frozen before the first run.

## 9. Post-pilot platform tools

### Hosted Postgres decision

No hosted database is used before M2.

After a Go decision:

- Prefer Neon Postgres when RepoPilot remains a Python/FastAPI service that primarily
  needs managed Postgres, pooled connections and database branches.
- Prefer Supabase when the next slice explicitly needs a bundled web backend with Auth,
  Storage, Realtime and Data APIs.
- Do not use Supabase and Neon as parallel authoritative databases.
- Run the same migration, transaction, restart and recovery acceptance against the
  selected provider before replacing SQLite.

If Supabase is selected, every exposed table requires an explicit Data API grant model,
RLS policies and security review; service-role credentials never enter a browser or
sandbox. If Neon is selected, preview branches use schema-only or scrubbed data unless
production-data copying is explicitly approved.

### Vercel

- M1a: candidate hosted Sandbox adapter.
- Post-M2: preview deployments for a future Web console.
- Production is promoted only from an already verified preview artifact.
- RepoPilot's long-running execution semantics are not silently forced into a frontend
  serverless function model.

### Datadog

Datadog is introduced after a deployed pilot has real traffic. Internal Evidence Capsule
fields remain vendor-neutral; Datadog receives selected operational spans for:

- model inference and tool-call latency;
- errors and failure classes;
- tokens and cost;
- service, environment and version correlation;
- dashboards and alerts.

Raw repository contents, source patches, secrets and full prompts are not exported by
default. Datadog is outside the M0 delivery gate; its connector and authorization state are
verified live only when a deployed observability stage begins.

### Devpost

Devpost is a release and presentation track, not part of the product runtime. It begins
only when RepoPilot has a reproducible demo.

The supported sequence is:

~~~text
$find-hackathon
-> $start-hackathon
-> $review-hackathon-rules
-> $resources
-> $prepare-submission
-> $submit-project
~~~

The optional guided build track is:

~~~text
$build-onboard
-> $build-scope
-> $build-prd
-> $build-spec
-> $build-checklist
-> $build-project
~~~

Official event facts come from Devpost, and submission always requires explicit final
confirmation.

## 10. Tool governance

Every default tool must document:

- trigger;
- required permissions;
- expected output;
- acceptance method;
- unique benefit;
- added time and cost.

A tool leaves the default path when ten representative uses produce neither a unique
finding nor a meaningful cycle-time improvement.

Multi-agent work follows:

~~~text
parallel independent reading
-> one primary writer
-> independent verifier
~~~

Agents do not concurrently edit the same files. External writes, deployments, database
creation and submissions require the relevant stage and explicit authority.

## 11. Publication handoff contract

For each candidate head, complete the exact two-stage cold local gate above, two-stage snapshot
smoke and security diff, publish it through a reviewable PR, and observe hosted GitHub Actions
after it explicitly checks out the PR head SHA, verifies actual `HEAD` equals the event SHA,
records `HEAD^{tree}` and confirms a clean worktree. Link that exact SHA/tree/run/review evidence
to the owning Linear issue or Evidence Capsule. Do not merge automatically, and do not encode
this live state as repository prose.

## 12. Current primary-source references

- Supabase platform: https://supabase.com/docs/guides/platform
- Supabase branching: https://supabase.com/docs/guides/deployment/branching
- Neon serverless Postgres: https://neon.com/docs/introduction/serverless
- Neon branching: https://neon.com/docs/introduction/branching
- Vercel Sandbox: https://vercel.com/docs/vercel-sandbox
- Datadog Agent Observability: https://docs.datadoghq.com/llm_observability/
