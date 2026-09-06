from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from mytraxcure.core.types import TextBlock


@dataclass
class ParsedDocument: #解析后的文档结构

    path: Path
    kind: str  # pdf / txt / word（不支持 image / 扫描件）
    pages: list[Any] = field(default_factory=list)  # 每页渲染对象/图像
    blocks: list[TextBlock] = field(default_factory=list)#每次创建实例时创建一个空列表，避免多个实例共享同一个列表
    sensitive: bool = False
    meta: dict[str, Any] = field(default_factory=dict) # 元数据（如 layout_pending：等待渲染期回填 bbox）

class FileParser: #根据文件扩展名调用对应的解析逻辑

    # v6.2：不做 OCR → 不支持图片/扫描件，仅文本型文档
    SUPPORTED: ClassVar[set[str]] = {".pdf", ".txt", ".doc", ".docx"}
    #表示path的类型可以是str或Path对象
    def parse(self, path: str | Path, sensitive: bool = False) -> ParsedDocument:
        p = Path(path)
        ext = p.suffix.lower()  #suffix->获取扩展名
        #检查后缀是否在允许范围内，并路由到不同解析器
        if ext not in self.SUPPORTED: 
            raise ValueError(f"不支持的文件类型：{ext}")
        if ext == ".pdf":
            doc = self._parse_pdf(p)
        elif ext == ".txt":
            doc = self._parse_txt(p)
        elif ext in {".doc", ".docx"}:
            doc = self._parse_word(p)
        else:  # 不可达：SUPPORTED 已排除图片等不支持类型
            raise ValueError(f"暂不支持的文件类型：{ext}（仅支持 PDF / Word / TXT）")
        doc.sensitive = sensitive
        return doc

    # ---- 各类型 ----------------------------------------------------------
    def _parse_pdf(self, path: Path) -> ParsedDocument:

        try:
            import fitz
        except ImportError as e:
            raise ImportError ("需要安装 PyMuPDF：pip install PyMuPDF") from e

        doc = fitz.open(path)
        parsed = ParsedDocument(path = path,kind = "PDF") #存储解析的PDF
        needs_ocr_pages:list[int] = [] #页数索引

        #同时返回元素的“索引”和“元素本身”
        for page_num,page in enumerate(doc):
            rect = page.rect #获取当前页面的矩形对象,得到页面的宽和高
            parsed.pages.append((rect.width,rect.height))

            text_dict = page.get_text("dict")#"dict" ->以字典格式提取文本
            page_has_text = False
            block_idx = 0
            
            #提取blocks键对应的值，如果没有这个键就返回[]
            for block in text_dict.get("blocks",[]):
                if block.get("type") != 0: #0 代表是文本块，1 代表图片块
                    continue
                block_idx += 1
                for line_idx,line in enumerate(block.get("lines",[])):
                    for span_idx,span in enumerate(line.get("spans",[])):
                        text = span.get("text","").strip()
                        if not text:
                            continue
                        x0,y0,x1,y1 = span["bbox"]#取出该文字片段的边界框坐标
                        parsed.blocks.append(
                            TextBlock(
                                text = text,
                                page = page_num,
                                x = x0,
                                y = y0,
                                w = x1-x0, #高
                                h = y1-y0, #宽
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
        text:str| None = None
        used_encoding = ""
        for enc in ("utf-8-sig","utf-8","gbk"):
            try:
                text = path.read_text(encoding = enc) #尝试不同的编码格式读取文件
                used_encoding = enc
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError(f"无法识别文件编码（已尝试 utf-8/gbk）：{path}")

        parsed = ParsedDocument(path = path , kind = "TXT")
        lines = text.splitlines() #按行切分得到列表

        paragraph_idx = 0
        for line_idx,raw in enumerate(lines):
            if raw.strip(): #去掉这段文本首尾的空格、换行符、制表符后判断是否还有字符
                textblock = TextBlock(
                    text = raw,
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
            "line_count":len(lines), #总行数
            "paragraph_count":paragraph_idx + 1, #段落数量
            "encoding":used_encoding,
            "layout_pending":True #标记：bbox 待渲染层回填
        }
        return parsed
        
    def _parse_word(self, path: Path) -> ParsedDocument:
        try:
            import docx
        except ImportError as e:
            raise ImportError ("需要安装 python-docx：pip install python-docx") from e
        
        d = docx.Document(str(path))
        parsed = ParsedDocument(path = path,kind = "WORD")
        parsed.pages.append( (595.0,842.0) )

        for para_idx,para in enumerate(d.paragraphs):
            text = para.text.strip()
            if not text:
                continue
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
