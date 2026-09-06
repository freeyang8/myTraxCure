from __future__ import annotations

from mytraxcure.core.translation.translation_engine import (
    TranslationContext,
    TranslationEngine,
    TranslationResult,
)


class BuiltinEngine(TranslationEngine):

    name = "builtin"

    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        self.model_name = model_name
        self.device = device
        self._model = None
        self._tokenizer = None

    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context: TranslationContext | None = None,
    ) -> TranslationResult:
        raise NotImplementedError("TODO: 实现内置 transformers 翻译")

    def health_check(self) -> bool:
        return False  # 未实现前不可用
