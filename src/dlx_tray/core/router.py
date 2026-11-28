"""Choose a translation provider for a complete desktop job."""

from dataclasses import dataclass

from dlx_tray.config import ProviderMode
from dlx_tray.core.chunker import DLX_MAX_UNITS, utf16_len
from dlx_tray.core.models import (
    DEFAULT_TRANSLATION_OPTIONS,
    AnalysisTier,
    StylisticRegister,
    TranslationOptions,
)
from dlx_tray.providers.base import BlockTranslator, Translator
from dlx_tray.providers.fallback import BlockFallbackTranslator, FallbackTranslator


@dataclass(frozen=True, slots=True)
class HybridRouter:
    mode: ProviderMode = "automatic"
    threshold: int = DLX_MAX_UNITS
    dlx: Translator | None = None
    cloud: BlockTranslator | None = None
    official: Translator | None = None
    dlx_starting: bool = False

    def select_route(
        self,
        text: str = "",
        *,
        file: bool = False,
        options: TranslationOptions = DEFAULT_TRANSLATION_OPTIONS,
        extract_terms: bool = False,
        allow_plain_fallback: bool = False,
    ) -> Translator | BlockTranslator | None:
        if self.mode == "cloud_only":
            return self.cloud
        if self.mode == "official_only":
            return self.official
        if self.mode == "dlx_only":
            return self.dlx

        if self.cloud is None:
            if self.dlx_starting:
                return None
            if self.dlx is not None and self.official is not None:
                return FallbackTranslator(self.dlx, self.official)
            return self.dlx or self.official

        cloud_first = (
            file
            or utf16_len(text) > self.threshold
            or options.register != StylisticRegister.NEUTRAL
            or bool(options.style_context or options.glossary or options.document_glossary)
            or options.include_notes
            or options.analysis_tier == AnalysisTier.DEEP
            or extract_terms
        )
        if cloud_first:
            if allow_plain_fallback and not options.include_notes and self.official is not None:
                return BlockFallbackTranslator(self.cloud, self.official)
            return self.cloud
        if self.dlx is not None:
            return FallbackTranslator(self.dlx, self.cloud, self.official)
        if self.official is not None:
            return BlockFallbackTranslator(self.cloud, self.official)
        return self.cloud
