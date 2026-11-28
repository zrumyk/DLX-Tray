"""Provider-specific execution budgets and supported translation features."""

from dataclasses import dataclass
from typing import Literal

from dlx_tray.core.chunker import DLX_MAX_UNITS, HARD_MAX_UNITS, MIN_MAX_UNITS
from dlx_tray.core.models import AnalysisTier

type ProviderName = Literal["dlx", "deepl", "cloud_ai"]


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    provider: ProviderName
    max_units: int
    max_concurrency: int
    max_segments: int
    context_units: int = 0
    analysis_tier: AnalysisTier = AnalysisTier.STANDARD

    def __post_init__(self) -> None:
        limits = PROVIDER_LIMITS.get((self.provider, self.analysis_tier))
        if limits is None:
            raise ValueError("unsupported provider and analysis tier")
        unit_cap, worker_cap, segment_cap, context_cap = limits
        if not MIN_MAX_UNITS <= self.max_units <= unit_cap:
            raise ValueError(f"max_units must be between {MIN_MAX_UNITS} and {unit_cap}")
        if not 1 <= self.max_concurrency <= worker_cap:
            raise ValueError(f"max_concurrency must be between 1 and {worker_cap}")
        if not 1 <= self.max_segments <= segment_cap:
            raise ValueError(f"max_segments must be between 1 and {segment_cap}")
        if not 0 <= self.context_units <= context_cap:
            raise ValueError(f"context_units must be between 0 and {context_cap}")

    @property
    def supports_philology(self) -> bool:
        return self.provider == "cloud_ai"


PROVIDER_LIMITS = {
    ("dlx", AnalysisTier.STANDARD): (DLX_MAX_UNITS, 2, 2, 0),
    ("deepl", AnalysisTier.STANDARD): (10_000, 1, 20, 0),
    ("cloud_ai", AnalysisTier.STANDARD): (HARD_MAX_UNITS, 4, 25, 1_500),
    ("cloud_ai", AnalysisTier.DEEP): (12_000, 2, 12, 2_000),
}

DLX_PROFILE = ProviderProfile("dlx", DLX_MAX_UNITS, 2, 2)
DEEPL_PROFILE = ProviderProfile("deepl", 5_000, 1, 20)
CLOUD_STANDARD_PROFILE = ProviderProfile("cloud_ai", 12_000, 3, 25, 1_500)
CLOUD_DEEP_PROFILE = ProviderProfile("cloud_ai", 8_000, 2, 12, 2_000, AnalysisTier.DEEP)
