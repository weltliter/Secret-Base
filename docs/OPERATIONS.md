# 运维与验证

本机命令在源码根目录执行；服务器示例目录为 `/opt/wechat-vault`。运行数据、密钥、模型和备份分开保存。

## 部署与更新

服务器需要 Docker Engine、Docker Compose，以及宿主机上的 `python3`、`tar` 和 `flock`。新部署先准备运行目录、查询机器密钥和公开模型：

```sh
cd /opt/wechat-vault
install -d -m 700 data backups .maintenance
install -d -o 10001 -g 70 -m 700 data/vault .maintenance/checks
install -d -o 70 -g 70 -m 770 data/pgsocket
chmod 700 ops/backup/backup.sh ops/backup/restore.sh ops/security/init-query-key.sh
./ops/security/init-query-key.sh
python3 -B ops/models/fetch-embedding.py --source hf-mirror.com
docker compose up -d --build
docker compose ps
```

查询机器密钥为 `secrets/query-unlock.key`，目录 root:root、0700，文件 10001:70、0400，以只读方式挂载。初始化脚本重复运行会验证已有密钥，不覆盖它。第一次管理登录会启用查询自动恢复；机器密钥或启动信封缺失、损坏、不匹配时，修复文件后重新登录。

更新发布前执行备份，上传 `vault/`、`ops/`、`docs/`、`Dockerfile`、`compose.yaml`、`requirements.txt`、`.dockerignore`、`README.md` 和 `VERSION`，排除缓存、私钥、`.local/`、`.venv/`、本机工具和单元测试。源码交付包不含运行数据，不应整包覆盖服务器。展开后删除上传包，运行：

```sh
cd /opt/wechat-vault
./ops/backup/backup.sh
docker compose config --quiet
docker compose build app
# 运行下文的隔离联调，通过后更新应用容器。
docker compose up -d --no-deps app
docker compose ps
```

应用入口是 `vault.web.app:app`，以 UID 10001、GID 70 运行，内存上限 512 MiB、禁止 swap；数据库上限 160 MiB。应用发布到服务器 `127.0.0.1:18080`，PostgreSQL 仅监听共享 Unix socket，以内部角色 `vault_app` 连接。

自动恢复启用后，微信查询不依赖电脑、SSH 隧道或浏览器登录；服务断线自动重连，微信授权失效需重新扫码。检查本机连接用 `console.ps1 -Action Status`，这只报告查询状态，不代表浏览器已登录。

## 备份与恢复

手动备份命令：

```sh
/opt/wechat-vault/ops/backup/backup.sh
```

备份存于 `/opt/wechat-vault/backups`，脚本执行时清理超过 7 天的备份，不自动安装定时任务。每个包包含 `database.dump` 和配套 `vault.json`；加密记录、设置和查询日志在数据库转储中，机器密钥及查询启动信封不进入备份。记录编号、时间和表结构等技术元数据可能可见。应在服务器以外保存备份副本。

恢复会替换当前密码库，先确认备份文件及对应的主密码或恢复密钥，再执行：

```sh
/opt/wechat-vault/ops/backup/restore.sh \
  /opt/wechat-vault/backups/<实际备份文件名>.tar.gz \
  --confirm-replace
```

脚本先备份当前已初始化的库，停止应用，在单个事务中恢复数据库，成功后替换配套密钥信封；数据库事务不覆盖后续文件操作。旧查询启动信封会移除；恢复后登录一次重新启用查询自动恢复。失败时应用可能保持停止，应按错误修复，不要生成新密钥信封覆盖旧文件。

## 本机验证

干净工作区先安装 Python（容器使用 3.12；本次本机测试使用 3.13.15）和 Node.js，然后在项目根目录创建维护环境：

```powershell
python -m venv .venv
```

已有 `.venv` 时不必重复创建。安装维护依赖并运行分组测试，不生成 `.pyc` 或 pytest 缓存：

```powershell
New-Item -ItemType Directory -Path .local/checks -Force | Out-Null
.venv\Scripts\python.exe -B -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --basetemp=.local/checks/pytest
node --test tests/frontend/filters.test.mjs
.\tests\client\tunnel.ps1
.\console.ps1 -Action Check
.\console.ps1 -Action Status
```

测试使用合成记录和模拟通道；不会向微信发送消息。`.local/checks/pytest` 是 pytest 可重建临时目录，不放业务文件。

## 云端隔离验证

联调容器使用项目内 `.maintenance/checks/` 作为临时目录，不挂载项目以外的主机路径：

```sh
cd /opt/wechat-vault
install -d -o 10001 -g 70 -m 700 .maintenance/checks
docker compose run --rm --no-deps -T \
  -e TMPDIR=/checks \
  -v /opt/wechat-vault/.maintenance/checks:/checks \
  --entrypoint python app -B - < ops/checks/integration.py
```

该检查用临时密钥及独立 PostgreSQL schema 验证加密、增改查、版本冲突、回收站、微信只读、管理注销与查询自动恢复，结束后移除自己的 schema 和临时文件。不会初始化或修改正式密码库，也不发送微信消息。

模型资源检查使用同一临时目录约束：

```sh
cd /opt/wechat-vault
docker compose run --rm --no-deps -T \
  -e TMPDIR=/checks \
  -v /opt/wechat-vault/.maintenance/checks:/checks \
  --entrypoint python app -B - < ops/checks/embedding_resources.py
```

资源检查只用合成记录和临时密钥，包含生产强度 Argon2id 与长描述检索并发；峰值超过 480 MiB 时失败。检查结束后仅清理 `.maintenance/checks/` 内本次生成的临时内容，不清理数据、密钥、模型或备份目录。

## 模型与资源依据

云端使用 [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) 的 [Xenova 量化 ONNX 固定版本](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781)。下载脚本 `ops/models/fetch-embedding.py` 校验 SHA-256，只下载公开权重和 tokenizer，不执行远端 Python。模型位于 `models/bge-small-zh-v1.5/`，应用只读挂载到 `/models`，推理单线程并关闭 ONNX 遥测。

业务向量只在内存中按需生成；描述相同复用缓存，修改后更新，删除后移出，重启后重建。数据库不保存明文向量。

以下为 2026-10-27 在实际云服务器上的历史实测，未使用个人电脑性能作为依据：

| 项目 | 配置或实测 |
| --- | --- |
| 云服务器 | 2 vCPU，约 1.7 GiB 内存，同时运行原有其他服务 |
| 评估容器 | 1 vCPU、512 MiB、禁止 swap，模型只读挂载 |
| 模型 | 512 维，权重 24,010,842 字节，tokenizer 439,125 字节 |
| 完整应用加载后 RSS | 141.1 MiB |
| 冷启动、构建 4 条索引并查询 | 471–994 ms |
| 模型已加载的单次向量查询 | 11–13 ms |
| 256 MiB Argon2id 与长描述检索并发峰值 | 374.7 MiB |
| 实际描述与 DeepSeek Flash 三次总查询耗时 | 777–1471 ms，包含模型接口耗时 |

当前数据规模满足 512 MiB 限额；大量记录或并发下需重新评估。首次建索引耗时会随描述分块数量增加，真实耗时和降级原因可在管理端查询日志查看。

## 日志与清理

管理端显示最近 50 次查询，日志正文整体加密，保留 30 天、最多 2000 条。运行统计用：

```sh
cd /opt/wechat-vault
docker compose logs --since 30m --tail 100 app
```

运行日志仅含请求编号、候选数、调用数、弱匹配数和耗时，不含查询原文或凭据。固定降级码包括 `http_401`（模型鉴权）、`http_429`（限流）、`timeout`、`invalid_response`、`model_disabled`；`vector_status=unavailable` 表示嵌入失败并降级关键词检索。

只清理项目内已确认的缓存、预览和上传产物；运行数据、密钥、模型及备份按各自保留规则维护。安全边界见 [安全说明](security/SECURITY.md)，网络配置见 [安全组说明](security/SECURITY-GROUP.md)。

