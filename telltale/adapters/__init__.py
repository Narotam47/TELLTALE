from __future__ import annotations

from telltale.adapters.base import SourceAdapter
from telltale.adapters.darwinbox_wp import DarwinboxWordPressAdapter
from telltale.adapters.greenhouse import GreenhouseAdapter
from telltale.adapters.keka import KekaAdapter
from telltale.adapters.lever import LeverAdapter

ADAPTER_REGISTRY: dict[str, type[SourceAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "darwinbox": DarwinboxWordPressAdapter,
    "keka": KekaAdapter,
}


def get_adapter(adapter_type: str) -> type[SourceAdapter]:
    cls = ADAPTER_REGISTRY.get(adapter_type)
    if cls is None:
        raise KeyError(
            f"Unknown adapter_type {adapter_type!r}. "
            f"Available: {', '.join(sorted(ADAPTER_REGISTRY))}"
        )
    return cls
