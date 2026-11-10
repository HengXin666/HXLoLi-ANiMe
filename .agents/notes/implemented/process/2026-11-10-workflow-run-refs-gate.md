# Agent Note: workflow_run 引用门禁

Status: implemented

Decision-ID: workflow-run-refs-gate

## Code

- `scripts/ci/check_workflow_run_refs.py`

## Problem

GitHub 按 glob 语义解析 on.workflow_run.workflows, 而本仓 workflow name 形如 [LoLi-ANiMe]: .... 未转义的方括号被当成字符类, 触发解析失败: 任意 workflow 完成后都会为该 consumer 生成一条 name 为空, 结论为 startup_failure 的幽灵 run, 且 consumer 永远不触发. 这类失败不产生可读报错, 只表现为静默失配与 run 列表污染

## Decision

新增 scripts/ci/check_workflow_run_refs.py 作为本地可跑的机械门禁: 解析全部 .github/workflows 文件的 name 与 workflow_run.workflows 引用, 按 glob 语义逐条匹配, 匹配不上报错误并给出转义后的字面量建议, 用字符类转义方括号的写法同样报错误 (fnmatch 能命中但真实解析失败), workflow name 自身含 glob 元字符时给警告. 含反斜杠转义的引用不能交给 fnmatch, 由同语义的翻译器处理, 纯 glob 引用仍用 fnmatch 复核

诊断始终先写完整 JSON 再决定终端展示: 不超过十条才逐项展开, 超过只给数量与类型统计. 本地退出码 0 无错误, 1 有错误, 2 工具错误. 配套 workflow-run-refs-check.yaml 在 workflow 或本脚本变更时运行, 全程 continue-on-error 并始终上传诊断 artifact, CI 成功只表示流程完成

## Alternatives considered

- 什么都不做 / 复用现有 Agent Notes 门禁: 改动为零, 但现有门禁只判断决策与源码的对应关系, 不解析 workflow 触发契约, 幽灵 run 这类运行时故障不会被任何本地门禁发现
- 只在文档里写清转义规则: 成本最低, 但规则靠人记, 新增 consumer 时仍会复现同一故障
- 直接调用 actionlint: 能校验表达式与事件结构, 但 actionlint 不解析 workflow_run.workflows 与真实 name 的匹配关系, 也无法给出转义的修复建议
- 让脚本自动改写 YAML: 修复一步到位, 但门禁改写用户文件难以审查, 且与 CI 只报告诊断的约定冲突, 因此保持只读

## Consequences

含 glob 元字符的 workflow name 一旦新增引用, 未正确转义就会在本地与 CI 诊断中暴露, 并附可直接粘贴的转义写法. 本脚本自身不修改文件, 修复仍由人完成. 新增 workflow name 含方括号时会产生警告级提示, 属预期噪声; 判断依据是 glob 语义而非真实 GitHub 行为的全量仿真, 极端写法仍需实测复核
