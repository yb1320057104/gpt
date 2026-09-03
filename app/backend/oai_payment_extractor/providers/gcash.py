from .base import ProviderAdapter


GCASH = ProviderAdapter(
    name="gcash",
    result_field="gcash_url",
    preferred_hosts=(
        "gcash.com",
        "m.gcash.com",
        "mynt.com",
        "adyen.com",
        "checkoutshopper-live.adyen.com",
        "checkoutshopper-test.adyen.com",
    ),
)
