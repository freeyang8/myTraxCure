"""file 包：文档的解析、切分、重排渲染与坐标映射。

对外暴露 FileParser / ParsedDocument（按扩展名解析出带坐标的 TextBlock）、
DocumentRenderer（全类型统一重排渲染）与 TextMapper（屏幕↔文档坐标、命中、段落聚合）。
句子切分与段落归组属包内环节，需从 sentence_splitter / segmenter 子模块直接引用。
"""

from mytraxcure.core.file.file_parser import FileParser, ParsedDocument
from mytraxcure.core.file.file_viewer import DocumentRenderer
from mytraxcure.core.file.text_mapper import TextMapper

__all__ = [
    "DocumentRenderer",
    "FileParser",
    "ParsedDocument",
    "TextMapper",
]
