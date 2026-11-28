# 使用与维护指南

## 115 与 AList

网站使用本地账号。首次在管理员设置中粘贴已有第三方客户端的 115 Cookie 后，应用将加密保存，并用同一份 Cookie 验证 115 会话、挂载 AList。Cookie 到期时，在原客户端获取新 Cookie 后更新设置。连接暂时失败时可点「修复连接」，使用已保存的 Cookie 重试。

默认部署包含 AList 和 Sonarr。应用自动取得 AList 管理令牌，115 存储复用设置中保存的 CK。AList 挂载信息位于临时内存盘，重启后会从加密配置恢复同一个专用目录。

需要使用已有 AList 时，在设置的 AList 区域切换「自定义」，填写地址、用户名以及管理员密码或管理令牌，保存时会验证连接。相同地址及用户名可以留空凭据以沿用已保存值。更改服务地址需要填写对应服务的凭据。选择「自动」并保存即可恢复本部署的服务。

系统使用独立的 `mikan` 转存根目录。旧版已绑定的专用根目录会按原目录 ID 更名，原有文件保留。如果网盘中已有其他 `mikan` 目录，先为其更名后再修复连接，系统不会接管或合并它。请通过网站更新绑定，不要手动修改 AList 专用挂载。日常网盘文件不属于任务管理范围。

关联作品的转存结果按以下结构整理，同一系列只有明确关联的来源 ID 相同时才会共用文件夹。

```text
mikan/
└── 系列名/
    ├── 系列名 - S01E01 [字幕组] [简日] [1080p].mkv
    └── 系列名 - S02E01 [字幕组] [简日] [1080p].mkv
```

转存期间使用独立的临时任务目录。改名完成后按文件 ID 和大小校验，移入系列目录，仅清理空的任务目录。重复文件名不会覆盖。旧任务中未关联作品的手动磁力保留隔离目录。

VPS 下载采用同一系列目录，保存到 `runtime/app/library/mikan/系列名/剧集文件`。已有下载保留原路径，新任务使用上述结构；同名文件不会覆盖，也不会只凭大小相同直接收录。

## 转存与入库

资源列表中每项均有「转存至 115」和「下载到 VPS」，勾选多个资源后也可批量操作。仅转存不会下载到 VPS。点击下载到 VPS 后，无需等待云端完成，任务会等待转存、改名和整理，再通过 AList 写入本机专用目录。在已完成任务中也可以选择全部文件或指定文件下载。文件成功写入并通过大小核验后计入已入库集数。

在资源弹窗顶部选择分辨率、字幕和字幕组。同一项可多选，不同筛选项组合生效。字幕组右侧「全选」选择该组当前匹配的资源，入库预览会优先使用已选资源；未勾选资源时，按当前筛选生成清单。

从详情页的转存弹窗打开补集、全集收录和洗版。确认预览清单后排队。合集集号以实际取得的文件为准。洗版按选定的字幕组、字幕版本或分辨率挑选资源，并保留已入库版本。

## 封面与评分

详情页使用 TMDB 横图、日语标题 Logo 及来源评分图标。Logo 按系列 ID 和季号核对，已确认的通用系列标志可用于后续季度。多季作品没有明确归属的 Logo 时保留空位，避免套用其他季或篇章。

TMDB 的[季度图片接口](https://developer.themoviedb.org/reference/tv-season-images)只提供季度海报，系列 Logo 图库没有季度归属。已核对的 Logo 关联保存在 `seed/tmdb-logo-scopes.json`，图像下载失败时只在同一适用范围内回退。

## RSS 订阅

支持蜜柑作品 RSS、字幕组 RSS 和个人 MyBangumi RSS。创建订阅时，默认仅处理此后发布的资源；需要下载历史资源时启用“包含现有资源”。

字幕组右侧「追新」会绑定该组的蜜柑 RSS，并带入当前分辨率和字幕筛选。默认每分钟检查 RSS，新建的单组追新匹配后立即转存。检查间隔和优选等待可在追新管理中修改。

| 规则 | 行为 |
| --- | --- |
| 字幕组 | 可设置准入列表及排序；未选择时允许全部 |
| 字幕版本及分辨率 | 根据发布信息识别，未知标签保留未知 |
| 关键词 | 必含词全部匹配，排除词任一匹配；使用字面文本 |
| 集数 | 可设集数范围、合集开关和偏移 |
| 优选等待 | 等待期内候选统一排序，完成转存后不自动替换 |
| 去重 | 支持每集、每集每种字幕或资源哈希去重 |
| 下载到 VPS | 显式开启后，转存就绪时另建 VPS 下载任务 |

使用预览检查规则不会创建云任务。暂停或取消订阅保留已转存文件；未连接 115 时可保存订阅规则，待连接后处理。

## 云端文件命名

新建追新和资源转存默认开启自动改名，已有追新保留原设置。也可在已完成任务中预览并确认手动改名。例如：

```text
作品名 - S02E03 [字幕组] [简日] [1080p].mkv
```

季集号无法确认、集数冲突或文件重名时保留原名，在任务中核对改名预览，确认后继续整理及 VPS 下载。集号偏移仅适用于无显式季号的总集数，例如总集数 22 对应 S02E09 时偏移为 -13。配套字幕随视频改名。改名与文件整理分别校验，均不会覆盖已有文件；整理中断后按原文件 ID 核对并继续。

Sonarr 随默认部署启动，应用自动设置服务地址，并从本部署凭据派生独立的 API Key。校对实例使用临时配置，访问范围为作品查找、季集解析和状态检查。需要使用已有 Sonarr 时，在设置中选择「自定义」并填写地址和 API Key，保存时自动检测可用的 API 版本。

应用按明确 TVDB ID 调用 Sonarr 的 `series/lookup` 和 `parse` 接口校对季集号。明确的冲突会进入复核。解析缺失时，仅在 TMDB 中有唯一对应季集记录、且发行集号一致时进行站内改名。

## 元数据

管理员可在设置中配置 TMDB API Key 或 Read Access Token。未配置时仍可使用本地公开目录。后台优先更新下一季度和本季，历史目录从新到旧分批刷新。上游失败时保留已有资料。来源及署名参见 [目录数据声明](../NOTICE-Catalog-Data.md)。

## 原生运行

需要 Python 3.12+。原生运行可在设置中自定义已有 AList 和 Sonarr，或通过环境变量提供本机服务的启动凭据。自动启动完整服务使用 `python3 tools/start.py`。环境变量参见 [应用配置](../mikan115/config.py)。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export ALIST_URL=http://127.0.0.1:5244
python run.py
```

应用默认监听本机 8788 端口。已有静态构建可直接运行，修改前端后再重新构建。

## 测试与构建

```bash
python -m unittest discover -v
cd frontend
npm ci
npm test
npm run build
```

测试使用模拟的 115、AList 和蜜柑服务。真实账户权限、云下载配额、凭据兼容性以及下载取链需在实际部署环境验证。

## 数据维护

Docker 部署的应用数据位于 `runtime/app`，其中 `library` 存放下载到 VPS 的文件。备份数据库时同时保留 `secret.key` 和私有 `.env`。旧版升级前可保留原 `runtime/alist` 的离线备份。分发包使用项目的公开文件。 [打包脚本](../tools/package.py)

## 技术参考

- [AList v3.66.0](https://github.com/AlistGo/alist/tree/v3.66.0)
- [AList 115 驱动](https://github.com/AlistGo/alist/tree/v3.66.0/drivers/115)
- [AList 115 云下载实现](https://github.com/AlistGo/alist/blob/v3.66.0/internal/offline_download/115/client.go)
- [AutoBangumi RSS 设置](https://www.autobangumi.org/en/config/rss.html)
- [Sonarr API](https://github.com/Sonarr/Sonarr/blob/develop/src/Sonarr.Api.V3/openapi.json)
- [Sonarr 配置项与环境变量](https://github.com/Sonarr/Sonarr/commit/d051dac12c8b797761a0d1f3b4aa84cff47ed13d)
- [LinuxServer Sonarr 镜像](https://docs.linuxserver.io/images/docker-sonarr/)
- [115 驱动文件操作](https://github.com/okatu-loli/115driver/blob/v1.2.3-1/pkg/driver/op.go)
- [tache 任务状态](https://github.com/Xhofe/tache/blob/v0.1.5/state.go)
