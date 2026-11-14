# 微信个人密码库

阶段版本 **1.1-alpha.3**。账号资料加密保存在自己的服务器，由 Web 管理台维护，通过绑定的微信只读查询。

数据库包含完整的加密信封和业务数据，可用单文件备份迁移，并使用原主密码或恢复密钥解锁。

部署与验证见[运维说明](docs/OPERATIONS.md)和[架构说明](docs/ARCHITECTURE.md)。

Windows 管理入口为 `打开管理页.cmd`。先用 `.\console.ps1 -Action Configure` 配置自己的服务器和私钥路径。首次连接核对服务器的 SSH 主机指纹。
