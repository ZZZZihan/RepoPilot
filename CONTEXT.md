# RepoPilot product context

## 用户和核心动作

RepoPilot 当前服务于希望先审查实施范围、再决定是否让自动化系统修改代码的开发者。用户的一个核心动作是：提交一个小型 Python GitHub 仓库和一份 Issue 描述，获得可以追溯到仓库文件证据的实施计划，并显式批准该计划。

## 本切片的成功定义

一次成功流程必须同时满足：

1. HTTP 输入只接受固定的 GitHub 仓库地址和受限长度的 Issue 数据；
2. 仓库适配器在硬性资源上限内读取文件树、README、Python 项目/测试配置、代表性源码和测试；
3. 计划符合版本化 Pydantic runtime model；接口提供结构性 JSON Schema 和
   `x-repopilot-semantic-constraints` 清单，但证据图、动作和状态语义由 runtime
   validator 单独执行；所有文件引用均能追溯到计划中的证据；
4. 验证意图只来自 README、项目配置或测试配置证据，且保持 `executed=false`；
   `verification_readiness="ready"` 至少需要一个有证据的 pytest 意图；
5. 仓库 URL/owner/name/ref/tree SHA 形成一致的 canonical identity，计划与批准时间必须
   带时区并规范化为 UTC；
6. 计划写入 SQLite 后可以由新应用实例重新读取，SQL 行的 plan ID、Schema version、
   status 和 version 必须与文档 envelope 精确一致；
7. 唯一合法状态为无审批的 `proposed/version 1`，或由版本 1 审批得到的
   `approved/version 2`；只有带正确 `expected_version=1` 的请求可以完成该转换；
8. 批准和 readiness 都不产生仓库写入、命令执行、外部发布或 execution seal。

## 权威数据与推导数据

- 请求中的仓库 URL、ref、Issue 标题和正文是用户提供的输入；当前不重新获取 Issue。
- GitHub tree SHA 和选中文档摘要来自只读仓库检查。
- 实施步骤、风险、验证意图和 `verification_readiness` 是确定性推导结果，必须由人审查。
- SQLite 中通过 Pydantic runtime 校验、且关系行 envelope 与文档一致的计划是当前切片
  的权威规划状态，不是代码执行授权。
- `approved_by` 是未经认证的本地标签，不能当作安全身份。

## 明确非目标

产品 API 与规划运行时不会生成代码、应用 Patch、Git clone、启动被检查仓库的
子进程或 Shell，也没有 Docker、后台任务、模型调用、身份认证、分支/提交/PR、
部署与长期记忆。`make smoke-m0` 仅为验收启动本项目的两个 Uvicorn 子进程，且
使用参数数组执行（`shell=False`）；它不会执行被检查仓库内容。其进程边界仅为
`managed_direct_children_original_posix_process_group_and_observed_ports`，即受管直接
子进程、仍在原 POSIX process group 的成员和已观测端口，并不声称捕获主动
`setsid()` 脱离的后代。`src/repopilot/future.py`
只描述后续阶段的 Interface 和前置条件，不代表这些能力已经实现；当前 future request
Schema 会拒绝 planning-only `ImplementationPlan` 1.0，即使该计划已经 approved/ready。
