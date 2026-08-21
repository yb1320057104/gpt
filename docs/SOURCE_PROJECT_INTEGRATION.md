# 提链源项目集成清单

只读参考项目：`D:\baiduProject\upi提链\提链`

目标项目：`D:\baiduProject\plus注册机\codex-auto-register`

## 集成原则

- 源目录只读；不从源项目复制 `data/`、日志、缓存、截图、数据库、Token、Cookie、账号或硬编码管理密码。
- 协议重叠部分以源项目的五通道语义为准，并复用目标项目已经增强的 HTTP 跟踪、代理池、重试、OAICS/CS 分支和任务日志。
- 目标项目现有 `/api/payment-extractor/*`、`/api/tasks`、账号池、代理池、自动流水线及页面调用保持兼容。
- 因目标项目已有 `/api/tasks` 契约，源项目的“一批账号对应一个聚合任务”契约放在 `/api/source-console/tasks`，内部仍拆分成目标任务并聚合；避免破坏已有调用方。

## 逐项映射

| 源项目能力 | 源实现 | 目标实现/适配 |
| --- | --- | --- |
| PayPal | `src/extractor.js`、`engines/sms_tool` | `oai_payment_extractor` + `paypal_agreement_protocol`；保留 BA 链接校验、审批、轮询和解析 |
| GoPay | `gopay_engine` | `providers/gopay.py`、OAICS/CS 流程；保留国家 ID、币种 IDR、指定支付方式与 0 元约束 |
| GCash | `gcash_engine` | `providers/gcash.py`、OAICS/CS 流程；保留国家 PH、币种 PHP、指定支付方式与 0 元约束 |
| 直卡 | `card_engine` | 新增一等 `card` Provider；Stripe 初始化、Checkout Update、金额复核后仅返回 0 元托管 Checkout 链接 |
| PIX | `src/extractor.js` / 钱包协议 | 目标已有 `PIX` Provider 与 BR/BRL 校验，接入源批量任务入口 |
| 双代理池 | `src/proxy.js`、`src/engine.js` | `checkoutProxy` / `updateProxy`，支持整池轮询、自动换代理重试、同会话兼容策略 |
| AT 解析 | `src/atutil.js` | `/api/at/parse`；支持 JWT、session JSON，响应只返回脱敏预览 |
| 多账号任务 | `src/engine.js` | `/api/source-console/tasks` 聚合层；每个 AT 为独立目标任务，聚合进度、结果、失败原因和取消 |
| 成功扣额 | `src/cards.js`、`src/users.js` | Mongo 原子更新；仅“指定支付方式 + 最终金额 0 + 有结果链接”成功项扣额，失败/取消不扣 |
| 日志与统计 | `src/logs.js`、`src/store.js` | 复用目标项目现有任务日志、详细请求/响应、成功率和国家统计 |
| 控制台交互 | `public/index.html` / `public/js/app.js` | 逻辑并入目标 Vue `PaymentToolsView.vue`：五通道、国家、双代理池、AT 批量提交、进度/结果 |
| 配置 | `src/config.js` | `app/.env.example` 中仅增加无值模板；国家/币种和模式元数据由后端提供 |

## 数据集合

源项目的卡密、用户额度和独立后台没有迁移。提链任务直接使用目标项目现有的任务管理器、账号池、代理池和日志集合；AT 仍只进入目标项目任务流程，不新增卡密扣费逻辑。

## 源目录完整性基线

对生产相关的 `server.js`、`README.md`、`package.json`、`src/`、`public/`、`card_engine/`、`gopay_engine/`、`gcash_engine/`、`engines/` 做排序后的“相对路径 + 文件长度 + SHA-256”聚合：

- 文件数：171
- 总字节数：5,793,281
- 集成前聚合 SHA-256：`72CE393F5143A5F61221273268638962078068B4D0E6DE2AD452268B3B8DE206`

完成后必须用相同算法复核，聚合值一致才可确认源生产文件零改动。
