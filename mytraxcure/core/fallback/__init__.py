"""模块名称：__init__.py（fallback 包）

功能描述：输入源降级策略的包入口。FallbackManager 决定本次会话用眼动还是鼠标充当视线输入，
眼动侧初始化失败时统一降级到鼠标，并保留一句可展示给用户的失败原因。

对外暴露：FallbackManager、InputMode。
"""

from mytraxcure.core.fallback.fallback_manager import FallbackManager, InputMode

__all__ = ["FallbackManager", "InputMode"]
