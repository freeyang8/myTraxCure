"""模块名称：overlay_view.py

功能描述：覆盖在原文之上的译文浮层。接收一块屏幕矩形与翻译结果，自己估算所需高度、
折行与截断，用 QPainter 直接画出半透明译文卡片；翻译未回来时先画 loading 态。

关键约束：
- 本控件设了 WA_TransparentForMouseEvents：它必须始终"看得见摸不着"，否则浮层会
  挡住原文的凝视命中与点击，反而破坏它自己的触发来源。
- 所有绘制都在 paintEvent 里现场算（包括折行），不缓存排版结果。浮层尺寸随原文区域
  变化，缓存需要在 region 变化时失效，收益抵不上同步成本。
- paintEvent 里用 try/finally 保证 QPainter 一定 end()；提前 return 的三条分支都在
  try 内部，漏掉 end() 会让 Qt 在控制台刷 Painter not active 一类告警。
"""

from __future__ import annotations

from enum import Enum

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import QWidget

from mytraxcure.core.translation.translation_engine import TranslationResult
from mytraxcure.core.types import Region


class OverlayMode(Enum):
    """浮层的两种背景样式。

    TRANSLUCENT 保留原文可见度，适合对着原文校验译文；REPLACE 几乎完全盖住原文，
    适合只想读译文的场景。
    """

    TRANSLUCENT = "translucent"  # 半透明浮层（默认）
    REPLACE = "replace"  # 替换式覆盖


class OverlayView(QWidget):
    """译文浮层控件：一块区域 + 一份译文，画完之后自动隐藏其内容。

    生命周期由外部驱动：主窗口在段被选中时 show_translation / show_loading，
    在选中失效时 hide_overlay。控件本身不订阅任何信号，也不持有文档状态。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """建立空浮层：无区域、无译文、非加载态。

        Args:
            parent: 父窗口，通常是阅读区容器。
        """
        super().__init__(parent)
        # 浮层必须对所有鼠标事件透明：它盖在原文上，若吃掉事件会挡住自己的触发来源
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._region: Region | None = None
        self._result: TranslationResult | None = None
        self._mode = OverlayMode.TRANSLUCENT
        self._loading = False

    # ---- 接口 ------------------------------------------------------------

    def _fit_region_for_text(self, region: Region, text: str) -> Region:
        """按译文长度把区域高度撑到够用；够高时保持原样。

        Args:
            region: 原文所在区域（屏幕坐标）。
            text: 译文全文。

        Returns:
            宽度不变、高度取原高与实际所需二者较大值的新 Region。

        Notes:
            高度只能往下扩：译文比原文长是常态，缩回去会让浮层跟原文错位。
            估算用固定的 11 号雅黑与正文宽度重建行号，与 paintEvent 里的折行规则一致，
            两处规则一旦不同步就会出现"估算够但实际被截断"。
        """
        font = QFont("Microsoft YaHei", 11)
        fm = QFontMetrics(font)
        line_h = fm.height() + 3
        avail_w = max(region.w - 20, 50)
        lines = 0
        for paragraph in text.split("\n") or [""]:
            line_w = 0
            lines += 1
            for ch in paragraph:
                cw = fm.horizontalAdvance(ch)
                if line_w + cw > avail_w and line_w > 0:
                    lines += 1
                    line_w = cw
                else:
                    line_w += cw
        # 估算译文实际需要的高度：header(约30px) + 正文行数 + 边距；
        # 原区域装不下时向下扩展，否则保持原高
        needed_h = 30 + lines * line_h + 12
        return Region(
            x=region.x,
            y=region.y,
            w=region.w,
            h=max(region.h, needed_h),
        )

    def show_translation(
        self,
        region: Region,
        result: TranslationResult,
        mode: OverlayMode | None = None,
    ) -> None:
        """显示译文，并按需要切换背景模式。

        Args:
            region: 原文区域（屏幕坐标）；高度会被撑到装得下译文。
            result: 翻译结果，header 里展示它的来源后端、模型与耗时。
            mode: 想同时切换的浮层模式；传 None 表示沿用上一次的模式。
        """
        self._region = self._fit_region_for_text(region, result.text)
        self._result = result
        if mode is not None:
            self._mode = mode
        self._loading = False
        self.setVisible(True)
        self.update()

    def show_loading(self, region: Region) -> None:
        """显示加载态：区域按原文原样保留，不撑高。

        Args:
            region: 原文区域（屏幕坐标）。
        """
        self._region = region
        self._loading = True
        self.setVisible(True)
        self.update()

    def hide_overlay(self) -> None:
        """清空内容并隐藏；下一次显示会重新走完整的状态设置。"""
        self._region = None
        self._result = None
        self._loading = False
        self.setVisible(False)
        self.update()

    # ---- 查询状态 --------------------------------------------------------
    @property
    def is_showing(self)->bool:
        """浮层当前是否有内容（含加载态）。"""
        return self._region is not None

    @property
    def is_loading(self)->bool:
        """是否处于等待译文的状态。"""
        return self._loading

    def set_mode(self, mode: OverlayMode) -> None:
        """切换背景模式并触发重绘；不影响已显示的译文内容。

        Args:
            mode: 目标模式。
        """
        self._mode = mode
        self.update()

    # ---- 渲染 ------------------------------------------------------------
    def paintEvent(self, event) -> None:
        """绘制浮层：加载态画遮罩 + 提示，否则画 header 与折行后的译文正文。

        Args:
            event: Qt 的重绘事件，本实现不使用（浮层状态全在成员变量里）。

        Notes:
            这是 Qt 每次重绘都会调用的覆写方法，因此必须幂等且只读取成员状态。
            超出行数的正文会被截断并在末行补省略号，"▼ 展开"提示会再与底边取较小值，
            确保不会画到卡片外面。
        """
        if self._region is None:
            return
        painter = QPainter(self)
        try:
            r = self._region
            rect = QRect(r.x,r.y,r.w,r.h)
            if self._loading:
                painter.fillRect(rect,QColor(0, 0, 0, 120))
                painter.setPen(QColor(255, 255, 255))
                painter.setFont(QFont("Microsoft YaHei", 11))
                painter.drawText(
                self._region.x + 8, self._region.y + 24, "翻译中…"
                )
                return
            if self._result is None:
                return
            if self._mode == OverlayMode.REPLACE:
                painter.fillRect(rect, QColor(20, 20, 30, 235))
            else:
                painter.fillRect(rect, QColor(20, 20, 30, 175))
            header = (
                f"译文 · {self._result.backend}/{self._result.model}"
                f" · {self._result.elapsed_ms}ms"
            )
            if self._result.cached:
                header += "(缓存)"

            painter.setPen(QColor(120, 220, 255))
            painter.setFont(QFont("Microsoft YaHei", 9))
            flags = int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            painter.drawText(
                rect.adjusted(10, 8, -10, -10),
                flags,
                header,
            )
            body_rect = rect.adjusted(10, 30, -10, -10)
            font = QFont("Microsoft YaHei", 11)
            painter.setFont(font)
            fm = QFontMetrics(font)
            line_h = fm.height() + 3
            max_lines = max(body_rect.height() // line_h, 1) #至少 1 行

            #逐字符折行：逐字符判宽是为了兼容无空格的中文长段，按空格切会整段不换行
            lines: list[str] = []
            for paragraph in (self._result.text.split("\n") or [""]):
                line = ""
                for ch in paragraph:
                    if fm.horizontalAdvance(line + ch) > body_rect.width() and line:
                        lines.append(line)
                        line = ch
                    else:
                        line += ch
                lines.append(line)
            #超出显示行数时截断，末行补省略号并标记折叠
            show_more = False
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                lines[-1] = lines[-1].rstrip() + "…"
                show_more = True
            painter.setPen(QColor(245, 245, 245))
            y = body_rect.y()
            for line in lines:
                painter.drawText(body_rect.x(), y + fm.ascent(), line)
                y += line_h
            #折叠时给个"展开"提示；纵坐标再与底边取小值，避免画到卡片外面
            if show_more:
                painter.setPen(QColor(120, 220, 255))
                painter.drawText(
                    body_rect.x(),
                    min(y + fm.ascent(), body_rect.bottom() - 2),
                    "▼ 展开",
                )
        finally:
            painter.end()
        # 以下是被当前实现对调回去的中间草稿（画轮廓 + 只画遮罩），暂留作对照：
        # painter = QPainter(self)
        # if self._loading:
        #     painter.setBrush(QColor(0, 0, 0, 120))
        #     painter.drawRect(self._region.x, self._region.y, self._region.w, self._region.h)
        #     painter.setPen(QColor(255, 255, 255))
