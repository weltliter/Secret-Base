# 项目结构与职责

根目录保留启动入口、容器配置、依赖和 README。应用按业务、检索、微信通道和管理端划分，运维与测试单独组织。

## 本地工作区

```text
secret/
├── 打开管理页.cmd                # Windows 双击连接并打开浏览器
├── console.ps1                  # 本机打开、连接、检查、状态入口
├── client/                      # Windows SSH 隧道工具和配置
├── vault/
│   ├── core/                    # 加密、存储、业务、查重、查询自动恢复
│   ├── search/                  # 混合检索、内存向量索引、诊断日志
│   ├── channels/                # 微信协议客户端、只读查询会话
│   └── web/
│       ├── app.py               # 应用装配、生命周期、静态页面
│       ├── schemas.py           # 请求输入结构
│       ├── security.py          # 来源、CSRF、管理授权与错误处理
│       ├── sessions.py          # 浏览器管理会话
│       ├── routes/              # session、records、settings API
│       ├── templates/console.html
│       └── static/
│           ├── css/console.css
│           └── js/
│               ├── main.js      # 启动、导航、共享状态、锁定清理
│               ├── shared/      # ui、transport
│               ├── auth/        # session 登录与初始化
│               ├── records/     # index 协调、list、editor、duplicates
│               │                # filter-controls、filters、tags
│               ├── settings/    # 微信绑定与模型设置
│               └── logs/        # 查询日志展示
├── ops/
│   ├── backup/                  # backup.sh、restore.sh
│   ├── security/                # init-query-key.sh
│   ├── models/                  # fetch-embedding.py
│   ├── postgres/                # 初始化 SQL、Unix socket 访问配置
│   └── checks/                  # 数据库联调、嵌入模型资源检查
├── tests/
│   ├── conftest.py              # 共用测试配置
│   ├── support/                 # 共用模拟存储、样例和响应工具
│   ├── core/                    # 加密、业务、综合查重
│   ├── web/                     # 管理 API、会话权限
│   ├── channels/                # 微信协议、只读通道
│   ├── search/                  # 检索、向量、日志
│   ├── frontend/                # filters.test.mjs
│   └── client/                  # tunnel.ps1
├── docs/
│   ├── ARCHITECTURE.md
│   ├── OPERATIONS.md
│   ├── RELEASE-1.0.md            # 1.0 范围、验证和交付边界
│   ├── ITERATION-REQUIREMENTS.md  # 待开发需求，不代表现有行为
│   └── security/                # 数据安全边界、安全组配置
├── VERSION                      # 源码交付版本：1.0
├── releases/1.0/                # 源码交付包及 SHA-256，不进入镜像
├── Dockerfile / compose.yaml
├── requirements.txt / requirements-dev.txt
├── .local/                      # 仅本机：keys、ssh、临时 checks、整理快照
└── .venv/                       # 本机维护环境
```

前端使用原生 ES modules，不需要 npm 安装或打包服务。记录列表、编辑、筛选和查重分别处理，通过 `records/index.js` 连接；公共 API 请求和 DOM 工具集中在 `shared/`。密码和业务内容不写入浏览器本地存储。

测试按应用职责分组，共用支持代码只放在 `tests/support/`。单元测试、维护环境和 SSH 私钥不发布到服务器或应用镜像。

## 云端工作区

服务器项目根目录为 `/opt/wechat-vault`。部署内容是 `vault/`、`ops/`、`docs/`、容器配置、运行依赖、README 和 VERSION；源码交付包另含本机入口与测试，不能整包覆盖服务器。运行文件单独存放：

| 路径 | 内容与保留方式 |
| --- | --- |
| `data/postgres/` | 数据库持久化目录，不随代码更新清理 |
| `data/vault/vault.json` | 密钥信封，必须与数据库配套备份 |
| `data/vault/query-unlock.json` | 查询自动启动信封；恢复备份后重新登录生成 |
| `data/pgsocket/` | 数据库维护的共享 Unix socket |
| `secrets/query-unlock.key` | 机器密钥，只读挂载，不进入数据库备份 |
| `models/bge-small-zh-v1.5/` | 运行所需公开嵌入模型，只读挂载 |
| `backups/` | 加密备份，执行备份脚本时清理超过 7 天的文件 |
| `.env` | 服务器镜像源配置，不随代码发布覆盖 |
| `.maintenance/checks/` | 隔离联调的临时文件，检查结束后清理 |

## 调用与权限

管理 API 调用 `core` 完成记录变动；微信控制器通过 `QueryVault` 只读接口查询和查重。检索模块用描述字段构建内存向量，结合关键词候选交给 DeepSeek Flash 排序；最终凭据由业务代码读取。数据库通过共享 Unix socket 连接。

管理会话与微信查询权限的生命周期分开管理，但使用同一份业务数据密钥：管理会话从登录起固定有效 8 小时，锁定仅撤销当前浏览器，不清除供微信查询使用的数据密钥；微信自动恢复不会创建管理会话。AI 固定使用 `deepseek-flash` 并关闭思考模式。

本机默认端口为 `127.0.0.1:18080`，允许选择 10000–65535。`Connect` 遇端口占用退出；`Open` 会检查该入口是否响应密码库状态，正常则复用并打开浏览器，否则报错，不停止已有进程。云端仅发布 `127.0.0.1:18080` 到容器 8000，数据库不监听 TCP；管理连接经过 SSH 默认 22 端口，可与日常 SSH 连接并存。

具体命令见 [运维说明](OPERATIONS.md)，数据边界见 [安全说明](security/SECURITY.md)。
