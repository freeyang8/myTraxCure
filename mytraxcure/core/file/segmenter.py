"""模块名称：segmenter.py

功能描述：把解析产物的 block 序列归一成阅读顺序并完成段落归组，是所有下游 Coordinate
计算的共同前提。在同一个 traversal 里做三件事：按阅读顺序排序、PDF 段落按几何二次切分、
把 para_key 重编为全文档连续的 seg{n}，同时产出段落文本与每块的字符区间。

关键约束：
- 各解析器给出的 block 顺序只保证"各格式自己的遍历顺序"（PDF 是 PyMuPDF 块序、
  TXT 是行序、Word 是段落序），跨页/多栏时并不保证是阅读顺序。重排渲染器直接信任
  本模块的输出顺序，排序规则的任何改动都会同时影响排版、命中与预翻译排队。
- 注意区分两种"排序"入口：本模块的 segment_blocks 是按几何排序（用于实际排版），
  TextMapper 侧另有纯 block_id 的兜底排序；两者规则不同，不能互相替换。
- TXT（空行分段）与 Word（段落对象）的段落粒度本来就是对的，不做二次切分；
  只有 PDF 会因为 PyMuPDF 把"标题 + 正文"并进同一个 block 而需要按几何再切。
"""

from __future__ import annotations

import re

from mytraxcure.core.types import TextBlock

NOTICE_SOURCE = "notice"  # 扫描页占位提示块：只渲染，不参与翻译
REFLOW_PAGE = 0           # 重排后整篇是一条连续版面，所有 block 同属这一页


def _doc_order(b: TextBlock) -> tuple[int, ...]:
    """block_id 里编码的原始文档顺序（块自身的结构位置）。

    PDF: p{页}-b{块}-l{行}-s{片} → (页, 块, 行, 片)
    TXT: l{行}-para{段}          → (行, 段)
    Word / 提示块: para{段} / notice-p{页} → (段,) / (页,)
    """
    if not b.block_id:
        return ()
    nums = re.findall(r"\d+", b.block_id)
    return tuple(int(n) for n in nums)


def _order_key(b: TextBlock, index: int) -> tuple:
    """阅读顺序排序键——三种格式走同一条规则。

    有原始坐标的（PDF）用 (page, y, x)，(y,x) 兜底，不做多栏检测；
    没有坐标的（TXT/Word 解析期坐标恒为 0）用 block_id 里的行号/段号定序，
    这样它们的顺序由 block 自身的结构决定，而不是"恰好等于解析器给的列表顺序"。
    index 兜底保证同键时顺序确定。
    """
    return (b.page, b.y, b.x, _doc_order(b), index)


def segment_blocks(blocks: list[TextBlock]) -> list[TextBlock]:
    """按阅读顺序排序、切分段落并重编 para_key，返回新的 block 顺序。

    PDF / TXT / Word 三种格式都走这里：先按 _order_key 排成阅读顺序（有坐标用坐标、
    没坐标用 block_id 编码的文档顺序），再把段落连续化并重编 seg{n}。
    """
    ordered = [
        b for _, b in sorted(enumerate(blocks), key=lambda pair: _order_key(pair[1], pair[0]))
    ]
    result: list[TextBlock] = []
    for index, (_key, members) in enumerate(group_paragraphs(ordered)):
        for b in members:
            b.para_key = f"seg{index}"
            b.page = REFLOW_PAGE
            result.append(b)
    return result


def group_paragraphs(blocks: list[TextBlock]) -> list[tuple[str, list[TextBlock]]]:
    """把已按阅读顺序排好的 block 归成段落，返回 (para_key, 段内 blocks)。

    Args:
        blocks: 已排好阅读顺序的文本块。

    Returns:
        段落列表，顺序与输入一致。para_key 取自每段首块，段 boundaries 由
        _same_paragraph 决定。

    Notes:
        同一段的 block 一定相邻——重排渲染是按段连续排版的，段内被别段插开就会
        渲染成两截，段落并集 bbox 也会横跨别的段。
    """
    groups: list[tuple[str, list[TextBlock]]] = []
    for b in blocks:
        if groups and _same_paragraph(groups[-1][1][-1], b):
            groups[-1][1].append(b)
        else:
            groups.append((b.para_key, [b]))
    return groups


def _same_paragraph(prev: TextBlock, cur: TextBlock) -> bool:
    """判断相邻两块是否仍属同一段落。

    Args:
        prev: 阅读顺序上的前一块。
        cur: 紧跟其后的一块。

    Returns:
        True 表示两块归为同一段。

    Notes:
        仅对 PDF 做二次切分。判定用的是行∈块的纵向重叠与行间距两条几何规则，
        阈值取"半个行高"与"一个行高"的原文行高比例而非绝对值：同一份 PDF 里标题
        与正文的行高能差一倍，用绝对像素会在任一字号上失效。
        坐标尚未回填（h ≤ 0）时保持解析器的判定，避免拿到假换行。
    """
    if prev.para_key != cur.para_key:
        return False  # 解析器已判定换段
    if prev.source != "PDF" or cur.source != "PDF":
        return True  # TXT / Word 的段落粒度本来就对，不再二次切分
    if prev.h <= 0 or cur.h <= 0:
        return True  # 缺几何信息（尚未回填），保持解析器判定

    if cur.y < prev.y + prev.h * 0.5:
        return True  # 与上一块基本同处一行（字体变化切出的 span）→ 同段
    gap = cur.y - (prev.y + prev.h)
    # 行间距达到一个行高（用原文自身的行高，不同字号文档自动缩放）→ 视为换段
    return gap < prev.h


# ---- 段落文本（翻译与渲染的唯一出处）--------------------------------------
def line_id(b: TextBlock, index: int) -> str:
    """block 在原始版式里的"视觉行"标识。

    block_id 编码了来源结构：PDF 是 p{页}-b{块}-l{行}-s{片}，TXT 是 l{行}-para{段}，
    Word 是 para{段}。同一行的多个 span 归到同一 id，拼接时不加空格；
    换行才补空格，避免把 "Hel" "lo" 拼成 "Hel lo"。
    """
    bid = b.block_id
    if not bid:
        return f"#anon{index}"  # 没有 id 的块各自成行
    if "-l" in bid and "-s" in bid:
        return bid.split("-s", 1)[0]
    if bid.startswith("l") and "-" in bid:
        return bid.split("-", 1)[0]
    return bid


def paragraph_text_with_spans(
    blocks: list[TextBlock],
) -> tuple[str, list[tuple[TextBlock, int, int]]]:
    """段落文本 + 每个 block 在文本中的字符区间（渲染回填 bbox 用）。

    跳过提示块与空白块：它们不参与翻译，也不该让段落文本多出内容。
    """
    parts: list[str] = []
    spans: list[tuple[TextBlock, int, int]] = []
    pos = 0
    prev_line: str | None = None
    for index, b in enumerate(blocks):
        # 提示块与空白块不参与翻译，也不该让段落文本多出内容
        if b.source == NOTICE_SOURCE or not b.text.strip():
            continue
        lid = line_id(b, index)
        # 只有真的换视觉行才补空格：同一行的多个 span 直接相连，
        # 否则 "Hel" + "lo" 会被拼成 "Hel lo"
        if parts and lid != prev_line:
            parts.append(" ")
            pos += 1
        start = pos
        parts.append(b.text)
        pos += len(b.text)
        spans.append((b, start, pos))
        prev_line = lid
    return "".join(parts), spans


def paragraph_text(blocks: list[TextBlock]) -> str:
    """段落文本（预翻译与即时翻译共用的唯一出处）。"""
    return paragraph_text_with_spans(blocks)[0]


def make_ocr_notices(needs_ocr_pages: list[int]) -> list[TextBlock]:
    """无文本层（扫描页）的占位提示块：重排视图里照样占一行，避免静默丢内容。

    Args:
        needs_ocr_pages: 无文本层的页码（0 基），来自 ParsedDocument.meta。
            重复项会被去重，页码在文案里再转回 1 基展示。

    Returns:
        每个扫描页一个 TextBlock，source 为 NOTICE_SOURCE，按页码升序。

    Notes:
        提示块的 text 是它自己的整句，因此取 current_text 时用的是 blocks[0].text
        而不是拼接结果——见 file_viewer 里 _fill_blocks 的分支。
    """
    return [
        TextBlock(
            text=f"【第 {int(p) + 1} 页为扫描页：无文本层，无法翻译】",
            page=int(p),
            x=0.0,
            y=0.0,
            w=0.0,
            h=0.0,
            block_id=f"notice-p{int(p)}",
            source=NOTICE_SOURCE,
            para_key=f"notice-p{int(p)}",
        )
        for p in sorted(set(needs_ocr_pages))
    ]
