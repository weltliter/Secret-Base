# 运维与验证

服务端命令在项目的 `ecs/` 目录或服务器对应发布目录执行；Windows 客户端命令在 `admin/` 目录执行。源码、状态目录和本机 SSH 身份分开保存，更新源码不覆盖数据库、运行密钥、模型、备份或私有配置。

## 部署与升级

首次部署、服务起停、日志和手动备份见 [README 的 ECS 部署步骤](../../README.md#1-部署-ecs)。已有部署应保留原状态目录和 Compose 项目名称，从 1.0 升级先备份并按恢复指南显式迁移。

新服务器恢复已有数据使用 `sh manage.sh deploy --prepare-only`，再按 [恢复指南](RECOVERY.md) 构建应用并恢复数据库。

升级已迁移的 1.1 密码库前先备份，再获取兼容发布版本、验证并更新应用：

```sh
sh manage.sh backup
sh manage.sh config --quiet
sh manage.sh build app
# 在独立测试环境完成恢复验收后更新。
sh manage.sh up -d --no-deps app
sh manage.sh ps
```

应用入口为 `vault.web.app:app`，以 UID 10001、GID 70 运行，内存上限 512 MiB、禁止 swap；数据库上限 160 MiB。生产数据库仅监听共享 Unix socket，管理页只发布到服务器 `127.0.0.1:18080`。不向应用挂载 Docker socket 或任何 ECS 私钥。

## 备份、迁移与恢复

管理页可按需创建后台全库备份并下载到本机。新备份为 PostgreSQL 17 custom-format `.dump`，包括业务表、序列、加密设置/日志、数据库版本与加密信封，不包含主密码、恢复密钥、查询机器密钥或启动信封。校验说明独立下载；文件存储、生成状态和本地恢复验证是不同阶段。

| 限制 | 默认值 |
| --- | --- |
| 同时生成 | 1 个任务 |
| 同时下载 | 2 条传输 |
| 生成超时 | 30 分钟 |
| 单次下载授权 | 最长 30 分钟，可在管理会话锁定后继续当前传输 |
| 网页备份保留 | 7 天 |
| 网页目录配额 | 5 GiB，包含临时生成文件 |
| 最低磁盘空闲 | 1 GiB |

限制可在 `config.env` 中配置。任务、下载和自动刷新不会延长管理会话；断线后重新登录领取仍有效的任务，从头下载，不支持断点续传。失败文件不发布，启动时与每小时清理过期任务，正在使用的文件被保留。清理不会影响用户已下载到本机的副本；不自动安装定时备份任务。

1.1 补丁也可在管理页选择 `.dump` 文件，勾选覆盖确认后上传并全量恢复。上传按块落盘，仅该接口使用备份配额作为请求上限，其他接口仍限制 256 KiB；上传与展开文件均计入目录配额。完整校验后，七张应用表及审计序列在同一事务中替换，失败保留原数据。恢复与备份、迁移共用维护锁。成功后旧管理会话全部失效，原查询启动信封被清除，必须使用备份的原主密码或恢复密钥重新登录。具体步骤见 [恢复指南](RECOVERY.md)。

命令行恢复、回退备份和 1.0 旧格式迁移的执行步骤见 [恢复指南](RECOVERY.md)。

## 本机回归检查

安装 Python 和 Node.js，在项目的 `ecs/` 目录运行：

```powershell
python -m venv .venv
New-Item -ItemType Directory -Path .local/checks -Force | Out-Null
.venv\Scripts\python.exe -B -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --basetemp=.local/checks/pytest
Get-ChildItem tests/frontend -Filter '*.test.mjs' | ForEach-Object {
    node --test $_.FullName
    if ($LASTEXITCODE -ne 0) { throw 'Frontend checks failed' }
}
..\admin\tests\client\tunnel.ps1
```

单元测试使用合成记录，不向微信发送消息。真实 PostgreSQL 事务测试需显式设置 `VAULT_TEST_DSN` 指向专用测试数据库；测试只创建并删除自己随机命名的 schema。默认不运行这些外部依赖测试，不能将跳过视为已完成恢复验收。

上传恢复的真实数据库测试位于 `tests/web/test_restore_postgres.py`。它要求 PostgreSQL 17 客户端和显式的 `VAULT_RESTORE_TEST_DSN`，数据库名必须以 `vault_restore_test_` 开头；测试会全量重置这个专用测试库，绝不能指向正式库。覆盖真实备份下载后上传、不同密钥密码库替换、全部应用表/审计序列、损坏文件拒绝和删除后异常回滚。

## 完整隔离恢复演练

`ops/checks/recovery.py` 默认创建超过 512 MiB 的不可压缩合成密文，通过真实 HTTP 生成、下载、注销和重新登录，再将下载的单数据库文件恢复到另一全新数据库和机器运行目录。它验证主密码/恢复密钥、回收站、设置、日志、消息去重、序列、密文边界和应用内存。

演练需独立 PostgreSQL 17 测试容器，不能使用正式服务。只挂载 `ops/checks/`，不要挂载含私钥的整个工作区。运行器要求显式设置 `VAULT_TEST_ADMIN_DSN` 指向本地测试服务，并传入 `--confirm-isolated`；仅创建和清理随机 `vault_recovery_*` 测试数据库。应用测试镜像应设置 512 MiB 内存与禁止 swap。

`--quick` 只用于调试，跳过大文件检查，不能代表完整验收。`--report <路径>` 输出本次检查报告；验证结果以实际运行报告和发布说明为准，本文件不预先宣称验收通过。更多运行边界见 [恢复指南](RECOVERY.md)。

原 `ops/checks/integration.py` 可用于专用测试库的隔离 schema 联调；`embedding_resources.py` 用合成记录检查模型资源。二者均不应通过正式应用的挂载配置直接运行，以免测试不必要地接触正式运行密钥。

## 模型、日志与源码发布

模型采用固定版本的 BGE-small 中文量化 ONNX 文件。`ops/models/fetch-embedding.py` 校验公开模型 SHA-256，不执行远端 Python，文件只读挂载到 `/models`。业务向量只在内存生成，重启后重建。历史 1.0 的资源测量不能替代新增备份路径和大数据量下的验证。

管理查询日志加密保存，保留 30 天、最多 2000 条。容器日志不包含查询原文、账号密码、主密码或密钥；查看运行状态可使用 `sh manage.sh logs --since 30m --tail 100 app`。管理页会话闲置 5 分钟、绝对期限 1 小时；锁定不停止微信查询。

公开源码仅包含通用程序、测试、部署工具与模板。从 `ecs/` 运行 `python -B ops/release/package.py --check` 检查整个仓库的跟踪清单及必需入口；运行不带 `--check` 的命令生成包含 `admin/`、`ecs/` 和根 README 的源码包，默认输出到 `ecs/releases/`。整理后的包名带 `organized-source`，不覆盖旧布局发布包。本地旧基线可能包含旧部署信息，应从经过检查的源码树创建干净发布历史，不推送所有本地分支、标签或镜像仓库。历史 [1.0 版本说明](history/RELEASE-1.0.md) 只记录 1.0 行为，不作为当前部署操作指南。

旧配置迁移及完整字段列表见[项目 README](../../README.md)。配置变更后使用 `sh manage.sh up -d --build` 重新创建服务；仅编辑文件不会自动改变已运行的容器。
