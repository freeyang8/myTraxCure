from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from mytraxcure.core.file.file_parser import ParsedDocument
from mytraxcure.core.file.segmenter import (
    NOTICE_SOURCE,
    group_paragraphs,
    paragraph_text,
    paragraph_text_with_spans,
)
from mytraxcure.core.types import TextBlock

if TYPE_CHECKING:
    from mytraxcure.core.file.text_mapper import TextMapper

# 重排版面参数（文档坐标，所有文档类型统一）
# 大字号 + 大行高：文本块 bbox 更大，凝视命中/段落选中概率显著提高（眼动输入精度有限）
_LINE_HEIGHT = 34      # 行高（像素，需 ≥ 字号 × 1.5，否则行重叠）
_MARGIN = 40           # 左右边距（像素）
_FONT_SIZE = 20        # 字号（磅）
_WRAP_WIDTH = 960      # 内容最大宽度（像素；超长行折行）
_PARA_GAP = _LINE_HEIGHT // 2   # 段落间距 = 0.5 行高（需求指定）
_MIN_HEIGHT = 400      # 版面最小高度，避免短文档贴顶

def _make_font():
    from PyQt6.QtGui import QFont

    font = QFont()
    font.setFamilies(["Microsoft YaHei", "SimSun", "Arial"])
    font.setPointSize(_FONT_SIZE)
    return font

@dataclass
class ParagraphLayout:

    para_key: str
    y0: int
    y1: int
    lines: list[str]
    blocks: list[TextBlock]
    is_notice: bool = False

@dataclass
class ReflowLayout:

    paragraphs: list[ParagraphLayout] = field(default_factory=list)
    width: int = 0
    height: int = 0

class DocumentRenderer:
    """全类型统一重排渲染：按段落顺序纵向排布，只渲染可视区域。

    v1 不分页——整篇文档就是一条连续的纵向版面（逻辑页恒为 0）。
    不预先拼整页大图：超长文档（几千段）会撞上 Qt 位图的 32767px 上限，
    改为按视口渲染后，每帧只分配"窗口大小"的图，滚动深度不受限。
    """

    def __init__(self, document: ParsedDocument) -> None:
        self.document = document
        self._layout: ReflowLayout | None = None
        self._font: Any = None
        # 回填 bbox 后重新注册文本块（命中/段落聚合依赖新 bbox）
        self._mapper: TextMapper | None = None

    def set_mapper(self, mapper: TextMapper | None) -> None:
        self._mapper = mapper

    # ---- 版面 ------------------------------------------------------------
    def _ensure_layout(self) -> ReflowLayout:
        if self._layout is not None:
            return self._layout

        from PyQt6.QtGui import QFontMetrics

        font = _make_font()
        fm = QFontMetrics(font)
        self._font = font

        paragraphs: list[ParagraphLayout] = []
        y = 0
        for para_key, blocks in group_paragraphs(self.document.blocks):
            is_notice = blocks[0].source == NOTICE_SOURCE
            text = blocks[0].text if is_notice else paragraph_text(blocks)
            if not text.strip():
                continue

            ranges = self._wrap_ranges(fm, text, _WRAP_WIDTH)
            lines = [text[a:b] for a, b in ranges]
            y0 = y
            y1 = y + len(lines) * _LINE_HEIGHT

            if is_notice:
                self._fill_blocks(blocks, lines=lines, y0=y0, fm=fm)
            else:
                self._backfill_blocks(blocks, text, ranges, y0, fm)
            paragraphs.append(
                ParagraphLayout(para_key, y0, y1, lines, blocks, is_notice)
            )
            y = y1 + _PARA_GAP

        self._layout = ReflowLayout(
            paragraphs=paragraphs,
            width=_WRAP_WIDTH + _MARGIN * 2,
            height=max(y - _PARA_GAP, _MIN_HEIGHT),
        )
        self.document.meta["layout_pending"] = False
        if self._mapper is not None:
            self._mapper.register_blocks(self.document.blocks)
        return self._layout

    def content_size(self) -> tuple[int, int]:
        layout = self._ensure_layout()
        return layout.width, layout.height

    def _backfill_blocks(
        self, blocks: list[TextBlock], text: str, ranges: list[tuple[int, int]], y0: int, fm: Any
    ) -> None:
        """把重排后的 bbox 写回 block：命中和段落并集都读这份坐标。

        每个 block 分到它在段落文本里占的字符区间所覆盖的那几行——
        段内 block 于是天然沿 y 连续，段落并集 bbox 就等于整段的排版范围。
        """
        _, spans = paragraph_text_with_spans(blocks)
        covered: set[int] = set()
        starts = [a for a, _ in ranges]
        for b, start, end in spans:
            i0 = max(bisect_right(starts, start) - 1, 0)
            i1 = max(bisect_right(starts, end - 1) - 1, i0)
            b.x = float(_MARGIN)
            b.y = float(y0 + i0 * _LINE_HEIGHT)
            b.h = float((i1 - i0 + 1) * _LINE_HEIGHT)
            b.w = float(max(fm.horizontalAdvance(text[a:c]) for a, c in ranges[i0 : i1 + 1]))
            covered.add(id(b))
        for b in blocks:
            if id(b) not in covered:  # 空白块：零尺寸框，标在段首（不会抢命中）
                b.x, b.y, b.w, b.h = float(_MARGIN), float(y0), 0.0, 0.0

    def _fill_blocks(
        self, blocks: list[TextBlock], lines: list[str], y0: int, fm: Any
    ) -> None:
        """提示块（扫描页占位）整段一个框。"""
        width = float(max(fm.horizontalAdvance(line) for line in lines))
        for b in blocks:
            b.x = float(_MARGIN)
            b.y = float(y0)
            b.w = width
            b.h = float(len(lines) * _LINE_HEIGHT)

    # ---- 渲染 ------------------------------------------------------------
    def render_viewport(
        self, x_offset: float, y_offset: float, width: int, height: int
    ) -> np.ndarray:
        """渲染文档坐标 (x_offset, y_offset) 起、width×height 大的可视区域。"""
        from PyQt6.QtCore import QRect, Qt
        from PyQt6.QtGui import QImage, QPainter

        layout = self._ensure_layout()
        w, h = max(int(width), 1), max(int(height), 1)
        qimg = QImage(w, h, QImage.Format.Format_RGB888)
        qimg.fill(Qt.GlobalColor.white)

        painter = QPainter(qimg)
        painter.setFont(self._font)
        painter.setPen(Qt.GlobalColor.black)
        painter.translate(-x_offset, -y_offset)  # 之后一律按文档坐标绘制
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for para in layout.paragraphs:
            if para.y1 < y_offset or para.y0 > y_offset + h:
                continue  # 视口外的段落直接跳过
            for k, line in enumerate(para.lines):
                painter.drawText(
                    QRect(_MARGIN, para.y0 + k * _LINE_HEIGHT, _WRAP_WIDTH, _LINE_HEIGHT),
                    flags,
                    line,
                )
        painter.end()
        return self._qimage_to_rgb_array(qimg, w, h)

    # ---- 折行 ------------------------------------------------------------
    @staticmethod
    def _wrap_text(fm: Any, text: str, max_w: int) -> list[str]:
        return [text[a:b] for a, b in DocumentRenderer._wrap_ranges(fm, text, max_w)]

    @staticmethod
    def _wrap_ranges(fm: Any, text: str, max_w: int) -> list[tuple[int, int]]:
        """按宽度折行，返回每行的字符区间 [start, end)。

        返回区间而不是字符串：渲染要用行文本，回填 bbox 还要知道"每个 block 占了哪几行"。
        """
        if not text:
            return [(0, 0)]
        ranges: list[tuple[int, int]] = []
        cur: tuple[int, int] | None = None
        cur_w = 0
        for m in re.finditer(r"\S+\s*", text):
            s, e = m.start(), m.end()
            # token 自身超宽（长英文串 / 无空格 CJK 整段）→ 按字符切
            while s < e and fm.horizontalAdvance(text[s:e]) > max_w:
                k = e - s - 1
                while k > 1 and fm.horizontalAdvance(text[s : s + k]) > max_w:
                    k -= 1
                k = max(k, 1)
                if cur is not None:
                    ranges.append(cur)
                    cur, cur_w = None, 0
                ranges.append((s, s + k))
                s += k
            if s >= e:
                continue
            seg_w = fm.horizontalAdvance(text[s:e])
            if cur is None:
                cur, cur_w = (s, e), seg_w
            elif cur_w + seg_w > max_w:
                ranges.append(cur)
                cur, cur_w = (s, e), seg_w
            else:
                cur, cur_w = (cur[0], e), cur_w + seg_w
        if cur is not None:
            ranges.append(cur)
        return ranges or [(0, 0)]

    @staticmethod
    def _qimage_to_rgb_array(qimg: Any, w: int, h: int) -> np.ndarray:
        bpl = qimg.bytesPerLine()
        buf = qimg.constBits()
        buf.setsize(bpl * h)
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl)
        # 去掉行尾 padding，紧致成 (H, W, 3)
        arr = arr[:, : w * 3].reshape(h, w, 3)
        return arr.copy()

    # ---- 缓存 ------------------------------------------------------------
    def invalidate_cache(self) -> None:
        self._layout = None
