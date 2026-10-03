from __future__ import annotations

from importlib import import_module

from mytraxcure.core.config import TranslationConfig
from mytraxcure.core.translation.translation_engine import TranslationEngine

# 后端名 → 构造器（惰性导入，避免顶层加载 ollama）
_BACKENDS: dict[str, str] = {
    "ollama": "mytraxcure.core.translation.ollama_engine:OllamaEngine",
}

def create_engine(config: TranslationConfig | None = None) -> TranslationEngine:
    cfg = config or TranslationConfig()
    backend = cfg.backend
    if backend not in _BACKENDS:
        raise ValueError(f"未知翻译后端 '{backend}'，可选：{sorted(_BACKENDS)}")

    module_name, class_name = _BACKENDS[backend].split(":")
    cls = getattr(import_module(module_name), class_name)

    return cls(cfg)
