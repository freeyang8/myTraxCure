"""模块名称：__init__.py（core.translation 包入口）

功能描述：集中再导出本包的对外符号——译文缓存（CacheKey / CacheStore）、
引擎抽象（TranslationContext / TranslationEngine / TranslationResult）与引擎工厂
create_engine，让 UI 层只从包一层 import，不必关心内部模块划分。

关键约束：本文件只做再导出，不引入任何实现依赖；__all__ 即包的对外契约，
新增公共符号必须同步登记，否则通配导入与接口文档都会漏。
"""

from mytraxcure.core.translation.cache_store import CacheKey, CacheStore
from mytraxcure.core.translation.engine_factory import create_engine
from mytraxcure.core.translation.translation_engine import (
    TranslationContext,
    TranslationEngine,
    TranslationResult,
)

__all__ = [
    "CacheKey",
    "CacheStore",
    "TranslationContext",
    "TranslationEngine",
    "TranslationResult",
    "create_engine",
]
