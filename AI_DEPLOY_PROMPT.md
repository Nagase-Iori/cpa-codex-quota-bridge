# 给 AI 的一键部署提示词

复制下面整段给有服务器操作能力的 AI。请先把尖括号中的信息替换成你的实际值，
不要把 OAuth Token 或原始 API Key 粘贴到对话中。

```text
我需要在一台 Linux 服务器上部署 GitHub 项目 cpa-codex-quota-bridge，目标是：

1. 读取服务器上现有 CLIProxyAPI（CPA）保存的 Codex OAuth 凭据；
2. 通过本机 HTTP 代理请求官方
   https://chatgpt.com/backend-api/wham/usage；
3. 将只含额度窗口的快照写入 CPA Credit Manager 的
   auth_quota_snapshots；
4. 提供只读 HTTPS 接口给 CC Switch（CCS）自定义用量查询；
5. 支持按设备/按 CCS provider 使用不同的访问 Key，并映射到一个或多个 CPA
   auth_id。

服务器信息：
- SSH 地址：<服务器地址>
- SSH 用户：<SSH用户>
- CPA 路径：<例如 /opt/cliproxyapi>
- Credit Manager 数据库：<绝对路径>
- CPA Codex auth 文件：<绝对路径；只在服务器读取，绝对不要上传>
- auth_id：<数据库里对应的 auth_id>
- 本机 HTTP 代理：<例如 http://127.0.0.1:2016，直连则填 direct>
- 用于额度查询的 HTTPS 域名：<例如 quota.example.com>

严格安全要求：
- 不要把 OAuth access_token、refresh_token、账号邮箱、服务器 IP、真实域名、
  任何原始 API Key、CPA 管理密钥、数据库、日志、订阅链接或个人路径写入 Git、
  README、命令输出或公开网页。
- 不要上传 auth 目录、*.db、*.db-wal、*.db-shm、日志或 CPA 官方二进制/第三方
  插件。仓库只使用项目自带的桥接代码、同步代码和 *.example 配置模板。
- 原始额度查询 Key 只在服务器交互式输入；配置文件仅保存 SHA-256 哈希。
- 操作前备份 Credit Manager 数据库，并显示备份的绝对路径；不要覆盖原数据库。
- 先阅读项目 README、PLUGIN_INTEGRATION.md 和现有 CPA 版本的表结构，不要假定
  升级后的数据库结构不变。

请按以下顺序工作：

1. 检查 Python 3.10+、systemd、sqlite3、CPA、Credit Manager、本机代理和 auth
   文件权限。确认 auth 文件包含 access_token/account_id，但绝不打印字段值。
2. 用最小权限安装到 /opt/cpa-quota-bridge 和 /etc/cpa-quota-bridge；从模板创建
   config.json 与 sync.json，并替换为上面给出的服务器本地路径。
3. 用 getpass 交互式读取每个额度查询 Key，生成 SHA-256 哈希，写入
   config.json 的 key_mappings；不要保存原始 Key，不要把它放入 shell 历史。
4. 先手动运行同步器，检查官方接口响应只包含额度窗口，确认数据库中对应行更新。
   不要打印 OAuth 请求头或完整响应中的凭据字段。
5. 备份确认后启用 cpa-quota-bridge.service 和
   credit-manager-quota-sync.timer；timer 每 5 分钟刷新一次，并验证日志不含密钥。
6. 配置 Caddy：只把 /ccs/quota 反代到 127.0.0.1:8765；如需 CPA API，单独反代
   /v1 到 127.0.0.1:8317；不要暴露 CPA 管理端口。
7. 用 HTTPS 域名和一个额度查询 Key 测试 /health 与 /ccs/quota，验证返回
   windows[*].label、used_percent、remaining_percent、resets_at。
8. 给出 CCS 配置：GET <HTTPS域名>/ccs/quota，Authorization: Bearer <设备专用Key>，
   并将上述四个字段映射到 CCS 的用量显示。说明 5 小时/7 天是官方 rolling
   rate-limit 窗口，CCS pricing 文件负责价格估算，两者不是同一项数据。
9. 最后列出已安装服务、timer 下次运行时间、测试结果和备份路径。若网络、TLS、
   auth 或数据库结构失败，停止并报告原因，不要删除原配置或数据库。
```

AI 执行完后，仍应人工检查公开仓库和服务器配置中没有真实凭据。
