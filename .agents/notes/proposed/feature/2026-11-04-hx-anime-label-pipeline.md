# Agent Note: HX-ANiMe 标注流水线 (爬虫事件检测 + data / 标注双流 + bgm 同步)

Status: proposed

Decision-ID: hx-anime-label-pipeline

## Code

- `scripts/crawler/events.py`
- `scripts/label-flow.py`

## Problem

自动推断的 tag 误标多且不可追溯: `scripts/crawler/Tags.py` 用 AOD / AniList / bangumi-data 打分自动上墙 (wallScore=2), 产出 `data/ANiMeTags.json`, 规则调参只能逼近平均口味, 无法反映个人口味. 数据更新与分类混在同一次爬虫提交里, 缺人工标注这一环和单一可信的标注源. 爬虫已能稳定给出新增番剧与状态变化 (watch_status 3 -> 2/4/5) 两类事件.

## Decision

1. 停用自动上墙, 以后只认人工标注: 入口是标注 PR 正文勾选或评论指令, 最终一律经 PR 合入. 未标注 = `data/labels/` 下没有文件.
2. 唯一词表 `data/labelVocab.json`: 分组 susume (物凄い / ロリ / シスコン, 各有专属页 slug) 与 genre (异世界 / 热血 / 战斗 / 推理 / 校园 / 日常 / 搞笑 / 恋爱 / 致郁). 标注表单, 解析器, bgm 同步都只读它. 旧名 妹控 废弃不迁移.
3. 数据落点一番一文件 `data/labels/<bgm_subject_id>.json` (tags / rate / review / mode / by / labeledAt / bgmSynced), 多个标注 PR 并行不冲突.
4. `scripts/crawler/events.py` 检测两类事件产出 events.json: new (新增 id) 与 review (3 -> 2/4/5 状态变化).
5. 拆两条提交流并用 GitHub label 区分: data 流 `flow:data` 自动合入, 永不碰 `data/labels/`; 标注流 `flow:label` 由 data 流合入后 workflow_run 触发, 按事件开 `label/<id>` 分支与草稿, label 为 `mode:new` 或 `mode:review` + `state:等待标注`. 一部番同时最多一个开着的标注 PR, 新增 PR 未完成时状态变化就地升级为观后感模式.
6. `scripts/label-flow.py` 提供 open / handle / post-merge / expire 四个子命令: PR 正文带封面与番剧名与 bgm 链接与勾选框, 勾选零打字; 评论指令 /tag /untag /rate /review /done /skip /nobgm; 置顶结果评论不刷屏; 勾标注完成自动 squash 合入; 满一个月 (创建或最后用户交互较晚者起算) 关闭并打 `state:expired`, 不写标注文件; 超过 5 部合并为一个批量标注 PR.
7. issue 表单 `标注番剧` 作备用入口: bot 代开对应标注 PR 并 `Closes #n`, 合入时 issue 自动关闭, 保证单一落点.
8. 合入后 `scripts/label-sync-bgm.py` 调 bgm api `POST /v0/users/-/collections/{id}`, 只增删词表内词, 其他手打标签原样保留, rate / comment 按勾选, 失败重试 3 次打 `bgm:sync-failed`. 主站不读同步回来的 user_status.tags, 防回环.

## Alternatives considered

- **什么都不做 / 复用现有 Tags.py 自动推断**: 最强理由是零维护且覆盖率高 (154/160 部有 tag), 流水线不用改. 否决: 误标正是这次重构的起因, 规则调参无法反映个人口味, 四个来源并存依然无法追溯.
- **只在 bgm 上打标签, 爬虫拉回**: 最强理由是 bgm 已有标注 UI 且天然同步. 否决: bgm 标签是自由文本, 无法约束到 labelVocab, 没有带封面的批量入口和审阅 / 过期机制, 拉回又回到从 user_status.tags 推断的老路.
- **只用 issue 不用 PR**: 最强理由是 issue 表单更像问卷. 否决: 标注结果要进仓库文件, 只有 PR 能 auto-merge 并留 diff, 也才能和 flow:data 在同一 PR 列表用 label 筛选.
- **单一 JSON 汇总所有标注**: 最强理由是读取简单, 一次 fetch 拿全量. 否决: 多个标注 PR 并行必冲突, 与 data 流难以按路径隔离, 改为一番一文件.
- **保留单流, 爬虫提交里附带标注**: 最强理由是不用新增 workflow 与 label. 否决: 标注要等用户, 可能很久不理, 与需及时合入的数据更新绑一起会卡住数据流.

## Consequences

- 站内分类视觉上清空, 之后分类只随人工标注增长. `ANiMeTags.json` 标废弃后删除.
- 新增人工成本: 每部新番 / 每次看完一个标注 PR. 用勾选零打字, 超过 5 部批量 PR, 一个月自动过期压低成本.
- bgm tag 不允许空格, labelVocab 同步名需预校验; `BGM_TOKEN` 需收藏写权限.
- 本站与 bgm 是两套标签视图, 手动清理 bgm 旧随手标签需单独确认, 不自动删除.

## Verification

- `uv run scripts/redlines/agent_notes.py --all` 在本子仓运行, 结果见本仓 `.git/reports/agent-notes-all.json`.
- 静态可查: `python3 -m py_compile scripts/crawler/events.py scripts/label-flow.py scripts/label-sync-bgm.py`; `.github/workflows/anime-label-flow.yaml` 与 `anime-label-expire.yaml` 合法 YAML; events.json 形状 `{"new": [...], "review": [{"id","from","to"}]}`.
- 端到端 (真实 GitHub PR / bgm api 写回) 未实测, 需要 BGM_TOKEN 与真实仓库事件.
