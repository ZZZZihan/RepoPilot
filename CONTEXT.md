# RepoPilot product context

## 用户和核心动作

RepoPilot 当前服务于希望先审查实施范围、再决定是否让自动化系统修改代码的开发者。用户的一个核心动作是：提交一个小型 Python GitHub 仓库和一份 Issue 描述，获得可以追溯到仓库文件证据的实施计划，并显式批准该计划。

## 本切片的成功定义

一次成功流程必须同时满足：

1. HTTP 输入只接受固定的 GitHub 仓库地址和受限长度的 Issue 数据；
2. 仓库适配器在硬性资源上限内读取文件树、README、Python 项目/测试配置、代表性源码和测试；
3. 计划符合版本化 Schema，所有文件引用均能追溯到计划中的证据；
4. 计划写入 SQLite 后可以由新应用实例重新读取；
5. 只有带正确 `expected_version` 的请求可以完成 `proposed → approved`；
6. 批准不产生仓库写入、命令执行或外部发布副作用。

## 权威数据与推导数据

- 请求中的仓库 URL、ref、Issue 标题和正文是用户提供的输入；当前不重新获取 Issue。
- GitHub tree SHA 和选中文档摘要来自只读仓库检查。
- 实施步骤、风险和验证意图是确定性推导结果，必须由人审查。
- SQLite 中通过 Schema 校验的计划文档是当前切片的权威状态。
- `approved_by` 是未经认证的本地标签，不能当作安全身份。

## 明确非目标

当前没有代码生成、Patch 应用、Git clone、子进程或 Shell、Docker、后台任务、模型调用、身份认证、分支/提交/PR、部署与长期记忆。`src/repopilot/future.py` 只描述后续阶段的接口和前置条件，不代表这些能力已经实现。

