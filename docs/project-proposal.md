# 推荐项目：RepoPilot——从 GitHub Issue 到可验证 PR 的研发 Agent

> Status: long-term vision and early proposal; this is not the authoritative current
> delivery sequence. The canonical staged scope and gates live in
> [RepoPilot development workflow](development-workflow.md). In particular, current M0 is
> planning/approval only; an approved `ImplementationPlan` 1.0 with
> `verification_readiness="ready"` is not execution authority,
> and Patch production or repository execution begins no earlier than an ADR-governed M1a seal.

我建议你下一个个人项目不要做通用聊天助手，也不要再做“上传 PDF 问答”，而是做一个：

> **面向 GitHub 仓库的研发任务 Agent：读取 Issue，分析代码库，制定修改计划，在沙箱中修改代码并执行测试，最终经人工确认后创建 Pull Request。**

项目名称暂定：

* **RepoPilot**
* **Issue2PR**
* **DevFlow Agent**

其中我更推荐 **RepoPilot**。

截至 **2026 年 8 月 24 日**，腾讯的智能体平台岗位明确要求智能体编排、工具调用、记忆管理、动态规划与 RAG；百度当前 Agent 开发实习岗要求知识库前后端、工作流评测、问题归因和回归验证；百度的 AI 平台后端岗位进一步要求模型路由、限流、上下文缓存、熔断、人工审核、断点续跑、Tracing、鉴权和审计。这些要求几乎都可以自然地放入 RepoPilot，而不是为了堆技术强行添加。([腾讯招聘][1])

---

## 一、项目要解决什么问题

### 用户场景

用户在 GitHub 仓库中提交一个 Issue，例如：

> 给用户查询接口增加分页功能，并补充单元测试。

RepoPilot 自动完成：

```text
读取 Issue
    ↓
分析代码仓库结构
    ↓
检索相关代码、文档和历史修改
    ↓
生成实施计划
    ↓
等待用户批准
    ↓
在隔离沙箱中修改代码
    ↓
执行单元测试、Lint 和类型检查
    ↓
根据测试结果修复问题
    ↓
展示 Diff、测试报告、执行轨迹和成本
    ↓
等待最终确认
    ↓
创建 GitHub Pull Request
```

它不是“帮你写一段代码”，而是完成一个具有明确起点和终点的研发工作流。

---

# 二、为什么这个项目适合作为求职项目

## 1. 有客观的任务完成标准

很多 Agent 项目只能展示：

> “回答得看起来不错。”

RepoPilot 可以使用客观标准：

* 代码是否能够运行；
* 原有测试是否通过；
* 新测试是否通过；
* 是否解决 Issue；
* 是否修改了不应该修改的文件；
* 是否产生安全问题；
* 是否成功创建 PR。

这使得你能够真正做 Agent Eval，而不是完全依赖另一个大模型打分。

## 2. 可以覆盖完整 Agent 技术栈

| 招聘要求       | RepoPilot 中的对应实现              |
| ---------- | ----------------------------- |
| 业务 Agent   | 完成真实的软件研发任务                   |
| 工具调用       | GitHub、代码搜索、文件读取、补丁修改、测试执行    |
| Agent 编排   | 分析、规划、审批、执行、验证、重试、提交          |
| RAG        | 检索仓库代码、README、API 文档、历史经验     |
| 后端开发       | API、数据库、任务队列、状态持久化            |
| 全栈开发       | Web 控制台、任务详情页、Diff 和 Trace 页面 |
| 生产部署       | Docker、CI/CD、配置管理、日志监控        |
| Agent Eval | 测试通过率、任务成功率、工具调用正确率           |
| 安全         | 沙箱、权限控制、人工确认、操作审计             |
| 可观测性       | 每一步输入输出、Token、延迟、错误和重试        |

## 3. 面试时容易演示

面试官不需要先理解一个复杂业务背景。

演示过程可以非常直观：

1. 选择一个仓库；
2. 输入一个 Issue；
3. 查看 Agent 的分析和计划；
4. 点击批准；
5. 查看实时工具调用；
6. 查看代码 Diff；
7. 查看测试结果；
8. 创建 PR。

这个演示能同时证明：

* 你理解 Agent；
* 你会写后端；
* 你会做前端；
* 你了解 Git；
* 你会设计工作流；
* 你会做评测和工程可靠性。

---

# 三、第一版必须严格限制范围

不要一开始就做“通用 Coding Agent”。第一版只支持：

> **规模较小的 Python 仓库中的确定性、可测试任务。**

## 支持的任务

第一版只做以下四类：

1. 修复局部 Bug；
2. 增加一个小型功能；
3. 补充单元测试；
4. 修改配置、文档或接口参数。

例如：

```text
修复 divide() 在除数为 0 时没有抛出正确异常的问题。

为用户列表接口增加 page 和 page_size 参数。

给 parse_config() 补充缺失字段的测试。

将日志级别改为通过环境变量配置。
```

## 暂时不支持

第一版明确排除：

* 大型跨仓库任务；
* 前后端联合重构；
* 数据库迁移；
* 自动部署生产环境；
* 自主合并 PR；
* 自由执行任意 Shell 命令；
* 多 Agent 协作；
* 长期记忆和复杂 GraphRAG；
* 自动修改超过一定数量的文件。

这一点很重要。**第一版应当证明基本闭环有效，而不是证明你能堆多少模块。**

---

# 四、完整工作流设计

推荐先使用一个 Agent 加确定性状态机，不要上来做多 Agent。

```text
CREATED
   ↓
REPOSITORY_SYNCING
   ↓
CONTEXT_RETRIEVAL
   ↓
PLANNING
   ↓
WAITING_PLAN_APPROVAL
   ↓
SANDBOX_PREPARING
   ↓
IMPLEMENTING
   ↓
TESTING
   ├── 通过 → REVIEWING
   ├── 失败且可重试 → DIAGNOSING → IMPLEMENTING
   └── 超过重试次数 → FAILED
   ↓
WAITING_PR_APPROVAL
   ↓
CREATING_PR
   ↓
COMPLETED
```

## 核心节点

### 1. Repository Sync

负责：

* 拉取仓库；
* 获取默认分支；
* 读取项目语言；
* 分析文件结构；
* 提取 README、依赖文件、测试配置；
* 建立代码索引。

### 2. Context Retrieval

根据 Issue 检索：

* 相关文件；
* 相关函数和类；
* 相关测试；
* 配置文件；
* README 或开发文档；
* 相邻调用关系。

检索结果必须记录：

```json
{
  "file": "src/user/service.py",
  "start_line": 42,
  "end_line": 96,
  "reason": "包含用户列表查询和分页逻辑"
}
```

这样 Agent 的计划能够提供代码证据，而不是凭空推断。

### 3. Planning

输出结构化计划：

```json
{
  "summary": "为用户列表接口增加分页支持",
  "files_to_read": [
    "src/user/service.py",
    "src/user/api.py",
    "tests/test_user_api.py"
  ],
  "files_expected_to_change": [
    "src/user/service.py",
    "src/user/api.py",
    "tests/test_user_api.py"
  ],
  "steps": [
    "确认当前查询接口的返回结构",
    "为 service 层增加 offset 和 limit",
    "为 API 层增加 page 和 page_size 参数",
    "增加分页边界测试"
  ],
  "verification_commands": [
    "pytest tests/test_user_api.py",
    "ruff check src tests"
  ],
  "risk_level": "medium"
}
```

计划必须通过 Schema 校验，不允许只返回自由文本。

### 4. Human Approval

涉及代码写入之前，用户必须确认：

* 计划；
* 修改范围；
* 测试命令；
* 风险级别。

用户可以：

* 批准；
* 拒绝；
* 修改计划；
* 限制允许修改的文件。

### 5. Sandbox Execution

每次任务创建独立 Docker 容器：

* 仓库挂载到临时目录；
* 限制 CPU 和内存；
* 限制运行时间；
* 默认关闭网络；
* 使用非 root 用户；
* 限制可执行命令；
* 不向容器注入 GitHub Token；
* 任务结束后销毁容器。

Agent 不能直接在宿主机执行模型生成的 Shell 命令。

### 6. Implementation

建议不要一开始开放任意文件操作，只提供有限工具：

```text
list_files
search_code
read_file
apply_patch
run_tests
get_git_diff
```

其中 `apply_patch` 应当：

* 校验文件路径；
* 禁止写出仓库目录；
* 限制单次修改大小；
* 保存修改前版本；
* 记录完整审计日志。

### 7. Testing and Recovery

测试失败后，不要立即让 Agent无限循环。

推荐规则：

```text
最大修复轮数：2
每轮必须先解释失败原因
每轮只能修改计划范围内的文件
连续出现相同错误时立即停止
测试超时立即停止
发现依赖安装或网络问题时请求人工介入
```

### 8. PR Creation

创建 PR 前展示：

* 修改文件；
* Git Diff；
* 新增和删除行数；
* 测试结果；
* 未通过检查；
* Agent 执行过程；
* Token 和模型成本；
* 风险提示。

最终由用户点击确认后创建 PR，不能自动合并。

---

# 五、建议的系统架构

```text
┌─────────────────────────────────────────────┐
│                Next.js Web UI               │
│  仓库管理 / Issue 输入 / 执行轨迹 / Diff / Eval │
└─────────────────────┬───────────────────────┘
                      │ REST + SSE/WebSocket
┌─────────────────────▼───────────────────────┐
│                FastAPI Backend              │
│ Auth / Project / Task / Approval / Run API  │
└──────────┬──────────────────────┬───────────┘
           │                      │
┌──────────▼──────────┐  ┌────────▼───────────┐
│ Agent Orchestrator  │  │ Background Worker │
│ 状态机 / Checkpoint │  │ 索引 / 测试 / 同步  │
└──────────┬──────────┘  └────────┬───────────┘
           │                      │
┌──────────▼──────────────────────▼───────────┐
│                  Tool Layer                 │
│ GitHub / Code Search / Patch / Test / Git   │
└──────────┬──────────────────────┬───────────┘
           │                      │
┌──────────▼──────────┐  ┌────────▼───────────┐
│ Docker Sandbox     │  │ PostgreSQL         │
│ Clone/Edit/Test    │  │ Task/Run/Trace     │
└────────────────────┘  └────────┬───────────┘
                                 │
                        ┌────────▼───────────┐
                        │ pgvector / Index  │
                        │ Code & Docs RAG   │
                        └────────────────────┘
```

---

# 六、推荐技术栈

| 层级        | 技术选择                         |
| --------- | ---------------------------- |
| 前端        | Next.js、TypeScript           |
| 后端        | Python、FastAPI、Pydantic      |
| Agent 编排  | LangGraph，后续可以自研状态机          |
| 数据库       | PostgreSQL                   |
| 向量检索      | pgvector                     |
| 后台任务      | Redis + Celery、RQ 或 Dramatiq |
| GitHub 接入 | GitHub App 或 OAuth App       |
| 代码解析      | Python AST，后续增加 Tree-sitter  |
| 沙箱        | Docker                       |
| Trace     | OpenTelemetry 或 Langfuse     |
| 测试        | Pytest                       |
| CI/CD     | GitHub Actions               |
| 本地部署      | Docker Compose               |
| 前端实时状态    | SSE 优先，必要时使用 WebSocket       |

## 关于 LangGraph

可以使用 LangGraph，但不要让整个项目变成：

> “我调用了 LangGraph 的几个节点。”

你必须自己设计：

* 状态模型；
* 节点输入输出；
* Checkpoint；
* 重试策略；
* 终止条件；
* 人工审批；
* 错误恢复；
* Tool Schema；
* 审计记录。

框架只是执行载体。

---

# 七、后端数据模型

第一版至少需要这些表。

## `projects`

```text
id
user_id
github_owner
github_repo
default_branch
language
index_status
created_at
```

## `tasks`

```text
id
project_id
issue_number
title
description
status
risk_level
created_at
updated_at
```

## `agent_runs`

```text
id
task_id
model
prompt_version
status
current_node
started_at
finished_at
input_tokens
output_tokens
estimated_cost
```

## `run_steps`

```text
id
run_id
node_name
step_index
input
output
status
latency_ms
error
created_at
```

## `tool_calls`

```text
id
run_id
step_id
tool_name
arguments
result
status
latency_ms
created_at
```

## `approvals`

```text
id
task_id
approval_type
requested_payload
decision
comment
created_at
```

## `artifacts`

```text
id
run_id
artifact_type
file_path
content_location
metadata
created_at
```

`artifact_type` 可以包括：

```text
plan
patch
test_report
git_diff
execution_log
pr_summary
```

---

# 八、评测系统怎么做

这是整个项目最重要的差异化部分。

## 1. 建立一个专用 Benchmark 仓库

不要一开始在大型开源仓库上测试。

建立一个小型仓库，例如：

```text
repopilot-bench/
├── src/
│   ├── calculator.py
│   ├── users.py
│   ├── config.py
│   └── orders.py
├── tests/
├── seeded_issues/
└── hidden_tests/
```

人为植入 20～30 个任务：

| 类型     | 示例             |
| ------ | -------------- |
| Bug 修复 | 除零异常处理错误       |
| 边界条件   | 空列表返回值错误       |
| 参数校验   | page_size 没有上限 |
| 小功能    | 增加用户状态过滤       |
| 测试补充   | 为配置解析增加测试      |
| 配置修改   | 从环境变量读取日志级别    |
| 错误恢复   | 测试命令首次执行失败     |
| 无法完成   | Issue 信息不足     |

## 2. 主指标

```text
Task Success Rate
Hidden Test Pass Rate
Regression Test Pass Rate
Valid Tool Call Rate
Patch Apply Success Rate
Average Repair Attempts
P50 / P95 Task Latency
Average Token Cost
Human Intervention Rate
Unsafe Operation Block Rate
```

## 3. 成功标准

一个任务只有同时满足以下条件才算成功：

```text
隐藏测试通过
原有测试无回归
代码能够正常导入或编译
修改范围没有越界
没有未批准的外部副作用
生成的 Diff 与 Issue 相关
```

不要把“Agent 自己说任务完成了”作为成功标准。

## 4. 基线对比

至少设置三种方案：

| 方案         | 描述                             |
| ---------- | ------------------------------ |
| Baseline A | 将 Issue 和相关文件一次性给模型，直接生成 Patch |
| Baseline B | Agent Loop，但没有代码检索和结构化计划       |
| RepoPilot  | 检索 + 结构化计划 + 工具循环 + 测试反馈       |

最终报告：

```text
             成功率   平均调用次数   平均成本   平均延迟
Baseline A
Baseline B
RepoPilot
```

这样你可以回答面试中最关键的问题：

> 为什么要设计这个 Agent 工作流？它比直接调用模型究竟好在哪里？

---

# 九、先做一个“击杀实验”，再决定是否完整开发

结合你之前倾向先做可证伪 Pilot，第一步不要开发前端，不要设计复杂数据库，也不要加入长期记忆。

## Pilot 范围

只实现一个命令行版本：

```bash
python -m repopilot run \
  --repo ./repopilot-bench \
  --issue seeded_issues/issue_01.md
```

只提供四类能力：

```text
search_code
read_file
apply_patch
run_tests
```

只准备 10 个确定性任务。

## Pilot 输出

每个任务保存：

```text
runs/<run_id>/
├── issue.md
├── retrieved_context.json
├── plan.json
├── tool_calls.jsonl
├── patch.diff
├── test_report.json
└── final_result.json
```

## 建议的项目决策阈值

下面是我们为项目管理设定的阈值，不是行业标准：

| Pilot 结果  | 决策                 |
| --------- | ------------------ |
| 成功 5 个及以上 | 继续做完整后端和前端         |
| 成功 2～4 个  | 缩小任务范围，分析失败类别      |
| 成功 0～1 个  | 暂停完整开发，重新设计工具和任务边界 |

关键不是初始成功率高，而是失败是否可以清楚归因：

* 检索失败；
* 计划错误；
* Patch 错误；
* 测试理解错误；
* 工具参数错误；
* 上下文不足；
* 任务本身不可完成。

---

# 十、项目开发顺序

## 阶段 0：CLI 击杀实验

完成：

* Benchmark 仓库；
* 10 个任务；
* 四个工具；
* 单 Agent Loop；
* Patch 应用；
* 测试执行；
* 基础结果统计。

**验收结果：能够自动完成一部分真实、可测试的小任务。**

## 阶段 1：Agent Runtime

增加：

* 显式状态机；
* Pydantic 状态定义；
* Checkpoint；
* 最大重试；
* 失败分类；
* Prompt 版本管理；
* 结构化计划。

**验收结果：中断后能够继续，失败原因能够追踪。**

## 阶段 2：后端系统

增加：

* FastAPI；
* PostgreSQL；
* 项目和任务 API；
* 后台 Worker；
* 用户审批接口；
* Trace 查询接口。

**验收结果：Agent 不再依赖一次性 CLI 进程。**

## 阶段 3：GitHub 集成

增加：

* GitHub 登录；
* 仓库授权；
* Issue 获取；
* 分支创建；
* PR 创建；
* Webhook；
* 最小权限控制。

**验收结果：能够完成完整 Issue-to-PR 闭环。**

## 阶段 4：前端控制台

增加：

* 项目列表；
* 任务创建；
* 实时执行轨迹；
* 计划审批；
* Diff 页面；
* 测试报告；
* 成本统计；
* PR 确认。

**验收结果：招聘方可以直接在线体验。**

## 阶段 5：Eval 与生产化

增加：

* 自动运行 Benchmark；
* 版本对比；
* 回归检测；
* P50/P95 延迟；
* Token 成本；
* 错误分类看板；
* Docker Compose；
* GitHub Actions；
* 部署文档。

**验收结果：项目不仅能运行，而且能够持续评估和迭代。**

---

# 十一、与你现有研究方向的结合方式

你目前还在研究 **Agent 记忆的负迁移、跨风险泛化和校准**。RepoPilot 后续可以成为一个真实的外部验证环境。

但这部分不要进入第一版。

## 后续研究扩展

Agent 完成任务后，将轨迹保存为经验：

```text
任务描述
仓库特征
检索到的文件
执行计划
代码修改
测试失败
修复动作
最终结果
```

新任务到来时检索历史经验。

然后比较：

### 方法 A：无经验记忆

只根据当前仓库和 Issue 执行。

### 方法 B：Naive Top-K Memory

直接检索最相似的历史修复轨迹。

### 方法 C：Risk-aware Memory Gate

预测某条历史经验是否适用于当前任务，风险高时拒绝注入。

可以测试：

* 同仓库、同任务类型；
* 跨仓库；
* API Schema Shift；
* 依赖版本变化；
* 表面相似但修复策略相反；
* 多条记忆累积；
* 失败轨迹污染。

指标可以继续采用你之前考虑的：

```text
任务成功率
负迁移率
ECE
Brier Score
Risk-Coverage
工具调用错误率
平均修复轮数
```

这样你的工程项目和研究课题会形成同一条主线：

```text
招聘项目：
完整的 Issue-to-PR Agent 系统

研究问题：
历史修复经验什么时候帮助 Agent，什么时候导致负迁移，以及如何校准地拒绝危险记忆
```

这比在一个纯人工构造问答环境里研究记忆更有说服力，也能测试真实 Agent 的外部有效性。

---

# 十二、第一步应该实际完成什么

当前不应先画完整 UI，也不应先接 GitHub OAuth。

先创建两个仓库：

```text
repopilot
repopilot-bench
```

`repopilot` 的初始结构：

```text
repopilot/
├── README.md
├── pyproject.toml
├── src/
│   └── repopilot/
│       ├── agent/
│       │   ├── state.py
│       │   ├── graph.py
│       │   └── prompts.py
│       ├── tools/
│       │   ├── search_code.py
│       │   ├── read_file.py
│       │   ├── apply_patch.py
│       │   └── run_tests.py
│       ├── sandbox/
│       │   └── runner.py
│       └── cli.py
├── evals/
│   ├── runner.py
│   ├── metrics.py
│   └── cases/
└── tests/
```

第一个可执行目标只有一句话：

> **输入一个本地 Python 仓库和一个 Issue，让 Agent 生成 Patch、运行测试，并保存完整执行轨迹。**

这一闭环跑通以后，再决定是否加入 Web 后端、GitHub PR、RAG、长期记忆。这样可以最大程度避免把大量时间消耗在一个核心能力尚未验证的“大而全系统”上。

[1]: https://careers.tencent.com/jobdesc.html?postId=2079104781984645120&utm_source=chatgpt.com "岗位详情"
