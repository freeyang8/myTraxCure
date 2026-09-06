from __future__ import annotations

from enum import Enum

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import QWidget

from mytraxcure.core.translation.translation_engine import TranslationResult
from mytraxcure.core.types import Region


class OverlayMode(Enum):
    TRANSLUCENT = "translucent"  # 半透明浮层（默认）
    REPLACE = "replace"  # 替换式覆盖

class OverlayView(QWidget):

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._region: Region | None = None
        self._result: TranslationResult | None = None
        self._mode = OverlayMode.TRANSLUCENT
        self._loading = False

    # ---- 接口 ------------------------------------------------------------
    
    #高度估算方法
    def _fit_region_for_text(self, region: Region, text: str) -> Region:
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
        self._region = self._fit_region_for_text(region, result.text)
        self._result = result
        if mode is not None:
            self._mode = mode
        self._loading = False
        self.setVisible(True)
        self.update()

    def show_loading(self, region: Region) -> None:
        self._region = region
        self._loading = True
        self.setVisible(True)
        self.update()

    def hide_overlay(self) -> None:
        self._region = None
        self._result = None
        self._loading = False
        self.setVisible(False)
        self.update()
    #查询状态
    @property
    def is_showing(self)->bool:
        return self._region is not None
    
    @property
    def is_loading(self)->bool:
        return self._loading
    
    def set_mode(self, mode: OverlayMode) -> None:
        self._mode = mode
        self.update()

    # ---- 渲染 ------------------------------------------------------------
    def paintEvent(self, event) -> None:
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

            #文本换行处理
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
            #截断处理（超出显示行数）
            show_more = False
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                lines[-1] = lines[-1].rstrip() + "…"
                show_more = True
            #绘制正文内容
            painter.setPen(QColor(245, 245, 245))
            y = body_rect.y()
            for line in lines:
                painter.drawText(body_rect.x(), y + fm.ascent(), line)
                y += line_h
            #绘制"展开"提示
            if show_more:
                painter.setPen(QColor(120, 220, 255))
                painter.drawText(
                    body_rect.x(),
                    min(y + fm.ascent(), body_rect.bottom() - 2),
                    "▼ 展开",
                )
        finally:
            painter.end()
        # painter = QPainter(self)
        # if self._loading:
        #     painter.setBrush(QColor(0, 0, 0, 120))
        #     painter.drawRect(self._region.x, self._region.y, self._region.w, self._region.h)
        #     painter.setPen(QColor(255, 255, 255))

        #     return
