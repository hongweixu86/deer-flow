# DeerFlow musl 离线开发镜像

需要 Nginx、Docker AIO 沙箱及持久化部署时，使用 [离线 Compose 部署](deploy/README.zh-CN.md)。

覆盖 Python 后端、可编辑安装的 `deerflow-harness` 和已构建的 Next.js 前端。
基础镜像为 `python:3.12-alpine3.22`，保留 C/C++、Rust、Node.js、pnpm、Git、Vim
等开发工具。镜像较大，目标是断网开发和重建。

## 1. 联网准备及断网验收

在仓库根目录执行，需要 Bash、tar、gzip 和可用的 Docker daemon：

```bash
./scripts/build-offline.sh prepare
./scripts/build-offline.sh bundle
```

`prepare` 会：

1. 按 `backend/uv.lock` 导出默认依赖及 dev 组，检查锁文件与项目声明一致。
2. 在目标 musl 环境下载 wheel，缺少 wheel 的依赖从源码编译；ONNX Runtime
   和 sqlite-vec 从与锁定版本相同的 Git 标签构建，DuckDB 等通过 `pip wheel` 构建。
   sqlite-vec 保留经过锁文件哈希校验的上游 Python 包装代码和元数据，用本地
   编译的 musl 动态库替换原始动态库，再重新生成 wheel 和文件校验记录。
3. 将 Python 依赖、hatchling/build 等构建依赖保存在 `/opt/offline/wheels`，
   将前端生产和开发依赖保存在 `/opt/offline/pnpm-store`，并预存 tiktoken
   的 `cl100k_base`、`o200k_base` 分词数据到 `/opt/offline/tiktoken`，避免首次使用时
   再下载。主站不可达时回退到 tiktoken-rs 数据镜像，仍由 tiktoken 校验官方 SHA256。
4. 生成资源镜像 `deerflow-offline-resources:local`。
5. 使用 `docker build --network=none --pull=false` 生成包含源码及前端产物的
   `deerflow-offline:local`。
6. 在禁网容器中，以空卷遮蔽虚拟环境、node_modules 和 `.next`，重新安装 Python
   环境、构建 harness wheel、检查源码语法、依赖与原生扩展，再重新安装和构建前端。
   任意步骤失败，命令返回非零，不能视为准备成功。

首次准备需要访问镜像仓库、Alpine、PyPI、npm、GitHub，以及原生库的上游源码
下载地址。ONNX Runtime 和 DuckDB 编译耗时、耗内存，默认只开两个编译任务。
建议至少 16 GB 内存和数十 GB 磁盘。资源只适用于准备时的 CPU 架构、Python ABI
和 Alpine 基础环境；换架构应在目标架构机器上重新准备。

可覆盖参数：

```bash
BUILD_JOBS=2 \
PYTHON_IMAGE=python:3.12-alpine3.22 \
PIP_INDEX_URL=https://pypi.org/simple \
NPM_REGISTRY=https://registry.npmjs.org \
./scripts/build-offline.sh prepare
```

正式归档建议将 `PYTHON_IMAGE` 设为经过验证的镜像 digest，并固定资源镜像标签。
`IMAGE`、`RESOURCE_IMAGE` 可自定义镜像名；`OUTPUT` 可指定交付目录。
源码构建上下文排除了 `.env`、本地配置、虚拟环境、node_modules 和运行数据。

如果已有同 Python/musl/CPU 架构下编译的可信 wheel，可放入
`docker/offline/seed-wheels/`，`prepare` 会优先复用符合锁定版本的 wheel。
这尤其适合复用 DuckDB 等耗时的原生构建。该目录的 wheel 不提交到 Git，
会随资源镜像和交付源码包保存。

## 2. 搬入无外网环境

将 `.offline-output` 整个目录搬入目标机器。它包含：

- `images.tar`：资源镜像及已构建开发镜像，包含全部基础层和离线依赖。
- `source.tar.gz`：前后端源码、skills、示例配置及本套脚本。
- `images.env`：本次镜像名称。
- `SHA256SUMS`：交付文件校验值。

```bash
cd /path/to/offline-output
sha256sum -c SHA256SUMS
docker load -i images.tar
mkdir -p ../deerflow-source
tar -xzf source.tar.gz -C ../deerflow-source
set -a
source images.env
set +a
cd ../deerflow-source
./scripts/build-offline.sh build
./scripts/build-offline.sh verify
```

加载完成后无需再拉取基础镜像或执行 apk/npm 联网安装。离线构建没有外部
Dockerfile syntax frontend。建议使用 Docker 28 或更新版本；脚本不依赖
Dockerfile 专属 ignore 功能，但过旧的容器运行时可能阻止新版 Alpine 工具使用的系统调用。

## 3. 容器内改代码、重装和重建

使用有名称的容器保存直接在容器内的编辑：

```bash
docker run -it --name deerflow-dev --network=none deerflow-offline:local bash
# 容器内
vim /app/backend/packages/harness/deerflow/__init__.py
sh /opt/offline-tools/rebuild.sh backend
sh /opt/offline-tools/rebuild.sh frontend
sh /opt/offline-tools/rebuild.sh all
```

退出后可用 `docker start -ai deerflow-dev` 继续。`scripts/build-offline.sh shell`
是临时容器，退出即删除，适合验证，不适合保存修改。也可以挂载源码目录：

```bash
docker run -it --name deerflow-edit --network=none \
  -v "$PWD/backend:/app/backend" \
  -v "$PWD/frontend:/app/frontend" \
  deerflow-offline:local bash
```

镜像默认以 root 运行，挂载目录中的新文件也可能归 root 所有。
容器内可直接使用 `python`、`pytest`、`ruff`；虚拟环境位于 `/opt/deerflow-venv`，
不会被源码挂载遮蔽。后端根目录是 uv 虚拟项目，没有 wheel 构建定义，`app`
通过 `PYTHONPATH` 加载；真正打包的 harness wheel 输出到 `/app/backend/dist`。
后端使用 editable 安装，普通 Python 修改立即生效；运行中的服务需要重启。
前端构建产物位于 `/app/frontend/.next`，修改后需要重新构建、重启前端。

本地 Python wheel 目录就是简易包仓库，不依赖额外仓库服务：

```bash
python -m pip install --no-index --find-links=/opt/offline/wheels 包名
python -m pip check
```

需要给内网其他容器共享时，可直接提供静态 wheel 目录，无需部署完整 PyPI 服务：

```bash
docker run --rm -p 8080:8080 deerflow-offline-resources:local \
  python -m http.server 8080 --directory /opt/offline/wheels
# 在同架构、同 Python/musl 环境的客户端使用 find-links（不是 index-url）
python -m pip install --no-index --find-links=http://内网包服务器:8080/ 包名
```

`rebuild.sh` 检查锁文件及 wheel 校验值，安装时禁用公网索引。修改依赖版本或
新增库后，必须先在联网环境更新锁文件并重新 `prepare`、`bundle`；仓库未收录的
包无法凭空离线安装。默认不包含 postgres、discord、ollama、pymupdf 可选组。
这里保证项目源码重建，未承诺任意第三方 C/Rust 库源码都能重新编译：它们可能还
需要额外源码、Cargo vendor 或 CMake 下载资源；预编译 wheel 用于离线重装。

## 4. 运行前后端

镜像默认进入 Bash，避免在没有配置时启动服务。按 `config.example.yaml` 准备
实际配置和模型凭据后，可在隔离网络或可访问内网模型的环境运行：

```bash
docker run --name deerflow -p 3000:3000 -p 8001:8001 \
  --env-file .env \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -e DEER_FLOW_CONFIG_PATH=/app/config.yaml \
  deerflow-offline:local bash /opt/offline-tools/start.sh
```

访问 `http://localhost:3000`。前端构建时使用 `127.0.0.1:8001` 作为内部后端地址，
适用于本镜像内同时运行两个进程。启动脚本在任意一个服务退出后停止另一个服务。
配置中的数据库、模型、MCP、sandbox 和搜索服务仍需按实际环境提供；前后端
能够离线构建不代表外部 AI 服务可以断网使用。`--network=none` 用于构建验收和
纯编辑，会同时禁用容器对宿主机和内网服务的连接。

## 参考

- [pip wheel：本地 wheel 仓库](https://pip.pypa.io/en/stable/cli/pip_wheel/)
- [uv：锁文件和依赖导出](https://docs.astral.sh/uv/concepts/projects/sync/)
- [pnpm install：offline 和 frozen-lockfile](https://pnpm.io/cli/install)
- [ONNX Runtime 源码构建](https://onnxruntime.ai/docs/build/inferencing.html)
- [tiktoken 缓存与哈希校验](https://github.com/openai/tiktoken/blob/main/tiktoken/load.py)
- [tiktoken-rs 编码数据镜像](https://github.com/zurawiki/tiktoken-rs/tree/main/tiktoken-rs/assets)
