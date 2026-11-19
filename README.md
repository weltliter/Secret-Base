# 微信个人密码库

> 本仓库为首次公开发布，按功能里程碑重建了 0.1 至 1.1 的 Git 历史。提交日期是 2026 年 11 月 2 日至 19 日（北京时间）的模拟时间线。

当前版本 **1.1，含数据库上传全量恢复补丁**。账号资料加密保存在自己的 ECS，通过 Windows 管理页维护，通过绑定的微信查询。

**使用方式：先部署 `ecs/`，再用 Windows 的 `admin/` 建立 SSH 隧道打开管理页。首次登录设置主密码、保存恢复密钥、配置模型并绑定微信；之后通过微信查询，在管理页新增、修改、备份或恢复数据。**

**每端只维护一份环境配置：`admin/config.env` 和 `ecs/config.env`。更换服务器、私钥、端口或数据目录只改对应配置，不修改程序代码。真实配置不上传 GitHub；仓库只提供 `config.example.env` 模板。**

```text
secret/
├── admin/       Windows 管理入口、SSH 隧道与客户端测试
├── ecs/         服务程序、网页、部署工具、测试与技术文档
└── README.md    使用与配置说明
```

网页由 ECS 提供，HTML、CSS 和 JavaScript 位于 `ecs/vault/web/`。隐藏的 `.git/` 是本地版本历史。

## 1. 部署 ECS

服务器需要 Linux、已启动的 Docker Engine、Compose 插件、Python 3.10+、tar 和 flock。将 `ecs/` 放在服务器上，首次部署以 root 执行：

```sh
cd ecs
cp config.example.env config.env
chmod 600 config.env
# 先编辑 config.env，核对状态目录、端口、镜像和下载源。
sh manage.sh deploy
sh manage.sh ps
```

部署会检查 `ecs/config.env`，创建状态目录和机器查询密钥，下载并校验公共模型，再构建应用镜像、安装 Python 依赖并启动应用与 PostgreSQL。所有运维命令统一从 `sh manage.sh ...` 进入，读取同一份 `ecs/config.env`。

| 字段 | 模板值 | 用途 |
| --- | --- | --- |
| `VAULT_PROJECT_NAME` | `wechat-vault` | Compose 项目名称；已有部署保持原名称 |
| `VAULT_STATE_DIR` | `/var/lib/wechat-vault` | 专用绝对路径；保存数据库、备份、模型及运行密钥，不允许符号链接 |
| `VAULT_ADMIN_PORT` | `18080` | ECS 回环监听端口，10000–65535；不开放公网 |
| `VAULT_APP_IMAGE` | `wechat-vault-app:local` | 本项目构建的应用镜像名称 |
| `POSTGRES_IMAGE` | `postgres:17-alpine` | 数据库镜像；保持 PostgreSQL 17 兼容版本 |
| `PYTHON_BASE` | `python:3.12-slim-trixie` | 应用构建基础镜像 |
| `PIP_INDEX_URL` | `https://pypi.org/simple` | HTTPS 依赖源，不接受含凭据或查询参数的地址 |
| `VAULT_MODEL_SOURCE` | `huggingface.co` | 公共模型源，也支持 `hf-mirror.com`；下载后校验固定摘要 |
| `VAULT_BACKUP_TIMEOUT_SECONDS` | `1800` | 备份生成与上传恢复处理时限（秒） |
| `VAULT_BACKUP_RETENTION_DAYS` | `7` | 网页备份保留天数 |
| `VAULT_BACKUP_QUOTA_BYTES` | `5368709120` | 网页备份与临时文件配额（字节） |
| `VAULT_BACKUP_RESERVE_BYTES` | `1073741824` | 最低磁盘空闲预留（字节） |
| `VAULT_DOWNLOAD_TIMEOUT_SECONDS` | `1800` | 单次下载最长时限（秒） |
| `VAULT_DOWNLOAD_CONCURRENCY` | `2` | 同时下载数量 |

数据库仅使用容器间共享的 Unix socket。应用只发布到 `127.0.0.1`，由 Compose 传入所需配置。

### 日常运维

在服务器的 `ecs/` 目录使用以下命令。

| 命令 | 用途 |
| --- | --- |
| `sh manage.sh start` | 重新启动已部署的应用与数据库容器 |
| `sh manage.sh stop` | 停止应用与数据库，保留容器和数据 |
| `sh manage.sh ps` | 查看运行状态 |
| `sh manage.sh logs --since 30m --tail 100 app` | 查看应用日志，数据库日志把 `app` 换成 `db` |
| `sh manage.sh backup` | 数据库运行时生成全库 `.dump` 和 SHA-256，写入 `VAULT_STATE_DIR/backups/manual/` |
| `sh manage.sh up -d --build` | 更新源码或配置后，构建并应用更新 |

用 `sh manage.sh help` 查看命令。其他 Docker Compose 操作也可直接传给 `manage.sh`。换机或无法登录管理页时，按[恢复指南](ecs/docs/RECOVERY.md)使用 `sh manage.sh restore`。

## 2. 配置 Windows 管理端

在 Windows PowerShell 中进入 `admin/`，通过配置向导生成唯一配置文件：

```powershell
cd admin
.\console.ps1 -Action Configure
.\console.ps1 -Action Check
.\console.ps1 -Action Open
```

也可复制 `config.example.env` 为 `config.env` 后填写。配置向导只负责写入这份文件，不生成第二份配置。使用 Windows PowerShell 5.1 或更新版本，并安装 Windows OpenSSH 客户端。

| 字段 | 用途 |
| --- | --- |
| `VAULT_SSH_HOST` | 自己的 ECS IPv4 地址或主机名 |
| `VAULT_SSH_USER` | SSH 登录用户名 |
| `VAULT_SSH_PORT` | SSH 端口，通常为 `22` |
| `VAULT_SSH_KEY_FILE` | 本机私钥路径，绝对路径或相对 `admin/` 的路径；只引用文件，不填写私钥内容 |
| `VAULT_LOCAL_PORT` | 本机管理页端口，默认 `18080`，允许 10000–65535 |
| `VAULT_REMOTE_PORT` | 必须与 ECS 的 `VAULT_ADMIN_PORT` 一致 |

**日常双击 `admin/打开管理页.cmd`；使用期间保持启动窗口开启，结束后按 `Ctrl+C`。** 如果端口被占用，修改 `admin/config.env` 中的 `VAULT_LOCAL_PORT`。改变配置后重开隧道。SSH 私钥的受限权限副本和主机指纹自动保存在忽略的 `admin/.local/`，属于运行文件，无需再维护一套配置。

首次连接应核对主机指纹。更换服务器后修改同一配置文件；主机密钥变化时先核实，不关闭主机校验。

## 3. 初始化、查询和恢复

**首次打开管理页：设置主密码 → 将恢复密钥另存到安全位置 → 填写 DeepSeek API Key → 扫码绑定微信 → 录入账号。** 微信只提供查询，账号新增、修改、删除和恢复通过管理页进行。管理会话闲置 5 分钟或持续 1 小时后锁定，管理端锁定不停止微信查询。

**“备份与灾备”页面可下载数据库 `.dump`，也可上传数据库文件、勾选覆盖确认后全量恢复。恢复会替换全部现有应用数据，成功后使用备份对应的主密码或恢复密钥重新登录。** 无效备份及事务失败不会清空原数据；建议恢复前另存当前备份。新服务器尚未初始化时使用[命令行灾备流程](ecs/docs/RECOVERY.md)。

主密码、恢复密钥、业务账号和微信授权不是部署环境配置：主密码不落盘，恢复密钥由用户保管，API Key 和业务设置通过管理页加密保存到数据库。SSH 私钥和服务器自动生成的机器密钥仍作为私有文件保存，配置只引用路径。不要把这些秘密写进模板、源码或 README。

## 配置格式与旧版本迁移

两端使用 UTF-8 的 `KEY=VALUE`，支持空行、整行 `#` 注释和成对引号。模板列出的字段必须齐全且只能出现一次；不支持变量展开、命令执行、行尾注释或额外键。包含空格的路径可加双引号；ECS 使用 Linux 路径。更新设置只编辑对应的 `config.env`。

- 旧管理端：将 `.local/console.psd1` 中的地址、用户、私钥路径和端口转入 `admin/config.env`，补充 SSH 端口；旧文件不再读取，可留作私有归档。
- 旧 ECS：从新模板创建 `ecs/config.env`，把旧 `.env` 或部署环境中的值转入，尤其核对 `VAULT_STATE_DIR`、`VAULT_PROJECT_NAME`、端口与镜像。保留原数据目录和 Compose 项目名称，不能直接用模板默认值替代现有部署。
- 只完成配置迁移不会迁移数据库或重启线上服务。升级前先备份，在隔离环境验证，再按[运维指南](ecs/docs/OPERATIONS.md)执行。1.0 数据升级另需[显式迁移](ecs/docs/RECOVERY.md)。

## 开发与验证

Python 与网页测试在 `ecs/`，Windows 客户端测试在 `admin/`。测试使用合成数据：

```powershell
cd ecs
python -m venv .venv
New-Item -ItemType Directory -Path .local/checks -Force | Out-Null
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.local/checks/pytest
node --test tests/frontend/*.test.mjs
cd ../admin
.\tests\client\tunnel.ps1
```

真实 PostgreSQL 恢复测试需要显式指定专用合成测试库，未配置时跳过；详见[运维与验证](ecs/docs/OPERATIONS.md)。

## GitHub 发布范围

公开范围只有 `admin/`、`ecs/` 和本 README 中的通用源码、测试与模板。两端 `.gitignore` 排除实际 `config.env`、`.local/`、私钥、数据库、备份、模型、依赖和生成包。Docker 构建采用白名单，只复制应用源码、依赖清单和版本文件。

整理后的源码导出入口：

```powershell
python ecs/ops/release/package.py --check
python ecs/ops/release/package.py
```

打包工具核对 Git 跟踪清单及必需入口，拒绝私有文件，输出到忽略的 `ecs/releases/`。变更须先纳入 Git 索引，再打包；导出包带文件校验清单，不含真实配置，使用者按模板创建自己的两份配置。

**上传 GitHub 时，从检查后的源码包建立干净仓库。不要复制当前 `.git/`，也不要推送所有旧分支、标签或使用 `--mirror`。** 本地旧历史含有真实连接信息，当前的 `.gitignore` 不会删除历史内容。旧版本与完整运维记录仅留在本机私有目录用于回退。

更多说明：[管理端指南](admin/USAGE.md) · [项目架构](ecs/docs/ARCHITECTURE.md) · [数据边界](ecs/docs/DATA-BOUNDARY.md) · [安全说明](ecs/docs/security/SECURITY.md)
