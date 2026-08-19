# 提链方式集成说明

本项目已对 `D:\baiduProject\upi提链` 中的源码按哈希去重，并将独立实现归入现有
OAICS / CS Checkout 统一任务引擎。外部目录中的 `.env`、Token、代理、数据库、日志
和运行结果均未复制。

| 类型 | Stripe 方法名 | 推荐国家/币种 | 结果字段 |
| --- | --- | --- | --- |
| PayPal | `paypal` | 自定义 | `paypal_url` |
| GoPay | `gopay` | ID / IDR | `gopay_url` |
| GCash | `gcash` | PH / PHP | `gcash_url` |
| iDEAL | `ideal` | NL / EUR | `ideal_url` |
| UPI | `upi` | IN / INR | `upi_url` |
| PIX | `pix` | BR / BRL | `pix_url` |
| BLIK | `blik` | PL / PLN | `blik_url` |
| TWINT | `twint` | CH / CHF | `twint_url` |
| KakaoPay | `kakao_pay` | KR / KRW | `kakao_pay_url` |
| MoMo | `momo` | VN / VND | `momo_url` |

统一引擎负责 Token、Checkout 创建/更新、代理轮换、支付方式可用性校验、Stripe
confirm、跳转轮询、任务取消和脱敏日志。只有上游 Checkout 实际返回对应
`payment_method_types` 时才继续执行，否则输出明确的不支持日志。

PayPal Billing Agreement 属于提链后的协议授权；菲律宾短链和卡直绑属于支付执行；
Hosted Checkout 属于页面形态。它们继续使用项目现有的协议工具或 Checkout 流程，
不作为新的支付方式。外部 Web UI、SQLite 队列、Go/Rust 包装器与现有任务队列重复，
因此只吸收协议参数和结果识别规则，不引入第二套服务。
