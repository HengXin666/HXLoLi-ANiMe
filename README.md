# HXLoLi-ANiMe

[HXLoLi](https://github.com/HengXin666/HXLoLi) 的番剧数据子仓库。

## 数据结构

```
data/
├── ANiMeRecord.json    ← 番剧记录 (~10MB)
├── Actor.json          ← 声优数据 (~1.3MB)
├── anime/*.jpg         ← 番剧封面
├── cv/*.jpg            ← 声优头像
├── kyara/*.jpg         ← 角色头像
└── relation/*.jpg      ← 关联作品封面

scripts/
└── crawler/            ← Bangumi 爬虫脚本
    ├── Api.py
    ├── ANiMeType.py
    └── ApiReqRateLimiter.py
```

## CDN 加速

通过 [HX-CDN-Forge](https://github.com/HengXin666/HX-CDN-Forge) 的 tag 方式实现 CDN 加速。

### 工作流

```
定时爬取 (crawl-anime.yml)
  → 数据有变化 → git push
    → 触发 cdn-tag.yml
      → 打 HX-{随机6位}-{时间戳} tag
      → 向 HXLoLi 主仓库发 PR 更新 cdnVersion.ts
```

### Tag 格式

`HX-{随机6位字符串}-{当前时间戳}`

例: `HX-eUNqEy-20260331231834`

### Secrets 配置

| Secret | 说明 |
|--------|------|
| `BGM_TOKEN` | Bangumi API Access Token |
| `BGM_USERNAME` | Bangumi 用户名 (默认 `heng_xin`) |
| `HXLOLI_PR_TOKEN` | 有 HXLoLi 仓库写权限的 PAT |
