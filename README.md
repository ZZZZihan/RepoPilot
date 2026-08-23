# RepoPilot

RepoPilot 的第一个端到端切片已经落到一个可运行的 FastAPI 后端：提交 GitHub 仓库地址和 Issue 内容后，系统通过受限的只读适配器检查小型 Python 仓库，生成带文件证据的结构化实施计划，将计划持久化到 SQLite，并等待显式人工批准。

当前切片故意止步于批准。它不会克隆仓库、应用 Patch、运行仓库命令、访问宿主 Shell、创建 Docker 沙箱、提交代码或创建 Pull Request。

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

计划由当前的确定性规划器生成。它会利用 Issue 文本为相关 Python 源码和测试排序，但不会调用外部模型；每个计划文件引用必须关联一个存在于计划中的证据 ID。完整计划会在生成、写入数据库以及从数据库读出时分别经过同一个 Pydantic Schema 校验。

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
      "title": "Handle zero divisors explicitly",
      "body": "Update divide() and add a regression test."
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

`approved_by` 目前只是本地审计标签，不是经过认证的身份。重复批准或过期的 `expected_version` 会返回 `409`；批准不会触发后续执行。

## HTTP 接口

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| `GET` | `/healthz` | 检查进程和 SQLite 可用性 |
| `POST` | `/v1/plans` | 检查仓库并持久化 `proposed` 计划 |
| `GET` | `/v1/plans/{plan_id}` | 读取权威计划记录 |
| `POST` | `/v1/plans/{plan_id}/approval` | 执行 `proposed → approved` 转换 |
| `GET` | `/v1/schemas/implementation-plan` | 返回实际使用的计划 JSON Schema |

不存在执行、Shell、沙箱或 PR 路由。

## 验证

```bash
make check
```

该命令依次检查格式、Lint、类型并运行自动化测试。测试使用一个最小 Python 夹具仓库，通过固定根目录适配器走真实 HTTP、Schema、SQLite 和审批链路；GitHub 生产适配器另有模拟 REST 契约测试，不依赖实时网络。

## 项目文档

- [产品上下文](CONTEXT.md)
- [验收标准](docs/product/acceptance.md)
- [开发状态与证据](docs/product/development-status.md)
- [当前架构](docs/architecture.md)
- [ADR 0001：第一个规划切片](docs/adr/0001-planning-vertical-slice.md)
- [后续执行与 PR 接口](docs/contracts/future-stages.md)
- [完整长期项目方案](docs/project-proposal.md)
