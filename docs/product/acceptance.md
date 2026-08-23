# First vertical-slice acceptance

## Automated acceptance

`make check` 必须重复证明：

- 格式、Lint 和严格类型检查通过；
- FastAPI 创建计划返回 `201 proposed/version 1`；
- 最小 Python 夹具的 README、项目配置、测试配置、源码和测试均形成证据；
- 每个步骤中的文件引用只引用计划内存在的证据 ID；
- 修改一个引用为未知证据后，Pydantic Schema 拒绝计划；
- SQLite 中的计划可由新 FastAPI 实例重新读取；
- 正确 `expected_version` 将计划转换为 `approved/version 2`；
- 过期版本和重复批准返回 `409`；
- Issue URL 与仓库不一致时返回 `422`；
- 非 GitHub 主机的仓库 URL 在网络请求前返回 `422`；
- 固定根目录适配器超过文件树上限时停止并报告 `413` 对应错误；
- GitHub 适配器契约测试只读取被选中的 tree/blob；
- OpenAPI 中不存在执行或 PR 路由。

## Manual smoke acceptance

1. 用临时数据库启动真实 Uvicorn 进程；
2. `GET /healthz` 返回 `200`；
3. 使用测试注入或公开的小型 Python GitHub 仓库创建计划；
4. 读取持久化计划并完成批准；
5. 重启进程后仍能读到批准状态。

实时 GitHub smoke 会消耗外部 API 配额，并受仓库状态与网络影响；自动化 CI 不依赖它。任何实时验证结果都必须单独记录日期、仓库/ref 和 tree SHA。

### Recorded result: passed

On 2026-08-24, a real Uvicorn process used the production GitHub adapter against `pallets/markupsafe` `main`, tree `b2e4d9c7687be25695fffbe93a37622302b24fb1`. Creation returned a bounded, evidence-backed proposed plan; approval returned version 2; a process restart preserved and returned the approved record. The run made only GitHub metadata/tree/blob reads.

## Deferred acceptance

本切片不接受以下项目作为已完成：代码修改、测试执行、Shell、Docker、Git commit/branch、PR、认证、并发多用户隔离、PostgreSQL、模型规划和部署。
