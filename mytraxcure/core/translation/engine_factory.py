"""模块名称：engine_factory.py

功能描述：按配置里的 backend 名字造出对应的 TranslationEngine 实例，
是上层拿到引擎的唯一入口，把"配置字符串 → 具体实现类"的映射收在一处。

关键约束：登记表存的是"模块路径:类名"字符串而不是直接 import，
避免导入本模块就顺带加载 ollama 相关依赖；新增后端只需往 _BACKENDS 加一项。
"""

from __future__ import annotations

from importlib import import_module

from mytraxcure.core.config import TranslationConfig
from mytraxcure.core.translation.translation_engine import TranslationEngine

# 后端名 → "模块路径:类名"；值写成字符串，真正 import 推迟到 create_engine
_BACKENDS: dict[str, str] = {
    "ollama": "mytraxcure.core.translation.ollama_engine:OllamaEngine",
}

def create_engine(config: TranslationConfig | None = None) -> TranslationEngine:
    """按 config.backend 查表并实例化对应引擎。

    Args:
        config: TranslationConfig，至少用到 backend；None 时回退到默认配置。

    Returns:
        TranslationEngine: 已构造的引擎实例。构造不做连通性检查，
        服务是否可用要另行调用 health_check()。

    Raises:
        ValueError: backend 不在 _BACKENDS 中；异常文案会列出当前合法的后端名，
        便于直接提示用户改配置。

    Notes:
        引擎类在调用时才 import，所以配置错误（未知后端）不会付出任何导入代价。
    """
    cfg = config or TranslationConfig()
    backend = cfg.backend
    if backend not in _BACKENDS:
        raise ValueError(f"未知翻译后端 '{backend}'，可选：{sorted(_BACKENDS)}")

    module_name, class_name = _BACKENDS[backend].split(":")
    cls = getattr(import_module(module_name), class_name)

    return cls(cfg)
