# 使用与维护指南

## 115 与 AList

网站使用本地账号。首次在管理员设置中粘贴已有第三方客户端的 115 Cookie 后，应用将加密保存，并用同一份 Cookie 验证 115 会话、挂载 AList。Cookie 到期时，在原客户端获取新 Cookie 后更新设置。连接暂时失败时可点「修复连接」，使用已保存的 Cookie 重试。

Compose 自带 AList，自动使用初始化脚本生成的管理凭据。挂载信息位于临时内存盘；AList 重启后，应用会从加密配置恢复同一个专用目录。使用已有 AList 时，在应用中配置 `ALIST_URL`、`ALIST_USERNAME`，并在连接的高级设置中填写 AList 管理令牌或密码。外部 AList 的存储和备份由其部署者管理。

系统为每个实例创建独立的 115 转存根目录，并按磁力信息哈希隔离任务。请通过网站更新绑定，不要手动修改 AList 专用挂载。日常网盘文件不属于任务管理范围。转存结果不确定时，应先核查任务状态再重试。

## 转存与入库

「转存至 115」可以勾选多个资源。转存完成后，在任务页点「下载到 VPS」，可以选择全部文件或指定文件。文件成功写入并通过大小核验后计入已入库集数。

从详情页的转存弹窗打开补集、全集收录和洗版。确认预览清单后排队。合集集号以实际取得的文件为准。洗版按选定的字幕组、字幕版本或分辨率挑选资源，并保留已入库版本。

## RSS 订阅

支持蜜柑作品 RSS、字幕组 RSS 和个人 MyBangumi RSS。创建订阅时，默认仅处理此后发布的资源；需要下载历史资源时启用“包含现有资源”。

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

在规则中开启自动改名，或在已完成任务中预览并确认手动改名。例如：

```text
作品名 - S02E03 [字幕组] [简日] [1080p].mkv
```

季集号无法确认、集数冲突或文件重名时保留原名。集号偏移仅适用于无显式季号的总集数，例如总集数 22 对应 S02E09 时偏移为 -13。配套字幕随视频改名。改名限制在专用任务目录，不移动、删除或覆盖文件。

Sonarr 是可选的只读校对服务。新部署可执行 `python tools/configure.py --sonarr` 生成部署密钥并启用配置，然后执行 `docker compose --profile sonarr up -d`。也可以先执行不带参数的配置脚本，再将 `.env` 中的 `SONARR_MANAGED` 改为 `1`。托管实例使用临时独立配置，不挂载媒体库或下载目录；不会添加作品、修改媒体库或操作下载器。API Key 由部署密钥按用途派生，不写入 `.env`。

应用仅对已有明确 TVDB ID 的作品调用 Sonarr 的 `series/lookup?term=tvdb:<id>` 和 `parse` 接口校对季集号。托管实例尚未添加作品时，只接受 `parsedEpisodeInfo` 的明确结果；缺失或不一致时保留原文件名并进入复核，不伪造集数。外部 Sonarr 仍可在管理员设置中填写地址、API 版本和 API Key；3/4 使用 v3，5 使用 v5。

## 元数据

管理员可在设置中配置 TMDB API Key 或 Read Access Token。未配置时仍可使用本地公开目录。后台优先更新下一季度和本季，历史目录从新到旧分批刷新。上游失败时保留已有资料。来源及署名参见 [目录数据声明](../NOTICE-Catalog-Data.md)。

## 原生运行

需要 Python 3.12+ 以及可用的 AList 服务。先设置私有 `ALIST_TOKEN` 或 `ALIST_PASSWORD` 和 `MIKAN_SETUP_TOKEN`，环境变量详见 [应用配置](../mikan115/config.py)。

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
