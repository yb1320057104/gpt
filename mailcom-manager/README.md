# MailCom Hub

独立的本机 mail.com 邮箱管理网站，不读取或修改主程序 `app/` 的代码、配置或 MongoDB。

## 功能

- 批量导入 `邮箱----密码`
- Windows DPAPI 当前用户加密存储密码
- 批量测试 IMAP 登录
- 只读查看 INBOX、Spam 和 Junk
- 提取邮件中的六位验证码
- 支持自定义 IMAP 地址、端口及 HTTP/HTTPS/SOCKS4/SOCKS5 代理
- 未显式配置代理时，自动使用本机 `127.0.0.1:7897` SOCKS/Mixed 代理（监听存在时）
- 搜索、复制邮箱和删除本地记录
- REST API 与 Swagger 文档
- 通过 SSH 将主邮箱、最新密码和别名全量推送到独立服务器

## 启动

双击根目录中的 `启动MailCom管理器.cmd`，浏览器会打开：

```text
http://127.0.0.1:3211
```

停止服务时双击 `停止MailCom管理器.cmd`。

## API

- `GET /api/health`
- `GET /api/stats`
- `GET /api/accounts`
- `POST /api/accounts/import`
- `POST /api/accounts/{id}/test`
- `POST /api/accounts/test-all`
- `GET /api/accounts/{id}/messages`
- `GET /api/accounts/{id}/latest-code`
- `GET /api/mail/latest?email=邮箱地址`
- `GET /code/{access_key}`：不可猜测的稳定接码地址，兼容长轮询 `?wait=60`
- `GET /api/export/registration-lines`
- `GET /api/export/registration-items`：供本机注册机同步分裂邮箱及其主邮箱归属。
- `GET /api/accounts/{id}/aliases`
- `POST /api/accounts/{id}/aliases/import`
- `DELETE /api/aliases/{id}`
- `DELETE /api/accounts/{id}`

API 文档：`http://127.0.0.1:3211/docs`

## 推送服务器

首页点击“推送服务器”，填写服务器主机、SSH 端口、用户名和密码。每次推送都是全量幂等快照：服务器上的主邮箱、密码和别名会与本机当前状态保持一致。

- SSH 密码只存在于本次浏览器请求和后端内存中，不写入配置、数据库或日志。
- 邮箱密码从本机 DPAPI 解密后经 SSH 加密通道传输，并在服务器端使用独立 Fernet 密钥重新加密。
- 服务器必须预先部署 `/opt/mailcom-manager-public` 接收端。
- 推送接口仅绑定在本机 MailCom Manager；公网 Nginx 不开放同步、账号列表、导入或删除接口。

注册机导入格式：

```text
邮箱----http://127.0.0.1:3211/code/不可猜测的访问密钥
```

网页中的“复制注册机格式”会一次复制全部邮箱。

## 分裂邮箱 / 别名管理

先在 mail.com 中创建并确认别名能够投递到对应主邮箱，再在网页中点击该主邮箱的“分裂管理”导入别名。系统不会为别名保存第二份密码，而是复用主邮箱的 DPAPI 加密凭据。

每个主邮箱和别名都有独立注册机地址：

```text
ALIAS_EMAIL----http://127.0.0.1:3211/code/不可猜测的访问密钥
```

别名读取会对 `To`、`Delivered-To`、`X-Original-To` 和 `Envelope-To` 收件人字段进行精确匹配，只返回投递给该别名的邮件，避免多个别名之间串验证码。“复制注册机格式”会同时导出主邮箱和所有别名。

## 数据

- SQLite：`data/mailcom.db`
- 日志：`data/server.stdout.log`、`data/server.stderr.log`
- 密码：DPAPI 加密 BLOB，仅当前 Windows 用户可解密

## 自定义 IMAP 与代理

启动时可以显式指定 IMAP 地址、端口和代理 URL：

```powershell
.\mailcom-manager\start.ps1 `
  -ImapHost "imap.mail.com" `
  -ImapPort 993 `
  -ImapProxy "http://用户名:密码@代理IP:端口"
```

也可以从仓库根目录传入同样的参数：

```powershell
.\启动MailCom管理器.cmd -ImapProxy "http://用户名:密码@代理IP:端口"
```

`-ImapProxy` 支持以下格式，用户名和密码可省略；其中包含 `@`、`:` 等特殊字符时需使用 URL 编码：

```text
http://HOST:PORT
http://USERNAME:PASSWORD@HOST:PORT
https://USERNAME:PASSWORD@HOST:PORT
socks4://HOST:PORT
socks4a://HOST:PORT
socks5://HOST:PORT
socks5h://USERNAME:PASSWORD@HOST:PORT
```

也可以使用环境变量 `MAILCOM_IMAP_HOST`、`MAILCOM_IMAP_PORT` 和 `MAILCOM_IMAP_PROXY`。显式参数或环境变量优先；没有配置代理时直接连接，但如果启动时检测到本机 `127.0.0.1:7897` 正在监听，则仍自动使用该 SOCKS5 代理。

要明确禁用代理并跳过 `7897` 自动检测，可使用：

```powershell
.\mailcom-manager\start.ps1 -DirectImap
```

更改这些参数前需要先停止正在运行的 MailCom Manager。
