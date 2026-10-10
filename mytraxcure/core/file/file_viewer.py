"""模块名称：file_viewer.py

功能描述：把 ParsedDocument 排成可渲染的版面并画出来，是全类型文档唯一的落地环节。
两条并行链路：DocumentRenderer 负责"整篇连续纵向版面 + 像素滚动"的段落模式，
StepRenderer 负责"每屏 N 句、整句步进"的逐句模式；两者都借 TextMapper 回填 bbox。

关键约束：
- 该模块是 TXT / Word 获得坐标的唯一通路：它们解析期 bbox 恒为 0，必须等这里的
  重排把 bbox 回填，才谈得上凝视命中与段落聚合。回填完会置 document.meta 的
  layout_pending 为 False，这是"可以开始命中"的信号。
- 版面参数（行高 / 边距 / 字号）是成组的：它们共同决定 bbox 大小，而眼动输入的命中率
  直接取决于 bbox 大小。单独调一个会让实测的命中率失效。
- 重的结果会被 StepRenderer 按"屏内坐标"覆盖：同一批 block 的坐标含义取决于当前由
  哪个渲染器接管，切换阅读模式必须重新注册。
"""

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
    """构造阅读用字体。

    Args:
        pixel_size: 像素字号；传 None 则用 _FONT_SIZE 的磅值。
            逐句模式要按视口自适应字号，只能传像素值。
        bold: 是否加粗，标题排版使用。

    Returns:
        QFont 实例。

    Notes:
        PyQt6 在函数内部延迟导入而非模块级导入：本模块会被纯数据与单元测试路径引用，
        模块级导入会把 Qt 依赖带给不需要渲染的调用方。
        字体族按"微软雅黑 → 宋体 → Arial"回退，覆盖中英文混排的缺字情况。
    """
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
    """一个段落排完后的结果：纵向位置、折出的行、成员块与是否占位提示。

    Attributes 之外的约定：blocks 与 lines 指向同一批数据——lines 是文本层面的折行结果，
    blocks 的 bbox 也已按这些行回填完毕，二者不可只更新其一。
    """

    para_key: str
    y0: int
    y1: int
    lines: list[str]
    blocks: list[TextBlock]
    is_notice: bool = False


@dataclass
class ReflowLayout:
    """整篇文档的一次重排结果：段落序列与版面尺寸（width / height）。"""

    paragraphs: list[ParagraphLayout] = field(default_factory=list)
    width: int = 0
    height: int = 0


class DocumentRenderer:
    """全类型统一重排渲染：按段落顺序纵向排布，只渲染可视区域。

    不分页——整篇文档就是一条连续的纵向版面（逻辑页恒为 0）。
    不预先拼整页大图：超长文档（几千段）会撞上 Qt 位图的 32767px 上限，
    改为按视口渲染后，每帧只分配"窗口大小"的图，滚动深度不受限。

    生命周期：构造后由排版惰性触发 _ensure_layout，耗时只发生一次；
    视口变化不需要重排版面，只有文档或配置变了才调 invalidate_cache。
    """

    def __init__(self, document: ParsedDocument) -> None:
        """持有文档并置空版面缓存。

        Args:
            document: 解析产物；其 blocks 会被就地回填 bbox（文档对象被修改）。
        """
        self.document = document
        self._layout: ReflowLayout | None = None
        self._font: Any = None
        # 回填 bbox 后重新注册文本块（命中/段落聚合依赖新 bbox）
        self._mapper: TextMapper | None = None

    def set_mapper(self, mapper: TextMapper | None) -> None:
        """挂载映射器，排版完成后立刻用它重注册坐标。

        Args:
            mapper: TextMapper 实例；传 None 表示只在纯渲染场景下使用。
        """
        self._mapper = mapper

    # ---- 版面 ------------------------------------------------------------
    def _ensure_layout(self) -> ReflowLayout:
        """惰性排版并返回结果；已排过直接用缓存。

        Returns:
            ReflowLayout，同时已完成 block bbox 回填与（若有）映射器重注册。

        Notes:
            排版是唯一一处副作用较大的操作：它会改写 document.meta 的 layout_pending，
            因此必须排在 BST mapper 注册之前，否则映射器拿到的是零坐标。
        """
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
            # 提示块整段一个框、原本就在它的 text 里，走 _fill_blocks；其余段读取并集文本
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
            # 末段后不再追加 _PARA_GAP，减去即真实内容高；再取下限避免短文档贴顶
            height=max(y - _PARA_GAP, _MIN_HEIGHT),
        )
        self.document.meta["layout_pending"] = False
        if self._mapper is not None:
            self._mapper.register_blocks(self.document.blocks)
        return self._layout

    def content_size(self) -> tuple[int, int]:
        """返回版面尺寸 (width, height)，供视图设置滚动条范围。"""
        layout = self._ensure_layout()
        return layout.width, layout.height

    def _backfill_blocks(
        self, blocks: list[TextBlock], text: str, ranges: list[tuple[int, int]], y0: int, fm: Any
    ) -> None:
        """把重排后的 bbox 写回 block：命中和段落并集都读这份坐标。

        每个 block 分到它在段落文本里占的字符区间所覆盖的那几行——
        段内 block 于是天然沿 y 连续，段落并集 bbox 就等于整段的排版范围。

        Args:
            blocks: 同一段落的块，按阅读顺序排列。
            text: 该段拼接后的完整文本，与 ranges 的下标空间一致。
            ranges: 折行结果的字符区间列表。
            y0: 该段起始纵坐标。
            fm: QFontMetrics，用来量实际像素宽。
        """
        _, spans = paragraph_text_with_spans(blocks)
        covered: set[int] = set()
        starts = [a for a, _ in ranges]
        for b, start, end in spans:
            # bisect 找起止字符落在哪一行：比逐行线性扫描少一次全行遍历，
            # 段落折出几十行时的差别才明显
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
        """提示块（扫描页占位）整段一个框。

        Args:
            blocks: 提示块的成员（通常只有一个）。
            lines: 折行后的行文本，用来取最大行宽。
            y0: 该段起始纵坐标。
            fm: QFontMetrics，用来量实际像素宽。
        """
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
        """渲染文档坐标 (x_offset, y_offset) 起、width×height 大的可视区域。

        Args:
            x_offset: 视口左上角的文档横坐标。
            y_offset: 视口左上角的文档纵坐标。
            width: 目标图像宽度（像素）。
            height: 目标图像高度（像素）。

        Returns:
            RGB 三通道 uint8 数组，形状 (height, width, 3)。

        Notes:
            先用 translate 把原点挪到视口位置，之后一律按文档坐标绘制，
            省掉每行的坐标换算。视口外的段落直接跳过。
        """
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
        """文本按宽度折行，返回行文本列表；详见 _wrap_ranges。"""
        return [text[a:b] for a, b in DocumentRenderer._wrap_ranges(fm, text, max_w)]

    @staticmethod
    def _wrap_ranges(fm: Any, text: str, max_w: int) -> list[tuple[int, int]]:
        """按宽度折行，返回每行的字符区间 [start, end)。

        返回区间而不是字符串：渲染要用行文本，回填 bbox 还要知道"每个 block 占了哪几行"。

        Args:
            fm: QFontMetrics，用来量宽度。
            text: 待折行的完整文本。
            max_w: 单行最大像素宽度。

        Returns:
            区间列表，覆盖整段文本；空文本返回 [(0, 0)]。

        Notes:
            超宽 token（长英文串、无标点 CJK 整段）按字符二分求切点——逐字回退在几千字的
            长句上是 O(n²)，会把界面直接卡死；逐句模式下一步就是整个长句，
            比段落模式更容易撞上这种情况。
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
        """QImage → 紧致的 RGB 数组。

        Args:
            qimg: QImage，格式为 RGB888。
            w: 有效宽度（像素）。
            h: 高度（像素）。

        Returns:
            形状 (h, w, 3) 的 uint8 数组副本。

        Notes:
            Qt 每行按 4 字节对齐，bytesPerLine 常大于 w×3；直接 reshape 会错位，
            必须先按字节窗口截掉行尾 padding。copy 是因为 constBits 是缓冲区的视图，
            QImage 析构后就成了野指针。
        """
        bpl = qimg.bytesPerLine()
        buf = qimg.constBits()
        buf.setsize(bpl * h)
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl)
        # 去掉行尾 padding，紧致成 (H, W, 3)
        arr = arr[:, : w * 3].reshape(h, w, 3)
        return arr.copy()

    # ---- 缓存 ------------------------------------------------------------
    def invalidate_cache(self) -> None:
        """丢弃版面缓存；下次取版面会重排并重新回填 bbox。"""
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
    """一屏的排版结果：视口尺寸或首句变了就重算。

    solo 为真表示该句超长到字号触底仍放不下，只能独占一屏并开放内部滚动；
    shrunk 为真表示本屏字号已低于配置值，用于提示或回归排查。
    """

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
        """本屏所有句高之和加上句间空隙：不含 pad_top 的实际占用高度。"""
        return sum(self.heights) + self.gap * max(self.n - 1, 0)


class StepRenderer:
    """逐句步进版面：每屏独立排版，只画能完整显示的 N 句。

    与 DocumentRenderer（整篇连续纵向版面 + 像素滚动）不同，这里没有全局滚动坐标：
    句高、字号、段间距都按当前视口算，因此映射器看到的永远是"屏内坐标"
    （scroll_offset 恒 0），每次换屏把句子级 block 重注册一遍。

    生命周期：units（句子切分）只算一次；screen（本屏排版）按 (宽, 高, 首句下标)
    为键缓存，任一项变化就重算。
    """

    def __init__(self, document: ParsedDocument, reading: ReadingConfig) -> None:
        """持有文档与阅读配置，句子列表与屏幕结果都留到首次使用时再算。

        Args:
            document: 解析产物，句子从它的段落文本里切出来。
            reading: 阅读配置，提供字号、行高倍数、边距、间隙等自适应参数。
        """
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
        """挂载映射器，每次换屏后用它重注册全部句子的屏内坐标。"""
        self._mapper = mapper

    # ---- 句子单位 --------------------------------------------------------
    @property
    def units(self) -> list[StepUnit]:
        """全篇句子列表（惰性构建）。

        切分结果在全篇范围内稳定：index 一旦分配就不会随翻屏变化，
        因此句子既是排版单位，也是翻译缓存键的天然下标。
        """
        if self._units is None:
            self._units = self._build_units()
        return self._units

    def _build_units(self) -> list[StepUnit]:
        """切出全篇句子，每句配一个合成 TextBlock。

        Returns:
            StepUnit 列表，index 在全篇范围内连续。

        Notes:
            每个句子都造一个 source 为 STEP_SOURCE 的合成块：原来的 block 是按行或按
            视觉行切的，一个字也装不下"跨行的一句"。para_key 用 sentence-{index}
            保证唯一，映射器据此做命中、取文本、建翻译缓存键。
            提示块与纯分隔符行直接丢弃——它们不承载可翻译的内容。
        """
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
        """最近一次排版结果；ensure_screen 被调用之前为 None。"""
        return self._screen

    def content_size(self) -> tuple[int, int]:
        """步进模式没有可滚动的长版面：返回视口尺寸本身，滚轮位移被夹成 0。

        Returns:
            (width, height)。未排版时返回 (0, 0)。
        """
        if self._screen_key is None:
            return (0, 0)
        return (self._screen_key[0], self._screen_key[1])

    def ensure_screen(self, width: int, height: int) -> StepScreen:
        """按视口尺寸取本屏排版结果，必要时重算并同步 block 坐标。

        Args:
            width: 视口宽度（像素）。
            height: 视口高度（像素）。

        Returns:
            缓存或新算出的 StepScreen。

        Notes:
            缓存键含 top，换屏时一定会重算；键里存的是 screen.top 而不是请求时的
            _top，因为 _compute_screen 可能为了填满最后一屏反向夹过一次。
        """
        key = (int(width), int(height), self._top)
        if self._screen is not None and self._screen_key == key:
            return self._screen
        screen = self._compute_screen(int(width), int(height))
        self._screen, self._screen_key = screen, (int(width), int(height), screen.top)
        self._sync_blocks(screen)
        return screen

    def _font(self, px: int, bold: bool = False) -> Any:
        """按 (像素字号, 是否加粗) 取字体；同一组合复用实例。

        QFontMetrics 构造代价不低，而折行测量会反复用同一组字体。
        """
        font = self._fonts.get((px, bold))
        if font is None:
            font = _make_font(px, bold)
            self._fonts[(px, bold)] = font
        return font

    def _measure(
        self, content_w: int, font_px: int
    ) -> list[tuple[list[tuple[int, int]], int, float]]:
        """每个句子的折行区间、行数、最大行宽（按内容宽度+字号缓存）。

        标题用粗体量、粗体画：量高与绘制必须同一字体，否则折行差一行就会画出半截。

        Args:
            content_w: 可用内容宽度（像素）。
            font_px: 像素字号。

        Returns:
            与 units 等长的列表，每项为 (折行区间列表, 行数, 最大行宽)。
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
        """算本屏放得下哪几句、用什么字号、句间留多大空隙。

        Args:
            width: 视口宽度（像素）。
            height: 视口高度（像素）。

        Returns:
            StepScreen；无句子时返回 n=0 的空屏。

        Notes:
            外层"两遍"循环是为了处理最后一屏：算出的首句 + n 越过结尾时，把 top 反向
            夹回重算一次，保证最后一屏装满而不是半屏。内层自减字号的循环只在
            一分未放下时触发，触到 min_font_px 就停止并转为独占一屏。
        """
        cfg = self.reading
        units = self.units
        margin_x = max(int(cfg.margin_x_vw * width), cfg.margin_x_min)
        content_w = max(width - 2 * margin_x, 160)
        avail = max(float(height) - 2 * cfg.margin_y, 80.0)
        if not units:
            return StepScreen(
                0, 0, 0.0, 0.0, int(cfg.font_px), 0, content_w, margin_x, avail, [], []
            )

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
            # 余量摊到句间空隙上，但不超过 max_gap_em：间隙过大就不再像"同一屏内容"
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
    def render_viewport(
        self, x_offset: float, y_offset: float, width: int, height: int
    ) -> np.ndarray:
        """画当前这一屏；x/y 偏移按设计恒为 0（无全局滚动坐标）。

        Args:
            x_offset: 恒为 0，保留是为了与 DocumentRenderer 保持签名一致。
            y_offset: 恒为 0，同上。
            width: 视口宽度（像素）。
            height: 视口高度（像素）。

        Returns:
            RGB 三通道 uint8 数组，形状 (height, width, 3)。

        Notes:
            独占一屏时按 _solo_offset 做屏内滚动，其余情况下该偏移恒为 0。
        """
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
                    QRect(
                        screen.margin_x, int(y + li * screen.line_h),
                        screen.content_w, screen.line_h,
                    ),
                    flags,
                    line,
                )
            y += screen.heights[k] + screen.gap
        painter.end()
        return DocumentRenderer._qimage_to_rgb_array(qimg, w, h)

    # ---- 步进 ------------------------------------------------------------
    def step(self, delta: int) -> bool:
        """向下/向上走一句；到顶、到底或超长句独占时返回 False。

        Args:
            delta: >0 向下走一句，否则向上走一句。

        Returns:
            True 表示 top 已推进；False 表示到达边界或当前处于独占屏（兜底 dyPascal
            由 solo_scroll 负责）。
        """
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

        Args:
            dy: 本次滚动位移（像素）。

        Returns:
            True 表示内部偏移有变化；False 表示已到该句的头或尾，调用方应改走 step。
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

        Args:
            screen: 刚算出的本屏排版结果。
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
