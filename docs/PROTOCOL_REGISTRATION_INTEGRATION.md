# GPT-Register-Tool 协议注册集成

目标项目原有的浏览器注册、MailCom Hub 和支付流程保持不变。GPT-Register-Tool 的纯协议注册代码被隔离复制到 `protocol-registration/`，由后端显式接口启动，不会自动替换现有注册驱动。

后端接口（受现有本地工作台密码保护）：

- `POST /api/protocol-registration/start`
- `GET /api/protocol-registration/{job_id}`

请求示例：

```json
{
  "count": 1,
  "workers": 1,
  "mailboxFile": "D:\\...\\mailcom-mailbox-pool.txt",
  "proxyPool": [
    "http://127.0.0.1:7897",
    "http://127.0.0.1:7898"
  ],
  "proxyGroup": "default",
  "country": "JP"
}
```

未传 `mailboxFile` 时，后端会从当前邮箱池导出可用邮箱；未传 `proxyPool`/`proxy` 时，会从当前代理池按国家和分组筛选，并通过协议侧的 `--proxy-pool` 进行轮换、重试。注册成功的 session 会按文件路径和邮箱去重后导入账号池。

MailCom Hub 仍由 `mailcom-manager/` 负责账号登录、DPAPI 加密、Session 恢复、别名同步和接码 URL；协议 sidecar 只消费邮箱池，不重复实现邮箱管理。
