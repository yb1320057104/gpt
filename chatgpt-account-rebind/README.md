# ChatGPT Account Rebind Console

独立的本地可视化账号池/邮箱池管理项目。当前阶段先完成数据模型、导入解析、任务状态机、日志和 UI；登录与换绑适配器通过接口层接入，避免把凭据写入日志或前端。

## 启动

```powershell
cd D:\baiduProject\plus注册机\codex-auto-register\chatgpt-account-rebind
..\register_env\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 3220
```

打开：<http://127.0.0.1:3220>

## 导入格式

账号池支持：

```text
email----totp
email----password----totp
email----password
email----email_access_url
email----password----email_access_url
```

## Adapter readiness

The UI and queue are usable without any remote mutation contract. Login currently
executes the observed password+TOTP HTTP flow. Other credential branches and email
rebind stay fail-closed until their exact captured request is configured. Copy
`.env.example` to `.env` (or set process environment variables) and provide both
`CHATGPT_EMAIL_CHANGE_ENDPOINT` and `CHATGPT_EMAIL_CODE_VERIFY_ENDPOINT` only after
verifying the request shape. Endpoints are restricted to HTTPS `chatgpt.com` or
`auth.openai.com`; guessed URLs are rejected. `GET /api/config` reports readiness
flags without returning secrets.

邮箱池支持：

```text
email----email_access_url
email----password----email_access_url
```

密码/TOTP/取码地址按 URL、Base32 TOTP 特征和字段位置自动识别。所有原文只存本地 SQLite，日志仅记录掩码邮箱和状态码。
