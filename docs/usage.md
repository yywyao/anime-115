# 使用与维护指南

## 115 与 AList

在管理员设置中绑定已有第三方客户端的 115 Cookie（UID、CID、SEID、KID）。Cookie 失效后手动更新；系统不提供扫码或自动重新登录功能。网站登录使用独立的本地账号。

Compose 自带 AList，使用初始化脚本生成的独立管理员密码。使用已有 AList 时，可通过管理员设置填写管理令牌或密码，并配置应用环境变量 `ALIST_URL`、`ALIST_USERNAME`。

系统为每个实例创建独立的 115 转存根目录，并按磁力信息哈希隔离任务。请通过网站更新绑定，不要手动修改 AList 专用挂载。日常网盘文件不属于任务管理范围。转存结果不确定时，应先核查任务状态再重试。

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

使用预览检查规则不会创建云任务。暂停或取消订阅保留已转存文件；未连接 115 时可保存订阅规则，待连接后处理。

## 云端文件命名

在规则中开启自动改名，或在已完成任务中预览并确认手动改名。例如：

```text
作品名 - S02E03 [字幕组] [简日] [1080p].mkv
```

季集号无法确认、集数冲突或文件重名时保留原名。集号偏移仅适用于无显式季号的总集数，例如总集数 22 对应 S02E09 时偏移为 -13。配套字幕随视频改名。改名限制在专用任务目录，不移动、删除或覆盖文件。

Sonarr 为可选服务，通过状态及解析接口只读校对具有明确 TVDB ID 的作品季集号。Sonarr 3/4 使用 API v3，Sonarr 5 使用 API v5；不修改 Sonarr 媒体库或下载器。

## 元数据

管理员可在设置中配置 TMDB API Key 或 Read Access Token。未配置时仍可使用本地公开目录。后台自动刷新当前及下一季度，并分批更新历史目录；上游失败时保留已有资料。来源及署名参见 [目录数据声明](../NOTICE-Catalog-Data.md)。

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

Docker 部署的持久数据位于 `runtime/app` 和 `runtime/alist`。备份数据库时同时保留 `secret.key`，并安全保存 `.env`。分发包可通过 [打包脚本](../tools/package.py) 生成，勿包含私有配置及运行数据。

## 技术参考

- [AList v3.66.0](https://github.com/AlistGo/alist/tree/v3.66.0)
- [AList 115 驱动](https://github.com/AlistGo/alist/tree/v3.66.0/drivers/115)
- [AList 115 云下载实现](https://github.com/AlistGo/alist/blob/v3.66.0/internal/offline_download/115/client.go)
- [AutoBangumi RSS 设置](https://www.autobangumi.org/en/config/rss.html)
- [Sonarr API](https://sonarr.tv/docs/api/)
- [115 驱动文件操作](https://github.com/okatu-loli/115driver/blob/v1.2.3-1/pkg/driver/op.go)
- [tache 任务状态](https://github.com/Xhofe/tache/blob/v0.1.5/state.go)
