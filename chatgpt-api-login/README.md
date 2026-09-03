# ChatGPT Pure HTTP Login (experimental)

这是一个不启动浏览器的最小 HTTP 客户端：使用 `httpx` 保持 Cookie/CSRF/OAuth 会话，按项目实测链路调用登录接口，并输出控制台与文件日志。

## 启用

```powershell
cd D:\baiduProject\plus注册机\codex-auto-register\chatgpt-api-login
..\register_env\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env 后运行
..\register_env\Scripts\python.exe main.py login
```

日志：`logs/chatgpt-api.log`。

## 登录链路

- `GET chatgpt.com/auth/login`
- `GET chatgpt.com/api/auth/csrf`
- `POST chatgpt.com/api/auth/signin/openai`
- `GET auth.openai.com/api/accounts/authorize`
- `POST auth.openai.com/api/accounts/password/verify`
- `POST auth.openai.com/api/accounts/mfa/issue_challenge`
- `POST auth.openai.com/api/accounts/mfa/verify`
- `GET chatgpt.com/api/auth/callback/openai`
- `GET chatgpt.com/backend-api/me`

## 重要限制

纯 HTTP 无法稳定生成网页端的 `openai-sentinel-token`、Cloudflare 设备证明和全部前端上下文。项目支持通过 `CHATGPT_SENTINEL_TOKEN` 注入已确认的令牌；为空时会在日志中明确提示，而不是伪造成功。

## 邮箱修改

OpenAI 官方说明邮箱修改需要在 ChatGPT 设置中完成，并且会发送验证邮件、完成后强制退出登录。当前公开抓包已确认登录接口，但尚未确认邮箱修改的真实请求路径，因此项目**禁止猜测或探测破坏性 endpoint**：

```powershell
..\register_env\Scripts\python.exe main.py change-email new@example.com --endpoint https://auth.openai.com/<已确认路径>
```

只有传入已确认的 `chatgpt.com` 或 `auth.openai.com` HTTPS endpoint 才会发送请求。未配置 endpoint 时返回 `email_change_endpoint_not_confirmed`。

修改成功后应清理旧会话并使用新邮箱重新执行 `login`；新邮箱必须完成验证且不能已绑定其他 OpenAI 账号。
