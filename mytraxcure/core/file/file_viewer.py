from __future__ import annotations

import math
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
from mytraxcure.core.file.sentence_splitter import (
    is_heading,
    is_separator,
    split_sentences,
    strip_heading,
)
from mytraxcure.core.types import TextBlock

if TYPE_CHECKING:
    from mytraxcure.core.config import ReadingConfig
    from mytraxcure.core.file.text_mapper import TextMapper

# 重排版面参数（文档坐标，所有文档类型统一）
# 大字号 + 大行高：文本块 bbox 更大，凝视命中/段落选中概率显著提高（眼动输入精度有限）
_LINE_HEIGHT = 34      # 行高（像素，需 ≥ 字号 × 1.5，否则行重叠）
_MARGIN = 40           # 左右边距（像素）
_FONT_SIZE = 20        # 字号（磅）
_WRAP_WIDTH = 960      # 内容最大宽度（像素；超长行折行）
_PARA_GAP = _LINE_HEIGHT // 2   # 段落间距 = 0.5 行高（需求指定）
_MIN_HEIGHT = 400      # 版面最小高度，避免短文档贴顶

STEP_SOURCE = "STEP"   # 逐句模式合成块的来源标记

# 阅读区配色：暖白纸色 + 近黑墨色（纯白底 + 纯黑字长时间读会刺眼）
_PAPER = (250, 248, 244)
_INK = (22, 24, 28)

def _make_font(pixel_size: int | None = None, bold: bool = False):
    from PyQt6.QtGui import QFont

    font = QFont()
    font.setFamilies(["Microsoft YaHei", "SimSun", "Arial"])
    if pixel_size is None:
        font.setPointSize(_FONT_SIZE)
    else:
        font.setPixelSize(int(pixel_size))
    if bold:
        font.setBold(True)
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
        from PyQt6.QtGui import QColor, QImage, QPainter

        layout = self._ensure_layout()
        w, h = max(int(width), 1), max(int(height), 1)
        qimg = QImage(w, h, QImage.Format.Format_RGB888)
        qimg.fill(QColor(*_PAPER))

        painter = QPainter(qimg)
        painter.setFont(self._font)
        painter.setPen(QColor(*_INK))
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
                # 二分求本行还能放几个字符：逐字回退在几千字的无标点长句上是 O(n²)，
                # 会把界面直接卡死（逐句模式下一步就是整个长句，比段落模式更容易撞上）
                lo, hi = 1, max(e - s - 1, 1)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if fm.horizontalAdvance(text[s : s + mid]) <= max_w:
                        lo = mid
                    else:
                        hi = mid - 1
                k = max(lo, 1)
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


# ---- 逐句步进阅读 ----------------------------------------------------------
@dataclass
class StepUnit:
    """一句 = 一屏里的一个显示单位（句子同时也是凝视/翻译的最小单位）。"""

    index: int
    text: str
    heading: bool
    block: TextBlock   # 合成块：para_key 唯一，映射器据此命中/取文本/建缓存键


@dataclass
class StepScreen:
    """一屏的排版结果：视口尺寸或首句变了就重算。"""

    top: int
    n: int
    gap: float
    pad_top: float
    font_px: int
    line_h: int
    content_w: int
    margin_x: int
    avail: float
    heights: list[float]
    lines: list[list[str]]
    solo: bool = False
    shrunk: bool = False

    @property
    def used_height(self) -> float:
        return sum(self.heights) + self.gap * max(self.n - 1, 0)


class StepRenderer:
    """逐句步进版面：每屏独立排版，只画能完整显示的 N 句。

    与 DocumentRenderer（整篇连续纵向版面 + 像素滚动）不同，这里没有全局滚动坐标：
    句高、字号、段间距都按当前视口算，因此映射器看到的永远是"屏内坐标"
    （scroll_offset 恒 0），每次换屏把句子级 block 重注册一遍。
    """

    def __init__(self, document: ParsedDocument, reading: ReadingConfig) -> None:
        self.document = document
        self.reading = reading
        self._units: list[StepUnit] | None = None
        self._top = 0
        self._solo_offset = 0.0
        self._screen: StepScreen | None = None
        self._screen_key: tuple[int, int, int] | None = None
        self._measured: dict[tuple[int, int], list[tuple[list[tuple[int, int]], int, float]]] = {}
        self._fonts: dict[int, Any] = {}
        self._mapper: TextMapper | None = None

    def set_mapper(self, mapper: TextMapper | None) -> None:
        self._mapper = mapper

    # ---- 句子单位 --------------------------------------------------------
    @property
    def units(self) -> list[StepUnit]:
        if self._units is None:
            self._units = self._build_units()
        return self._units

    def _build_units(self) -> list[StepUnit]:
        units: list[StepUnit] = []
        for _para_key, blocks in group_paragraphs(self.document.blocks):
            is_notice = blocks[0].source == NOTICE_SOURCE
            text = blocks[0].text if is_notice else paragraph_text(blocks)
            if not text.strip() or is_separator(text):
                continue
            heading = is_heading(text)
            body = strip_heading(text) if heading else text
            for sentence in split_sentences(body, 0, heading):
                index = len(units)
                units.append(
                    StepUnit(
                        index=index,
                        text=sentence.text,
                        heading=heading,
                        block=TextBlock(
                            text=sentence.text,
                            page=blocks[0].page,
                            x=0.0,
                            y=0.0,
                            w=0.0,
                            h=0.0,
                            block_id=f"sentence-{index}",
                            source=STEP_SOURCE,
                            para_key=f"sentence-{index}",
                        ),
                    )
                )
        return units

    # ---- 排版 ------------------------------------------------------------
    @property
    def screen(self) -> StepScreen | None:
        return self._screen

    def content_size(self) -> tuple[int, int]:
        """步进模式没有可滚动的长版面：返回视口尺寸本身，滚轮位移被夹成 0。"""
        if self._screen_key is None:
            return (0, 0)
        return (self._screen_key[0], self._screen_key[1])

    def ensure_screen(self, width: int, height: int) -> StepScreen:
        key = (int(width), int(height), self._top)
        if self._screen is not None and self._screen_key == key:
            return self._screen
        screen = self._compute_screen(int(width), int(height))
        self._screen, self._screen_key = screen, (int(width), int(height), screen.top)
        self._sync_blocks(screen)
        return screen

    def _font(self, px: int, bold: bool = False) -> Any:
        font = self._fonts.get((px, bold))
        if font is None:
            font = _make_font(px, bold)
            self._fonts[(px, bold)] = font
        return font

    def _measure(self, content_w: int, font_px: int) -> list[tuple[list[tuple[int, int]], int, float]]:
        """每个句子的折行区间、行数、最大行宽（按内容宽度+字号缓存）。

        标题用粗体量、粗体画：量高与绘制必须同一字体，否则折行差一行就会画出半截。
        """
        from PyQt6.QtGui import QFontMetrics

        key = (content_w, font_px)
        cached = self._measured.get(key)
        if cached is not None:
            return cached

        metrics = {
            False: QFontMetrics(self._font(font_px, False)),
            True: QFontMetrics(self._font(font_px, True)),
        }
        rows: list[tuple[list[tuple[int, int]], int, float]] = []
        for unit in self.units:
            fm = metrics[unit.heading]
            ranges = DocumentRenderer._wrap_ranges(fm, unit.text, content_w)
            width = max(
                (fm.horizontalAdvance(unit.text[a:b]) for a, b in ranges), default=0
            )
            rows.append((ranges, len(ranges), float(width)))
        self._measured[key] = rows
        return rows

    def _compute_screen(self, width: int, height: int) -> StepScreen:
        cfg = self.reading
        units = self.units
        margin_x = max(int(cfg.margin_x_vw * width), cfg.margin_x_min)
        content_w = max(width - 2 * margin_x, 160)
        avail = max(float(height) - 2 * cfg.margin_y, 80.0)
        if not units:
            return StepScreen(0, 0, 0.0, 0.0, int(cfg.font_px), 0, content_w, margin_x, avail, [], [])

        self._top = min(max(self._top, 0), len(units) - 1)
        for _ in range(2):  # 第二遍：首句越界时夹紧后重算
            font_px = int(cfg.font_px)
            shrunk = False
            while True:
                line_h = max(int(round(font_px * cfg.line_height_em)), 1)
                gap_target = font_px * cfg.gap_em
                measured = self._measure(content_w, font_px)
                heights: list[float] = []
                used = 0.0
                for i in range(self._top, len(units)):
                    h = float(measured[i][1] * line_h)
                    add = h + (gap_target if heights else 0.0)
                    if used + add > avail:
                        break
                    used += add
                    heights.append(h)
                if heights or font_px <= cfg.min_font_px:
                    break
                font_px -= 1
                shrunk = True

            n, solo = len(heights), False
            if n == 0:  # 到字号下限仍放不下 → 该句独占一屏，内部可滚
                solo = True
                n = 1
                heights = [float(measured[self._top][1] * line_h)]
                used = heights[0]
            if self._top + n > len(units):
                self._top = max(len(units) - n, 0)
                continue
            break

        leftover = avail - used
        gap = 0.0
        # 独占一屏时顶部对齐：剩余高度是负的，居中的会把这句推出视口
        pad_top = float(cfg.margin_y) + (0.0 if solo else leftover / 2)
        if n > 1:
            max_gap = font_px * cfg.max_gap_em
            gap = min(gap_target + leftover / (n - 1), max_gap)
            content_h = used + (gap - gap_target) * (n - 1)
            pad_top = float(cfg.margin_y) + (avail - content_h) / 2

        lines = [
            [units[i].text[a:b] for a, b in measured[i][0]]
            for i in range(self._top, self._top + n)
        ]
        return StepScreen(
            top=self._top, n=n, gap=gap, pad_top=pad_top, font_px=font_px, line_h=line_h,
            content_w=content_w, margin_x=margin_x, avail=avail, heights=heights,
            lines=lines, solo=solo, shrunk=shrunk,
        )

    # ---- 渲染 ------------------------------------------------------------
    def render_viewport(self, x_offset: float, y_offset: float, width: int, height: int) -> np.ndarray:
        """画当前这一屏；x/y 偏移按设计恒为 0（无全局滚动坐标）。"""
        from PyQt6.QtCore import QRect, Qt
        from PyQt6.QtGui import QColor, QImage, QPainter

        w, h = max(int(width), 1), max(int(height), 1)
        qimg = QImage(w, h, QImage.Format.Format_RGB888)
        qimg.fill(QColor(*_PAPER))
        screen = self.ensure_screen(w, h)

        painter = QPainter(qimg)
        painter.setPen(QColor(*_INK))
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        y = screen.pad_top - (self._solo_offset if screen.solo else 0.0)
        for k in range(screen.n):
            painter.setFont(self._font(screen.font_px, self.units[screen.top + k].heading))
            for li, line in enumerate(screen.lines[k]):
                painter.drawText(
                    QRect(screen.margin_x, int(y + li * screen.line_h), screen.content_w, screen.line_h),
                    flags,
                    line,
                )
            y += screen.heights[k] + screen.gap
        painter.end()
        return DocumentRenderer._qimage_to_rgb_array(qimg, w, h)

    # ---- 步进 ------------------------------------------------------------
    def step(self, delta: int) -> bool:
        """向下/向上走一句；到顶、到底或超长句独占时返回 False。"""
        screen = self._screen
        units = self.units
        if screen is None or not units or screen.solo:
            return False
        if delta > 0:
            if screen.top + screen.n >= len(units):
                return False
            self._top = screen.top + 1
        else:
            if screen.top <= 0:
                return False
            self._top = screen.top - 1
        self._solo_offset = 0.0
        return True

    def solo_scroll(self, dy: float) -> bool:
        """超长句独占一屏时，滚轮先走它自己的内部滚动；到边界返回 False 交回整句步进。

        dy > 0 = 向下滚（内容上移）。
        """
        screen = self._screen
        if screen is None or not screen.solo:
            return False
        max_off = max(screen.heights[0] - screen.avail, 0.0)
        want = min(max(self._solo_offset + dy, 0.0), max_off)
        if want == self._solo_offset:
            return False
        self._solo_offset = want
        return True

    # ---- 映射器同步 ------------------------------------------------------
    def _sync_blocks(self, screen: StepScreen) -> None:
        """可见句写屏内坐标；其余句停到远处，避免被"最近块回退"误命中。

        全部句子都要注册（而不是只注册可见的）：预翻译取的是 mapper.segments()，
        只注册可见的两三句会让整本书只预翻译首屏。
        """
        if self._mapper is None:
            return
        measured = self._measure(screen.content_w, screen.font_px)
        tops: list[float] = []
        y = screen.pad_top
        for k in range(screen.n):
            tops.append(y)
            y += screen.heights[k] + screen.gap
        screen_height = screen.avail + 2 * self.reading.margin_y

        for unit in self.units:
            block = unit.block
            offset = unit.index - screen.top
            if 0 <= offset < screen.n:
                block.x = float(screen.margin_x)
                block.y = tops[offset]
                block.h = screen.heights[offset]
                block.w = measured[unit.index][2]
                # 容差上限 = 与相邻句空隙的一半：相邻两块的命中域正好在中线相接，
                # 容差给多大都不会串到别句。首/末句的外侧没有邻居（只有屏边留白），
                # 那一侧不设限——否则首句只会分到 pad_top(24px) 的一半，反而更难命中
                half_prev = (
                    (block.y - (tops[offset - 1] + screen.heights[offset - 1])) / 2
                    if offset > 0 else math.inf
                )
                half_next = (
                    (tops[offset + 1] - block.bottom) / 2
                    if offset + 1 < screen.n else math.inf
                )
                block.hit_pad_max = max(min(half_prev, half_next), 0.0)
            else:
                block.x, block.y, block.w, block.h = 0.0, -1e5, 0.0, 0.0
                block.hit_pad_max = 0.0
        self._mapper.register_blocks([u.block for u in self.units])
