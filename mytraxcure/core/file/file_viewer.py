from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import numpy as np

from mytraxcure.core.file.file_parser import ParsedDocument

if TYPE_CHECKING:
    from mytraxcure.core.file.text_mapper import TextMapper

# TXT 排版基准参数（文档坐标，1.0 基准；不随 zoom 变化）
# 大字号 + 大行高：文本块 bbox 更大，凝视命中/段落选中概率显著提高（眼动输入精度有限）
_TXT_LINE_HEIGHT = 34      # 行高（像素，需 ≥ 字号 × 1.5，否则行重叠）
_TXT_MARGIN = 40           # 左右边距（像素）
_TXT_FONT_SIZE = 20        # 字号（磅）
_TXT_MIN_PAGE_W = 640      # 最小页面宽度（像素）
_TXT_MIN_PAGE_H = 400      # 最小页面高度（像素）
_TXT_WRAP_WIDTH = 960      # 内容最大宽度（像素，1.0 基准；超长行折行）

class DocumentRenderer:

    def __init__(self, document: ParsedDocument) -> None:
        self.document = document
        self.zoom = 1.0  # 默认缩放比例
        self._page_cache: dict[tuple[int, float], Any] = {}
        self._fitz_doc: Any = None
        # TXT/Word 回填 bbox 后用于重新注册文本块（需求 6.8.8 前置）
        self._mapper: TextMapper | None = None

    def set_mapper(self, mapper: TextMapper | None) -> None:
        self._mapper = mapper

    # 获取当前文档的总页数
    def page_count(self) -> int:
        return len(self.document.pages)

    # ---- PDF ------------------------------------------------------------
    def _render_pdf(self, page_index: int, zoom: float) -> np.ndarray:
        if self._fitz_doc is None:
            import fitz
            self._fitz_doc = fitz.open(self.document.path)

        page = self._fitz_doc[page_index]  # 根据页码取出页面对象

        pix = page.get_pixmap(  # 获取图像的截图
            matrix=fitz.Matrix(zoom, zoom),  # x 和 y 轴各自缩放
            alpha=False,  # 是否生成透明通道 A
        )

        img = np.frombuffer(  # 把“字节流”变成“数组”
            pix.samples,
            dtype=np.uint8,
        ).reshape(
            pix.height,
            pix.width,
            pix.n,
        )
        return img.copy()

    # ---- TXT ------------------------------------------------------------
    def _render_text_page(self, page_index: int, zoom: float) -> np.ndarray:
        from PyQt6.QtCore import QRect, Qt
        from PyQt6.QtGui import QFont, QFontMetrics, QImage, QPainter

        blocks = [b for b in self.document.blocks if b.page == page_index]
        need_backfill = bool(self.document.meta.get("layout_pending", False))

        # 基准字体 + metrics（1.0）
        font = QFont()
        font.setFamilies(["Microsoft YaHei", "SimSun", "Arial"])
        font.setPointSize(_TXT_FONT_SIZE)
        fm = QFontMetrics(font)

        # 布局：把每个 block 按 _TXT_WRAP_WIDTH 折成视觉行，连续推进行游标；
        # 相邻 block 行号差 > 1 时补空行位（原文空行无 block 但要留间距）
        line_of_block = [self._extract_line_idx(b.block_id) for b in blocks]
        layout: list[tuple] = []  # (block, start_row, wrapped_lines)
        row = 0
        prev_li = -1
        for b, li in zip(blocks, line_of_block):
            if layout and li > prev_li + 1:
                row += li - prev_li - 1  # 空行占位
            wrapped = self._wrap_text(fm, b.text, _TXT_WRAP_WIDTH)
            layout.append((b, row, wrapped))
            row += len(wrapped)
            prev_li = li

        # 页面基准尺寸（1.0）：宽度固定（超长行已折行），高度按折行后总行数
        page_w_base = max(_TXT_WRAP_WIDTH + _TXT_MARGIN * 2, _TXT_MIN_PAGE_W)
        page_h_base = max(row * _TXT_LINE_HEIGHT, _TXT_MIN_PAGE_H)

        # 按 zoom 缩放绘制到白底 QImage
        img_w = round(page_w_base * zoom)
        img_h = round(page_h_base * zoom)
        qimg = QImage(img_w, img_h, QImage.Format.Format_RGB888)
        qimg.fill(Qt.GlobalColor.white)

        painter = QPainter(qimg)
        draw_font = QFont(font)
        draw_font.setPointSizeF(_TXT_FONT_SIZE * zoom)
        painter.setFont(draw_font)
        painter.setPen(Qt.GlobalColor.black)

        for b, start_row, wrapped in layout:
            # 画图坐标（按 zoom）：行位 × 行高 × zoom，逐视觉行绘制
            for k, line in enumerate(wrapped):
                px = _TXT_MARGIN * zoom
                py = (start_row + k) * _TXT_LINE_HEIGHT * zoom
                painter.drawText(
                    QRect(
                        int(px),
                        int(py),
                        int(_TXT_WRAP_WIDTH * zoom),
                        int(_TXT_LINE_HEIGHT * zoom),
                    ),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    line,
                )
            # 回填文档坐标（1.0 基准，与 zoom 无关）：bbox 覆盖整个折行段
            if need_backfill:
                b.x = float(_TXT_MARGIN)
                b.y = float(start_row * _TXT_LINE_HEIGHT)
                b.w = float(max(fm.horizontalAdvance(l) for l in wrapped))
                b.h = float(len(wrapped) * _TXT_LINE_HEIGHT)

        painter.end()

        # 回填完成 → 清标志 + 重新注册（段落模式对 TXT 生效的唯一前置）
        if need_backfill:
            self.document.meta["layout_pending"] = False
            if self._mapper is not None:
                self._mapper.register_blocks(self.document.blocks)

        return self._qimage_to_rgb_array(qimg, img_w, img_h)

    # ---- 工具 -----------------------------------------------------------
    @staticmethod
    def _extract_line_idx(block_id: str) -> int:
        if not block_id or not block_id.startswith("l"):
            return 0
        try:
            return int(block_id[1:].split("-", 1)[0])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _wrap_text(fm: Any, text: str, max_w: int) -> list[str]:
        if not text:
            return [""]
        lines: list[str] = []
        cur = ""
        cur_w = 0
        for token in re.findall(r"\S+\s*", text):
            tw = fm.horizontalAdvance(token)
            # token 自身超宽（长英文串 / 无空格 CJK 整段）→ 按字符切
            while tw > max_w and len(token) > 1:
                k = len(token) - 1
                while k > 1 and fm.horizontalAdvance(token[:k]) > max_w:
                    k -= 1
                head, token = token[:k], token[k:]
                if cur:
                    lines.append(cur)
                    cur, cur_w = "", 0
                lines.append(head)
                tw = fm.horizontalAdvance(token)
            if cur and cur_w + tw > max_w:
                lines.append(cur)
                cur, cur_w = token, tw
            else:
                cur += token
                cur_w += tw
        lines.append(cur)
        return lines

    @staticmethod
    def _qimage_to_rgb_array(qimg: Any, w: int, h: int) -> np.ndarray:
        bpl = qimg.bytesPerLine()
        buf = qimg.constBits()
        buf.setsize(bpl * h)
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl)
        # 去掉行尾 padding，紧致成 (H, W, 3)
        arr = arr[:, : w * 3].reshape(h, w, 3)
        return arr.copy()

    # ---- 对外 -----------------------------------------------------------
    def render_page(self, page_index: int, zoom: float | None = None) -> np.ndarray:
        if zoom is None:
            zoom = self.zoom
        key = (page_index, round(zoom, 2))  # round->保留小数位数
        if key in self._page_cache:
            return self._page_cache[key]

        kind = self.document.kind.upper()
        if kind == "PDF":
            img = self._render_pdf(page_index, zoom)
        elif kind == "TXT":
            img = self._render_text_page(page_index, zoom)
        else:
            raise ValueError(f"暂不支持渲染 {kind} 类型（本项目仅支持 PDF / Word / TXT）")
        self._page_cache[key] = img
        return img

    def set_zoom(self, zoom: float) -> None:
        self.zoom = zoom

    def invalidate_cache(self) -> None:
        self._page_cache.clear()
