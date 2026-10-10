"""模块名称：file_parser.py

功能描述：解析链路的第一环。按扩展名把文件路由到 PDF / TXT / Word 各自的解析器，
产出统一的 ParsedDocument（路径 + 页面尺寸 + 带来源结构的 TextBlock 列表），
再交给 segmenter 归一阅读顺序、由渲染器回填 bbox。

关键约束：
- 不做 OCR，因此图片与扫描件没有文本块：扫描页只把页码记入 meta["needs_ocr_pages"]，
  由 segmenter.make_ocr_notices 补占位提示行，避免整页内容被静默丢弃。
- 除 PDF 外，TXT / Word 在解析期拿不到几何信息，bbox 恒为 0 并置
  meta["layout_pending"]=True，坐标必须等渲染期回填——这是它们能被凝视命中的唯一通路。
- block_id 编码了来源结构（页/块/行/片、行号、段号），segmenter 的阅读顺序归一与
  折行拼接都靠反解它；改格式必须同步改 segmenter 与 sentence_splitter。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from mytraxcure.core.types import TextBlock


@dataclass
class ParsedDocument:
    """一份文件解析后的统一容器：路径、页面信息、全部文本块与元数据。

    blocks 的顺序仍是各格式自己的遍历顺序（PDF 块序 / TXT 行序 / Word 段序），
    跨页与多栏时不保证是阅读顺序，需经 segmenter.segment_blocks 归一后方可渲染。
    """

    path: Path
    kind: str  # PDF / TXT / WORD（解析器写入值）；无 image / ocr 来源
    pages: list[Any] = field(default_factory=list)  # 每页 (宽, 高)；重排渲染后不再参与计算
    blocks: list[TextBlock] = field(default_factory=list)  # 每实例独立列表：默认可变对象会被所有实例共享
    meta: dict[str, Any] = field(default_factory=dict)  # 元数据（如 layout_pending：等待渲染期回填 bbox）

class FileParser:
    """按扩展名把文件路由到 PDF / TXT / Word 解析器，产出统一的 ParsedDocument。

    解析期只负责"取出文字 + 尽量带上坐标"，不做版面还原：坐标缺失的部分一律留给
    渲染器回填，避免在两个地方各维护一套版面规则。
    """

    # 白名单与下方分支一一对应；项目不做 OCR，故图片与扫描件不在此列
    SUPPORTED: ClassVar[set[str]] = {".pdf", ".txt", ".doc", ".docx"}
    # path 允许 str 或 Path，内部统一转成 Path 再取扩展名
    def parse(self, path: str | Path) -> ParsedDocument:
        """按扩展名路由到对应解析器。

        Args:
            path: 文件路径，str 或 Path 均可；扩展名大小写不敏感。

        Returns:
            ParsedDocument，文本块顺序为该格式自己的遍历顺序，尚未归一为阅读顺序。

        Raises:
            ValueError: 扩展名不在 SUPPORTED 白名单内。
        """
        p = Path(path)
        ext = p.suffix.lower()  # 统一转小写，兼容 .PDF / .DOCX 等写法
        # 先校验后路由：白名单是唯一入口，其它扩展名一律在此拦下
        if ext not in self.SUPPORTED:
            raise ValueError(f"不支持的文件类型：{ext}")
        if ext == ".pdf":
            doc = self._parse_pdf(p)
        elif ext == ".txt":
            doc = self._parse_txt(p)
        elif ext in {".doc", ".docx"}:
            doc = self._parse_word(p)
        else:  # 不可达分支：SUPPORTED 已排除其余类型，保留作为白名单漏改时的兜底
            raise ValueError(f"暂不支持的文件类型：{ext}（仅支持 PDF / Word / TXT）")
        return doc

    # ---- 各类型 ----------------------------------------------------------
    def _parse_pdf(self, path: Path) -> ParsedDocument:
        """PyMuPDF 逐 span 建块：一个 span 就是一个 TextBlock。

        span 是字体与字号一致的最小文本片段，直接取它的 bbox 作为命中框比取整行更贴
        合文字实际占位（行的 bbox 会带上行内空白与悬挂间距）。

        Args:
            path: PDF 文件路径。

        Returns:
            ParsedDocument，kind 为 "PDF"，meta 带 page_count 与 needs_ocr_pages。

        Raises:
            ImportError: 未安装 PyMuPDF。

        Notes:
            扫描页取不到文本层，不产出任何文本块，只把页码记入 needs_ocr_pages；
            提示文案在 segmenter.make_ocr_notices 里生成，页码在此仍为 0 基。
        """
        try:
            import fitz
        except ImportError as e:
            # 延迟导入：PyMuPDF 是可选依赖，缺包时给出可执行的安装提示
            raise ImportError ("需要安装 PyMuPDF：pip install PyMuPDF") from e

        doc = fitz.open(path)
        parsed = ParsedDocument(path = path,kind = "PDF") #解析结果容器
        needs_ocr_pages:list[int] = [] #无文本层的页码（0 基）

        # enumerate 同时给出页码与页面对象：页码即 TextBlock.page
        for page_num,page in enumerate(doc):
            rect = page.rect #页面矩形，取宽高供重排前估算版面
            parsed.pages.append((rect.width,rect.height))

            text_dict = page.get_text("dict")#dict 输出带 bbox 的层级结构：block / line / span
            page_has_text = False
            block_idx = 0

            # 缺 blocks 键时退化为空列表：空白页与异常页也能安全跳过
            for block in text_dict.get("blocks",[]):
                if block.get("type") != 0: #0 = 文本块，1 = 图片块（不做 OCR，直接跳过）
                    continue
                block_idx += 1
                for line_idx,line in enumerate(block.get("lines",[])):
                    for span_idx,span in enumerate(line.get("spans",[])):
                        text = span.get("text","").strip()
                        if not text:
                            continue
                        x0,y0,x1,y1 = span["bbox"]#PDF 坐标，原点在页左上角、y 向下增长
                        # block_id 编码 页-块-行-片：segmenter 靠反解它还原文档顺序，
                        # 折行拼接也靠它判断哪些 span 同属一个视觉行
                        parsed.blocks.append(
                            TextBlock(
                                text = text,
                                page = page_num,
                                x = x0,
                                y = y0,
                                w = x1-x0, #宽（x 方向跨度）
                                h = y1-y0, #高（y 方向跨度）
                                block_id = f"p{page_num}-b{block_idx}-l{line_idx}-s{span_idx}",
                                source = "PDF",
                                para_key=f"p{page_num}-b{block_idx}",
                            )
                        )
                        page_has_text = True

            # 无文本层 → 扫描页：本项目不做 OCR，该页无文本块，仅记录页码供提示
            if not page_has_text:
                needs_ocr_pages.append(page_num)

        parsed.meta = {
            "page_count":doc.page_count,
            "needs_ocr_pages":needs_ocr_pages
        }
        doc.close()
        return parsed

    def _parse_txt(self, path: Path) -> ParsedDocument:
        """按行建块：一行 = 一个 TextBlock，空行作为段落分隔。

        Args:
            path: TXT 文件路径。

        Returns:
            ParsedDocument，kind 为 "TXT"，meta 带 line_count、paragraph_count、
            实际命中的 encoding 与 layout_pending=True。

        Raises:
            ValueError: 三种候选编码都解不开该文件。

        Notes:
            编码按 utf-8-sig → utf-8 → gbk 依次试读：utf-8-sig 用来剥掉 BOM，
            gbk 用来覆盖中文 Windows 记事本的历史默认编码。只捕获 UnicodeDecodeError，
            文件不存在等 IO 错误照常上抛。
            纯文本没有版面信息，坐标恒为 0，layout_pending=True 标记 bbox 需由渲染期回填。
        """
        text:str| None = None
        used_encoding = ""
        for enc in ("utf-8-sig","utf-8","gbk"):
            try:
                text = path.read_text(encoding = enc) #任一编码解出即停，记下实际用的编码
                used_encoding = enc
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError(f"无法识别文件编码（已尝试 utf-8/gbk）：{path}")

        parsed = ParsedDocument(path = path , kind = "TXT")
        lines = text.splitlines() #splitlines 一并吃掉 \r\n 与 \r

        paragraph_idx = 0
        for line_idx,raw in enumerate(lines):
            # 空行才递增段号：非空行与它上面的行同段，段号变化即段落边界
            if raw.strip():
                textblock = TextBlock(
                    text = raw,#保留原始行（含行首缩进），仅跳过纯空白行
                    page = 0,
                    x = 0.0,
                    y = 0.0,
                    w = 0.0,
                    h = 0.0,
                    block_id = f"l{line_idx}-para{paragraph_idx}",
                    source = "TXT",
                    para_key=f"para{paragraph_idx}",

                )
                parsed.blocks.append(textblock)
            else:
                paragraph_idx += 1
        parsed.meta = {
            "line_count":len(lines), #总行数（含空行）
            "paragraph_count":paragraph_idx + 1, #段落数：末段后不再有空行，需补 1
            "encoding":used_encoding,#实际解码成功的编码，供排查乱码
            "layout_pending":True #标记：bbox 待渲染层回填
        }
        return parsed

    def _parse_word(self, path: Path) -> ParsedDocument:
        """python-docx 逐段落建块：一个段落对象 = 一个 TextBlock。

        Args:
            path: .doc / .docx 文件路径。

        Returns:
            ParsedDocument，kind 为 "WORD"，meta 带 paragraph_count、engine
            与 layout_pending=True。

        Raises:
            ImportError: 未安装 python-docx。

        Notes:
            python-docx 只处理 .docx 包格式；.doc 虽在 SUPPORTED 白名单里，
            实际会被 python-docx 以"包无法识别"拒绝，属于白名单与能力的已知落差。
            页面尺寸写死 A4 纵向（595×842pt）：python-docx 不给几何信息，这里的
            pages 仅供估算，真实坐标同样靠渲染期回填。
            只遍历 d.paragraphs，表格、文本框、页眉页脚里的文字不在结果中。
        """
        try:
            import docx
        except ImportError as e:
            raise ImportError ("需要安装 python-docx：pip install python-docx") from e

        d = docx.Document(str(path))
        parsed = ParsedDocument(path = path,kind = "WORD")
        parsed.pages.append( (595.0,842.0) )#A4 纵向（pt），仅作版面估算占位

        for para_idx,para in enumerate(d.paragraphs):
            text = para.text.strip()
            if not text:
                continue
            # 段号取原始段落下标，空段被跳过因此编号不连续（不影响唯一性）
            textblock = TextBlock(
                text = text,
                page = 0,
                x = 0.0,
                y = 0.0,
                h = 0.0,
                w = 0.0,
                block_id= f"para{para_idx}",
                source = "WORD",
                para_key=f"para{para_idx}",
            )
            parsed.blocks.append(textblock)

        parsed.meta = {
            "paragraph_count":len(parsed.blocks),
            "layout_pending":True,
            "engine":"python-docx"
        }
        return parsed

    # 图片/扫描件解析器已移除（v6.2 范围裁剪：不支持图片，不做 OCR）
