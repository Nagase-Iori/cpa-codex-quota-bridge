# CPA Codex Quota Bridge

把 CLIProxyAPI（CPA）里已保存的 Codex/ChatGPT 账号额度，以安全的只读接口
提供给 CC Switch（CCS）自定义用量查询，并可定时同步到 CPA Credit Manager。

它解决的是“请求走 CPA，但 CCS 想显示官方账号的 5 小时/7 天额度”这一场景：

```text
CCS custom usage query
          │ HTTPS + Bearer key
          ▼
     Caddy /ccs/quota
          ▼
  cpa_quota_bridge.py ── live ──► chatgpt.com/backend-api/wham/usage
          │                         (可经本机 Mihomo/Clash)
          └── fallback ──► CPA Credit Manager sanitized snapshot

credit-manager-quota-sync.timer ──► 定时刷新 Credit Manager 快照
```

## 重要边界

- 本项目只包含自行编写的桥接和同步代码，不包含 CPA 官方程序、第三方插件、
  OAuth 凭据或数据库。
- 真实域名、服务器 IP、API Key、CPA 管理密钥、`access_token`、`refresh_token`、
  订阅链接和日志都必须留在服务器上，不能提交到 Git。
- 桥接接口只返回额度窗口，不返回 OAuth Token，也不会记录 `Authorization` 请求头。
- 这里的 API Key 是“访问额度接口的密钥”，不是 ChatGPT OAuth Token。建议为每台
  设备/每个 CCS provider 使用不同的随机 Key，并只保存 SHA-256 哈希。
- 额度属于对应的官方账号，不是按请求逐次计费的 token 账单；5 小时和 7 天是官方
  rolling rate-limit 窗口。CCS 的价格计算仍由 CCS 的 pricing 文件/规则负责。

## 前置条件

服务器需要：

1. Linux、Python 3.10+、systemd 和 SQLite3。
2. 已正常安装并运行 CLIProxyAPI，且 Credit Manager 已启用。
3. CPA 的 Codex OAuth 文件位于服务器本地，并至少包含 `access_token` 和
   `account_id`。不要把这个文件复制到仓库或发给任何人。
4. 如果服务器直连官方接口不稳定，准备一个本机 HTTP 代理，例如
   `http://127.0.0.1:2016`。没有代理时把 `proxy_url` 改成 `direct`。
5. 一个已解析到服务器的 HTTPS 域名，并让 Caddy 管理证书。不要把 CPA 管理端口
   `8317` 或桥接端口 `8765` 直接暴露到公网。

## 安装

以下命令在服务器上执行。把本仓库中的文件上传到临时目录后再安装；示例中的
`ACCOUNT_AUTH_FILE.json`、`quota.example.com` 和 `CHANGE_ME` 都必须替换成你自己的
值，不能照抄。

```bash
sudo install -d -m 0750 -o cliproxy -g cliproxy /opt/cpa-quota-bridge
sudo install -d -m 0750 -o root -g cliproxy /etc/cpa-quota-bridge

sudo install -o root -g root -m 0755 cpa_quota_bridge.py \
  /opt/cpa-quota-bridge/cpa_quota_bridge.py
sudo install -o root -g root -m 0755 credit_manager_quota_sync.py \
  /opt/cpa-quota-bridge/credit_manager_quota_sync.py

sudo install -o root -g cliproxy -m 0640 config.json.example \
  /etc/cpa-quota-bridge/config.json
sudo install -o root -g cliproxy -m 0640 sync.json.example \
  /etc/cpa-quota-bridge/sync.json
sudo install -o root -g root -m 0644 cpa-quota-bridge.service \
  /etc/systemd/system/cpa-quota-bridge.service
sudo install -o root -g root -m 0644 credit-manager-quota-sync.service \
  /etc/systemd/system/credit-manager-quota-sync.service
sudo install -o root -g root -m 0644 credit-manager-quota-sync.timer \
  /etc/systemd/system/credit-manager-quota-sync.timer
```

编辑两个 JSON 文件：

- `auth_file`：CPA 服务器上真实 OAuth 文件的绝对路径；
- `auth_id`：Credit Manager 表中与该文件对应的 `auth_id`；
- `database`：当前 CPA Credit Manager 数据库路径；
- `proxy_url`：本机 Mihomo/Clash HTTP 代理地址，或填写 `direct`；
- `default_auth_id`：单账号时可填写同一个 `auth_id`；多账号时留空并使用
  `key_mappings`。

### 为每台设备设置不同的访问 Key

不要在配置中写原始 Key。先在服务器上交互式生成哈希：

```bash
python3 - <<'PY'
import getpass, hashlib
key = getpass.getpass('quota API key: ').encode()
print(hashlib.sha256(key).hexdigest())
PY
```

把输出放入 `/etc/cpa-quota-bridge/config.json`。单账号示例：

```json
{
  "default_auth_id": "ACCOUNT_AUTH_FILE.json",
  "allowed_key_hashes": ["这里填上面生成的64位哈希"],
  "key_mappings": {}
}
```

多账号/多设备示例：

```json
{
  "default_auth_id": "",
  "allowed_key_hashes": [],
  "key_mappings": {
    "设备A的Key哈希": "ACCOUNT_A.json",
    "设备B的Key哈希": "ACCOUNT_B.json"
  }
}
```

`key_mappings` 的右侧必须与数据库中对应的 `auth_id` 完全一致。不同设备使用
不同 Key 后，CCS 可以分别保存查询配置；额度结果会分别映射到对应官方账号。

## 启用服务

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cpa-quota-bridge.service
sudo systemctl enable --now credit-manager-quota-sync.timer

sudo systemctl start credit-manager-quota-sync.service
curl -fsS http://127.0.0.1:8765/health
```

第一次同步前建议备份数据库：

```bash
sudo cp -a /opt/cliproxyapi/data/credit-manager/credit-manager.db \
  "/opt/cliproxyapi/data/credit-manager/credit-manager.db.bak.$(date +%Y%m%d-%H%M%S)"
```

查看状态：

```bash
systemctl status cpa-quota-bridge.service
systemctl status credit-manager-quota-sync.timer
journalctl -u credit-manager-quota-sync.service -n 50 --no-pager
```

## Caddy HTTPS

复制 `Caddyfile.example` 中的相关站点块到 Caddy 配置，并把示例域名改成你
自己的域名：

```text
quota.example.com {
    @quota path /ccs/quota
    handle @quota {
        reverse_proxy 127.0.0.1:8765
    }
    handle { respond "Not Found" 404 }
}
```

然后检查并重载：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

域名必须解析到服务器，且防火墙放行 TCP 443。桥接服务只监听
`127.0.0.1:8765`，不应直接从公网访问。

## CCS 用量查询配置

在 CCS 对应 provider 的“用量查询/自定义用量”中填写：

- 请求地址：`https://quota.example.com/ccs/quota`
- 方法：`GET`
- 请求头：`Authorization: Bearer <该设备专用的额度查询 Key>`
- 不要把 OAuth `access_token` 填入 CCS。

返回字段如下：

```text
windows[*].label
windows[*].used_percent
windows[*].remaining_percent
windows[*].resets_at
```

把 `label` 映射为名称，把 `used_percent` 映射为已使用百分比，把
`remaining_percent` 映射为剩余百分比，把 `resets_at` 映射为重置时间。
不同 CCS 版本的“自定义查询”界面字段名可能不同；仓库里的
`examples/quota-response.json` 可用来对照 JSONPath。若 CCS 版本只接受脚本，
让脚本请求该 URL 后读取 `windows` 数组，不要在脚本中硬编码真实 Key。

本地测试（只在服务器执行）：

```bash
curl -fsS -H 'Authorization: Bearer YOUR_DEVICE_QUOTA_KEY' \
  http://127.0.0.1:8765/ccs/quota
```

公网测试使用 HTTPS 域名。若响应中 `stale` 为 `true`，表示实时官方请求暂时失败，
返回的是 Credit Manager 最近一次成功同步的净化快照。

## 故障排查

### `401 unauthorized`

请求头格式必须是 `Authorization: Bearer ...`；Key 的 SHA-256 哈希必须存在于
`allowed_key_hashes` 或 `key_mappings`。修改配置后重启桥接服务。

### `quota snapshot unavailable`

检查 `auth_id` 是否与数据库一致、Credit Manager 是否已创建对应快照，并手动运行
一次同步服务。

### `live quota request failed`

检查服务器到官方域名的 DNS/TLS/网络，以及 `proxy_url` 指向的本机代理是否在监听。
同步器和桥接器都使用同一代理设置。

### Credit Manager 不更新

```bash
systemctl list-timers credit-manager-quota-sync.timer
journalctl -u credit-manager-quota-sync.service --since '30 min ago' --no-pager
```

确认 timer 已启用、auth 文件仍有效，且 CPA 升级没有改变数据库表结构。每次升级
CPA 后先备份数据库，再在测试环境运行同步器。

## 隐私与发布检查

公开仓库中不应出现真实值。提交前在仓库目录运行：

```bash
rg -n -i '真实域名|服务器IP|sk-|tk-|access_token|refresh_token|订阅|个人路径|邮箱' .
```

其中代码里的字段名 `access_token` 是读取 CPA 本地凭据所必需的；真正不能出现的
是 Token 值、邮箱、IP、域名、数据库和日志内容。`.gitignore` 只是最后一道防线，
提交前仍应人工检查 `git diff --cached`。

## 免责声明

本项目只做额度读取和净化快照同步，不绕过认证、不破解服务限制，也不保证第三方
CPA/CCS 版本长期保持相同接口。请遵守所使用服务、CPA、CCS 及代理软件的许可和
服务条款。
