# Mikan115

自托管动漫资源管理与自动追番工具，集成蜜柑计划、115 网盘及 AList，提供动漫检索、RSS 订阅、云端转存、文件命名和下载管理。

## 功能特性

- **动漫目录**：支持季度浏览、搜索、作品详情及 Bangumi、TMDB 元数据同步。
- **云端转存**：选择蜜柑计划资源或提交磁力链接，通过 115 云下载保存至专用目录。
- **RSS 追番**：支持作品、字幕组及个人 MyBangumi 订阅，可按字幕版本、分辨率、集数和关键词筛选资源。
- **智能去重**：支持字幕组优先级、候选等待、资源去重及任务状态跟踪。
- **文件命名**：支持云端自动改名、季集编号、配套字幕命名及可选的 Sonarr 集号校对。
- **下载管理**：通过 AList 代理下载已完成的文件，支持 HTTP Range 请求，无需在服务器存放完整视频。
- **账号管理**：支持本地管理员与普通账号，凭据加密存储，云盘任务目录隔离。

## 快速开始

### 环境要求

- Git
- Python 3.12+
- Docker 与 Docker Compose v2
- 可用的 115 网盘账号及受支持的第三方客户端 Cookie（用于转存）

### Docker Compose

```bash
git clone https://github.com/yywyao/anime-115.git
cd anime-115
python3 tools/configure.py
docker compose up -d --build
```

初始化脚本生成私有 `.env` 文件及运行数据目录。已有配置时无需再次运行。

查看服务状态：

```bash
docker compose ps
docker compose logs -f app
```

默认服务地址：

| 服务 | 地址 |
| --- | --- |
| Mikan115 | http://127.0.0.1:8788 |
| AList | http://127.0.0.1:5244 |

两个服务默认仅监听本机地址。远程服务器可通过 SSH 端口转发访问：

```bash
ssh -L 8788:127.0.0.1:8788 user@server
```

使用域名部署时，可参考 [Caddy 配置示例](deploy/Caddyfile.example)，启用 HTTPS 并在 `.env` 中设置 `MIKAN_SECURE_COOKIE=1`，随后执行 `docker compose up -d`。请勿直接对公网开放 AList 管理端口。

### 初始化

1. 打开 Mikan115，使用 `.env` 中的 `MIKAN_SETUP_TOKEN` 创建管理员账号。
2. 在设置中绑定已有的 115 第三方客户端 Cookie。
3. 按需配置 TMDB API 和 Sonarr 服务。
4. 浏览动漫资源，创建转存任务或 RSS 订阅。

## 工作流程

```mermaid
flowchart LR
    A[蜜柑计划 / RSS] --> B[资源筛选]
    B --> C[115 云下载]
    C --> D[文件核验与整理]
    D --> E[AList 代理下载]
```

云下载完成并通过文件核验后，资源将在转存任务中提供下载。默认追番规则仅处理订阅后发布的新资源，可按需启用历史资源处理。

## 配置

| 配置项 | 用途 |
| --- | --- |
| `MIKAN_PORT` | Web 服务端口，默认 8788 |
| `ALIST_PORT` | AList 端口，默认 5244 |
| `MIKAN_SECURE_COOKIE` | HTTPS 部署时设置为 1 |
| `MIKAN_URL` | 蜜柑计划服务地址 |
| `MIKAN_SETUP_TOKEN` | 首次管理员初始化令牌 |
| `ALIST_PASSWORD` | AList 管理密码 |

完整配置示例见 [`.env.example`](.env.example)。115 Cookie、TMDB 凭据及 Sonarr API Key 通过管理员设置管理，请勿将真实凭据提交到仓库。

## 更新与备份

更新前备份 `runtime/app`、`runtime/alist` 和私有 `.env`，数据库与配套加密密钥应一并保留。

```bash
git pull --ff-only
docker compose up -d --build
```

## 文档

- [使用与维护指南](docs/usage.md)：订阅规则、文件命名、第三方服务、原生运行及测试。
- [目录数据来源与署名](NOTICE-Catalog-Data.md)

## 相关项目

- [蜜柑计划](https://mikanani.me/)
- [AList](https://github.com/AlistGo/alist)
- [Bangumi](https://bangumi.tv/)
- [TMDB](https://www.themoviedb.org/)
- [Sonarr](https://sonarr.tv/)
- [AutoBangumi](https://www.autobangumi.org/)

本项目使用 TMDB 数据，但未经 TMDB 认可或认证。第三方组件及数据的许可与署名参见 [目录数据声明](NOTICE-Catalog-Data.md) 和 [React 许可声明](NOTICE-React-MIT.txt)。

## 问题反馈

功能建议和问题请通过 [GitHub Issues](https://github.com/yywyao/anime-115/issues) 提交。反馈时请附上复现步骤及脱敏日志。

请遵守相关服务条款，仅处理有权使用的资源。实际云下载能力取决于 115 账号权限、配额和第三方服务可用性。
