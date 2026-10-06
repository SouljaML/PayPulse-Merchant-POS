from functools import lru_cache

from app.adapters.base import BaseProviderAdapter
from app.adapters.cpay import CPayAdapter
from app.adapters.mock_provider import MockProviderAdapter
from app.config import get_settings

# Real deployments register one concrete adapter per provider here, e.g.:
#   from app.adapters.mpesa import MpesaAdapter
#   from app.adapters.ecocash import EcocashAdapter
#
# Each concrete adapter subclasses BaseProviderAdapter (see adapters/base.py) and
# is constructed with that provider's base_url / api_key / callback_secret pulled
# from Settings. Keeping construction in one place means adding a 6th provider is
# a 2-line change here, nowhere else in the codebase.


@lru_cache
def get_adapter_registry() -> dict[str, BaseProviderAdapter]:
    settings = get_settings()

    return {
        # Every real provider key is mock-backed for now, so the app is fully
        # usable end to end (login, initiate a transaction, confirm via
        # callback, print a receipt) before any real provider integration
        # exists. Replace one entry at a time as each real adapter is built —
        # cpay is now real; the rest are still mock-backed.
        "mock": MockProviderAdapter(callback_secret="dev-secret"),
        "cpay": CPayAdapter(
            base_url=settings.cpay_base_url,
            api_key=settings.cpay_api_key,
            secret=settings.cpay_secret,
            client_code=settings.cpay_client_code,
            currency=settings.cpay_currency,
        ),
        "mpesa": MockProviderAdapter(callback_secret="dev-secret"),
        "ecocash": MockProviderAdapter(callback_secret="dev-secret"),
        "mywallet": MockProviderAdapter(callback_secret="dev-secret"),
        "khetsi": MockProviderAdapter(callback_secret="dev-secret"),
        # "mpesa": MpesaAdapter(
        #     base_url=settings.mpesa_base_url,
        #     api_key=settings.mpesa_api_key,
        #     callback_secret=settings.mpesa_callback_secret,
        # ),
        # "ecocash": EcocashAdapter(...),
        # "mywallet": MyWalletAdapter(...),
        # "khetsi": KhetsiAdapter(...),
    }


def get_adapter(adapter_key: str) -> BaseProviderAdapter:
    registry = get_adapter_registry()
    adapter = registry.get(adapter_key)
    if adapter is None:
        raise ValueError(f"No adapter registered for provider '{adapter_key}'")
    return adapter
