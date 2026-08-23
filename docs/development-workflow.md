# RepoPilot development workflow

Status: executing M0; integrated local candidate is green, hosted CI is pending

Last reviewed: 2026-08-24

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

## 2. Current evidence

### Confirmed

- 当前 main 与 origin/main 指向初始化提交
  a3469d43430f8d276174dc25969102aeb33a328b。
- main 和 origin/main 未在恢复过程中被覆盖；主工作区只包含尚未提交的规划
  文档变更。
- 37 文件的 Planning Slice 已由本地分支 `codex/recovered-planning-slice` 和恢复
  提交 e54d5ba4b2ccdca8a4562588476d009d88ced763 保护；后续硬化提交仍不是 main
  的一部分，也尚未推送。
- 恢复提交的干净检出已用 CPython 3.12.12、uv 0.10.0 完成锁定安装和
  `make check`：Ruff、严格 MyPy 以及 6/6 Pytest 全部通过。
- M0-02 已确认 `httpx2` 是 Starlette 1.6 `TestClient` 的直接测试后端，不应
  删除；依赖来源已迁移为仓库内显式的官方 PyPI 配置，未使用的
  `pytest-cov`/`coverage` 已删除，GitHub adapter 负向路径已经补齐。精确提交
  26942c7 在全新 worktree 完成官方源、无缓存冷安装、34/34 测试和构建。
- 独立语义审计发现原 golden Issue 已被夹具预先满足。COL-9 已把目标改为
  尚未满足且可观察的 `ValueError` 契约，收紧文件排序与 evidence window，
  并加入噪声文件和变异 Issue 测试。
- 集成候选在精确提交 61641fe 上通过 Ruff、严格 MyPy、37/37 Pytest，以及
  两个真实 Uvicorn 子进程的创建、审批、冲突、重启读取和 SQLite 清理 smoke；
  文档合入后的最终提交仍需再做一次冷验收。
- Linear 连接已授权；项目
  [RepoPilot — Verified Issue-to-PR Pilot](https://linear.app/colife/project/repopilot-verified-issue-to-pr-pilot-bf73022b2c42)
  及四个里程碑已创建。COL-5、COL-6 已完成；COL-9 与 COL-7 负责语义和
  真实进程验收；COL-8 等待最终分支、PR 和托管 CI。
- Exa、Supabase、Neon Postgres 和 Vercel 的连接可读。
- Supabase 账号已有一个健康项目，但尚未选择给 RepoPilot 使用。
- Neon Postgres 账号可读，但目前没有项目。
- Vercel 账号可读，但目前没有项目。
- Datadog 工具存在，但连接器当前未授权。
- Devpost 当前没有 RepoPilot 本地流程状态；没有查询、注册或提交任何比赛。

### Inference

- 已有证据支持继续硬化恢复候选，而不是从零重写；发布前仍必须完成最终
  冷态复验、PR 安全审查和远端 CI。
- RepoPilot 在 M0 至 M2 不需要托管 Postgres；SQLite 和版本化运行工件足以
  验证核心产品假设。
- Vercel Sandbox 与 RepoPilot 的不可信代码执行场景高度匹配，但在通过
  威胁模型、网络、凭据、资源限制和清理验收前，只能视作 M1a 候选。

### Unknown

- 文档合入后最终提交在全新无缓存 worktree 中的综合复验结果。
- 候选 Slice 的远端 GitHub Actions 结果。
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
- Uvicorn and an HTTP client: real-process create/read/approve/restart/read smoke.
- security-diff-scan: baseline PR security review.
- GitHub Actions and gh: remote CI and PR evidence.

### Exit criteria

- The candidate tree is preserved by a named recovery commit and branch.
- main is not overwritten during recovery.
- Dependency provenance and secrets are reviewed.
- A clean checkout can install from the lockfile.
- make check passes and records the test count and tool versions.
- A real Uvicorn process completes the documented persistence smoke.
- The documentation distinguishes local, live-GitHub and hosted-CI evidence.
- The branch is pushed through a human-reviewable PR.
- GitHub Actions is observed green.
- The final worktree is clean and both the recovery SHA and final commit SHA are recorded.

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
default. Datadog is currently disconnected and is not a delivery blocker.

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

## 11. Current next action

Complete one cold verification on the final documentation-integrated commit, then push the
recovery branch, open the reviewable baseline PR, observe hosted GitHub Actions, and link the
exact PR/check evidence back to COL-8. Do not merge automatically.

## 12. Current primary-source references

- Supabase platform: https://supabase.com/docs/guides/platform
- Supabase branching: https://supabase.com/docs/guides/deployment/branching
- Neon serverless Postgres: https://neon.com/docs/introduction/serverless
- Neon branching: https://neon.com/docs/introduction/branching
- Vercel Sandbox: https://vercel.com/docs/vercel-sandbox
- Datadog Agent Observability: https://docs.datadoghq.com/llm_observability/
