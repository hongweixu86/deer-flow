# DeerFlow 离线 Compose 部署

本目录可独立搬入 Linux x86_64 主机，需要已安装 Docker Engine 和 Compose v2。
不依赖目标主机安装 Python、Node.js 或在线拉取镜像。

## 联网制作交付包

在 DeerFlow 仓库根目录，已有经过验证的 `deerflow-offline:local` 和资源镜像后：

```bash
./scripts/build-offline-deploy.sh prepare
./scripts/build-offline-deploy.sh bundle
```

输出默认在 `.offline-deploy-output/`。`NGINX_IMAGE`、`SANDBOX_IMAGE` 可指定
上游 digest；交付包的 `images.json` 记录实际镜像 ID 和 digest。交付包包括：

- 原应用及资源镜像；
- `deerflow-offline-deploy:local`：在应用镜像上补充 Docker CLI、curl、tmux；
- `deerflow-offline-nginx:local`：拉取的 Nginx 镜像；
- `deerflow-offline-sandbox:local`：拉取的 AIO 沙箱镜像；

原 Compose 的前后端服务在这里由同一个 app 容器运行，其网络别名保留 gateway、
frontend，复用原 Nginx 路由。这样也保持已构建前端的 `127.0.0.1:8001` 内部地址有效。
默认访问端口 2026，修改 `.env` 中的 PORT 即可。没有额外 PostgreSQL/Redis 服务，
默认使用 SQLite 持久化。模型及 MCP 服务需另外提供。

## 离线导入与初始化

将整个交付目录复制到最终部署位置，再执行：

```bash
./deploy.sh load
./deploy.sh init aio     # 独立 Docker 沙箱；也可选 local
```

初始化不会覆盖已有配置，会生成独立随机的服务密钥。请编辑：

- `config/config.yaml`：配置内网模型地址、模型名和实际需要的工具；初始 models 为空。
- `runtime.env`：模型凭据等环境变量，不要提交到 Git 或分享。
- `.env`：端口、部署绝对路径、沙箱模式。

`init aio` 会启用宿主机 Docker socket 挂载，应用因此可管理宿主机容器；应在受控
部署主机上使用。沿用上游的动态沙箱端口映射，应用通过 host.docker.internal
访问它们；不要将沙箱端口对不可信网络开放。`local` 模式不挂载 Docker socket，
且保留 `allow_host_bash: false` 默认值。

```bash
./deploy.sh up
./deploy.sh ps
./deploy.sh logs
# 浏览器访问 http://服务器IP:2026
```

Compose 设置 `pull_policy: never`，启动使用 `--no-build --pull never`。启动脚本
先检查沙箱镜像存在，动态沙箱使用已导入的本地标签。bridge 网络保留容器互通及
内网模型访问能力，禁止外网访问请由部署网络策略控制；不能使用 network=none
来替代可用的服务网络。

## 数据持久化与备份

| 宿主机目录 | 容器路径 | 内容 |
| --- | --- | --- |
| `data/` | `/data` | 用户、会话文件、上传、产物、认证数据及其他 DEER_FLOW_HOME 数据 |
| `data/database/` | `/data/database` | SQLite 数据库、检查点、运行记录；run_events 使用 db |
| `config/` | `/deployment` | 主配置和可更新的 MCP 扩展配置 |
| `skills/` | `/app/skills` | 初始化复制的技能文件及后续修改 |

容器重建、`down` 后 `up` 不删除这些目录。Docker 动态沙箱的工作目录映射到同一
data 树，沙箱销毁后已写入挂载目录的数据保留；沙箱容器其他路径中的临时文件不保留。
日志使用 Docker json-file，20 MB × 3 轮转。进行中的内存任务不会跨重启继续执行。

停服后备份数据库及配置，避免复制正在写入的 SQLite WAL 文件：

```bash
./deploy.sh down
sudo tar -czf ../deerflow-data-backup.tar.gz data config skills .env runtime.env
./deploy.sh up
```

备份含凭据，应妥善保管。恢复时先解压到部署目录，若迁移了目录位置，更新 `.env`
中的 DEPLOY_ROOT（Docker 沙箱挂载需要真实宿主机绝对路径）。容器写出的文件通常归 root。

## 容器内开发

```bash
./deploy.sh exec
# 容器内：
vim /app/backend/packages/harness/deerflow/__init__.py
sh /opt/offline-tools/rebuild.sh all
exit
# 宿主机：
docker compose --env-file .env -f compose.yaml restart app
```

源码修改保存在 app 容器可写层，restart 保留、重建容器会丢失；上表中的数据则持久化。
需要保留源码修改时，另行绑定源码目录或将修改导出到源码仓库。可用 curl 和 tmux。
