from .base import ProviderAdapter


# Consolidated from the independent extractors under ``upi提链``. Runtime
# state, proxies and credentials remain owned by the common task engine.
CARD = ProviderAdapter("card", "card_url", ("chatgpt.com", "stripe.com", "pay.openai.com"))
IDEAL = ProviderAdapter("ideal", "ideal_url", ("stripe.com", "pay.openai.com"))
UPI = ProviderAdapter("upi", "upi_url", ("stripe.com", "pay.openai.com"))
PIX = ProviderAdapter("pix", "pix_url", ("stripe.com", "pay.openai.com"))
BLIK = ProviderAdapter("blik", "blik_url", ("stripe.com", "pay.openai.com"))
TWINT = ProviderAdapter("twint", "twint_url", ("twint.ch", "stripe.com"))
KAKAO_PAY = ProviderAdapter(
    "kakao_pay", "kakao_pay_url", ("nicepay.co.kr", "nicepay.com", "kakaopay.com", "kakao.com", "stripe.com")
)
MOMO = ProviderAdapter(
    "momo",
    "momo_url",
    ("payment.momo.vn", "momo.vn", "stripe.com"),
)
