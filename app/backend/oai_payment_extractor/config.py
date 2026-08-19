from __future__ import annotations

from dataclasses import asdict

from .errors import ConfigurationError
from .models import BillingProfile


DEFAULT_TIMEOUT = 30
PROVIDER_POLL_TIMEOUT_SECONDS = 5
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
STRIPE_VERSION_BASE = "2025-03-31.basil"
STRIPE_RUNTIME_VERSION = "692f102a8f"
OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION = STRIPE_RUNTIME_VERSION

COUNTRY_PROFILES = {
    "GB": {"currency": "GBP", "locale": "en-GB", "timezone": "Europe/London"},
    "US": {"currency": "USD", "locale": "en-US", "timezone": "America/New_York"},
    "BR": {"currency": "USD", "locale": "pt-BR", "timezone": "America/Sao_Paulo"},
    "DE": {"currency": "EUR", "locale": "de-DE", "timezone": "Europe/Berlin"},
    "TH": {"currency": "USD", "locale": "th-TH", "timezone": "Asia/Bangkok"},
    "BA": {"currency": "USD", "locale": "bs-BA", "timezone": "Europe/Sarajevo"},
    "PH": {"currency": "PHP", "locale": "en-PH", "timezone": "Asia/Manila"},
    "ID": {"currency": "IDR", "locale": "id-ID", "timezone": "Asia/Jakarta"},
    "NL": {"currency": "EUR", "locale": "nl-NL", "timezone": "Europe/Amsterdam"},
    "AE": {"currency": "AED", "locale": "en-AE", "timezone": "Asia/Dubai"},
    "DK": {"currency": "DKK", "locale": "da-DK", "timezone": "Europe/Copenhagen"},
    "JP": {"currency": "JPY", "locale": "ja-JP", "timezone": "Asia/Tokyo"},
    "ES": {"currency": "EUR", "locale": "es-ES", "timezone": "Europe/Madrid"},
    "FI": {"currency": "EUR", "locale": "fi-FI", "timezone": "Europe/Helsinki"},
    "FR": {"currency": "EUR", "locale": "fr-FR", "timezone": "Europe/Paris"},
    "IN": {"currency": "INR", "locale": "en-IN", "timezone": "Asia/Kolkata"},
    "PL": {"currency": "PLN", "locale": "pl-PL", "timezone": "Europe/Warsaw"},
    "CH": {"currency": "CHF", "locale": "de-CH", "timezone": "Europe/Zurich"},
    "KR": {"currency": "KRW", "locale": "ko-KR", "timezone": "Asia/Seoul"},
    "VN": {"currency": "VND", "locale": "vi-VN", "timezone": "Asia/Ho_Chi_Minh"},
}

_BILLING_VALUES = {
    "GB": ("James Smith", "james.smith@example.com", "+442079250918", "10 Downing Street", "London", "Greater London", "SW1A 2AA"),
    "US": ("John Smith", "john.smith@example.com", "+12025550123", "1600 Pennsylvania Avenue NW", "Washington", "DC", "20500"),
    "BR": ("João da Silva", "joao.silva@example.com", "+551130001234", "Avenida Paulista, 1578", "São Paulo", "SP", "01310-200"),
    "DE": ("Max Mustermann", "max.mustermann@example.com", "+493012345678", "Unter den Linden 77", "Berlin", "Berlin", "10117"),
    "TH": ("Somchai Prasert", "somchai.prasert@example.com", "+6621234567", "123 ถนนสุขุมวิท", "Bangkok", "Bangkok", "10110"),
    "BA": ("Amar Hadžić", "amar.hadzic@example.com", "+38733123456", "Zmaja od Bosne 8", "Sarajevo", "Federation of Bosnia and Herzegovina", "71000"),
    "PH": ("Juan Dela Cruz", "juan.delacruz@example.com", "+63281234567", "678 Ayala Avenue", "Makati", "Metro Manila", "1226"),
    "ID": ("Budi Santoso", "budi.santoso@example.com", "+622112345678", "Jl. Jenderal Sudirman No. 45", "Jakarta", "DKI Jakarta", "10220"),
    "NL": ("Daan de Vries", "daan.devries@example.com", "+31201234567", "Dam 1", "Amsterdam", "Noord-Holland", "1012 JS"),
    "AE": ("Ahmed Al Mansoori", "ahmed.almansoori@example.com", "+97142123456", "Sheikh Zayed Road", "Dubai", "Dubai", "00000"),
    "DK": ("Lars Jensen", "lars.jensen@example.com", "+4532123456", "Rådhuspladsen 1", "Copenhagen", "Capital Region of Denmark", "1550"),
    "JP": ("Taro Yamada", "taro.yamada@example.com", "+81312345678", "1-1 Marunouchi", "Chiyoda City", "Tokyo", "100-0005"),
    "ES": ("Carlos García", "carlos.garcia@example.com", "+34911234567", "Calle de Alcalá, 1", "Madrid", "Madrid", "28014"),
    "FI": ("Matti Meikäläinen", "matti.meikalainen@example.com", "+35891234567", "Mannerheimintie 1", "Helsinki", "Uusimaa", "00100"),
    "FR": ("Jean Dupont", "jean.dupont@example.com", "+33142345678", "10 Rue de Rivoli", "Paris", "Île-de-France", "75001"),
    "IN": ("Aisha Sharma", "aisha.sharma@example.com", "+919810123456", "24 Park Street", "Kolkata", "West Bengal", "700016"),
    "PL": ("Jan Kowalski", "jan.kowalski@example.com", "+48221234567", "Marszalkowska 1", "Warsaw", "Masovian", "00-001"),
    "CH": ("Alex Meyer", "alex.meyer@example.com", "+41441234567", "Bahnhofstrasse 1", "Zurich", "Zurich", "8001"),
    "KR": ("Kim Minjun", "minjun.kim@example.com", "+82212345678", "Teheran-ro 1", "Seoul", "Gangnam-gu", "06130"),
    "VN": ("Nguyen Minh Anh", "minh.anh@example.com", "+842812345678", "1 Le Duan", "Ho Chi Minh City", "Ho Chi Minh", "700000"),
}

SUPPORTED_COUNTRIES = tuple(COUNTRY_PROFILES)


def country_config(country: str) -> tuple[str, str, str, str]:
    code = str(country or "").upper()
    profile = COUNTRY_PROFILES.get(code)
    if not profile:
        raise ConfigurationError(
            "country must be " + ", ".join(SUPPORTED_COUNTRIES)
        )
    return code, profile["currency"], profile["locale"], profile["timezone"]


def billing_for_country(country: str) -> BillingProfile:
    code, *_ = country_config(country)
    name, email, phone, line1, city, state, postal_code = _BILLING_VALUES[code]
    return BillingProfile(
        name=name,
        email=email,
        phone=phone,
        country=code,
        line1=line1,
        city=city,
        state=state,
        postal_code=postal_code,
    )


def billing_dict_for_country(country: str) -> dict[str, str]:
    return billing_for_country(country).to_dict()


def currency_minor_scale(currency: str) -> int:
    """Return the number of decimal places for display conversion."""
    return 0 if str(currency or "").upper() in {"JPY", "IDR", "KRW", "VND"} else 2


def payment_currency(country: str, payment_method: str) -> str:
    """Resolve currencies that differ by payment method within one country."""
    method = str(payment_method or "").strip().lower()
    if method == "pix" and str(country or "").upper() == "BR":
        return "BRL"
    return country_config(country)[1]


def normalize_payment_method(value: str) -> str:
    method = str(value or "paypal").strip().lower() or "paypal"
    supported = {
        "paypal", "gopay", "gcash", "ideal", "upi", "pix", "blik",
        "twint", "kakao_pay", "momo",
    }
    if method not in supported:
        raise ConfigurationError("payment_method must be one of " + ", ".join(sorted(supported)))
    return method


def processor_entity_for_country(country: str, existing: str = "") -> str:
    return str(existing or "").strip() or (
        "openai_llc" if country.upper() == "US" else "openai_ie"
    )
