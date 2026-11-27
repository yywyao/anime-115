# 公开目录来源

本项目的原公开元数据快照来自用户提供的备份。2026-11-27 的补全使用以下公开来源的 ID 关联和别名，不包含旧账号、凭据或私有文件。

- [bangumi-data](https://github.com/bangumi-data/bangumi-data)：标题、译名、播出日期与明确的跨站链接，数据采用 CC BY 4.0。保留该来源署名。
- [open-ani/bangumi-tmdb](https://github.com/open-ani/bangumi-tmdb)：Bangumi 条目到 TMDB 作品、季或特定剧集的关联，数据采用 CC BY 4.0。特定剧集补全使用发布版本 `b866d1a4fbb1c140daa82f2b13ac86abad04c3ae`，按发布 manifest 校验 SHA-256，且只补充与现有 Bangumi ID、TMDB 作品 ID 和季号一致的唯一集号。
- [Fribb/anime-lists](https://github.com/Fribb/anime-lists)：AniList、MAL、AniDB 与 TMDB 等来源的 ID 对照。该项目聚合 Anime-Lists 和 anime-offline-database；原始数据及各来源的许可与署名要求以相应项目为准。本项目保留来源记录，不将其重新标注为自有数据。
- [蜜柑计划](https://mikanani.me/)：公开季度、作品 ID、字幕组信息、Bangumi 链接及原始封面。
- [TMDB](https://www.themoviedb.org/)：已有资料和用户配置 API 后取得的元数据与图片。本产品使用 TMDB 数据，但未经 TMDB 认可或认证。
- [TMDB 每日 ID 导出](https://developer.themoviedb.org/docs/daily-id-exports)：`seed/tmdb-original-titles.json` 来自 2026-11-27 的官方电影和电视剧导出，仅保留本目录已有的、媒体类型和 TMDB ID 一致的原名。没有译名时可离线使用原名。
- `seed/bangumi-zh-hans.json` 仅包含 bangumi-data 中与现有 Bangumi ID 明确对应的唯一简中名称。`seed/tmdb-japanese-posters.json` 记录经人工确认的 [《名侦探光之美少女！》TMDB 日文竖版海报](https://image.tmdb.org/t/p/original/zuPQ9R5x17EM6xEA37TLRkL2BGD.jpg)。
- `seed/tmdb-japanese-logos.json` 恢复原目录中已按 TMDB ID、媒体类型和图内日文核验的 Logo。后续素材通过 TMDB 的图片语言标签自动选取。Bangumi 原封面和中文简介从 [公开作品 API](https://bangumi.github.io/api/) 按明确的条目 ID 获取。

来源一致不等于实时 TMDB 认证。未配置用户 TMDB API 时不会借用其他人的密钥。当前助手对可用来源、冲突类型及代表性 TMDB 页面作了核对；未确认的条目保留蜜柑资料，不伪造缺失评分或图片。
