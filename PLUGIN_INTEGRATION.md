# 与 CPA Credit Manager 的关系

本项目不是 CLIProxyAPI 的官方二进制，也不重新分发第三方 Credit Manager
插件。它假定服务器上已经安装并启用了 CPA 的 Credit Manager，然后提供两层
集成：

1. `credit_manager_quota_sync.py` 每隔一段时间请求官方 Codex 用量接口，并把
   只含额度窗口的净化快照写回 Credit Manager 的现有
   `auth_quota_snapshots` 表。
2. `cpa_quota_bridge.py` 提供只读 HTTP 接口，优先实时请求官方额度，失败时回退
   到上面的净化快照，供 CCS 的自定义用量查询使用。

这样不会修改或上传 CPA 的 OAuth 文件、数据库、管理密钥或官方插件文件。
Credit Manager 的数据库结构属于 CPA 版本实现细节；升级 CPA 后应先备份并确认
表结构仍为当前版本。

## 兼容性检查

```bash
test -f /opt/cliproxyapi/data/credit-manager/credit-manager.db
sqlite3 /opt/cliproxyapi/data/credit-manager/credit-manager.db \
  'PRAGMA table_info(auth_quota_snapshots);'
```

如果表结构不同，不要直接运行同步器；请先根据当前 CPA 版本调整
`update_database()`，并在备份数据库副本上测试。
