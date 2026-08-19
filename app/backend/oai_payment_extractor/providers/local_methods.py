from .base import ProviderAdapter


# Consolidated from the independent extractors under ``upi提链``. Runtime
# state, proxies and credentials remain owned by the common task engine.
IDEAL = ProviderAdapter("ideal", "ideal_url", ("stripe.com", "pay.openai.com"))
UPI = ProviderAdapter("upi", "upi_url", ("stripe.com", "pay.openai.com"))
PIX = ProviderAdapter("pix", "pix_url", ("stripe.com", "pay.openai.com"))
BLIK = ProviderAdapter("blik", "blik_url", ("stripe.com", "pay.openai.com"))
TWINT = ProviderAdapter("twint", "twint_url", ("twint.ch", "stripe.com"))
KAKAO_PAY = ProviderAdapter(
    "kakao_pay", "kakao_pay_url", ("kakaopay.com", "kakao.com", "stripe.com")
)
MOMO = ProviderAdapter("momo", "momo_url", ("momo.vn", "stripe.com"))
