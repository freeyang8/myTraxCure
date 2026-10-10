"""模块名称：test_file_parser_pdf.py

功能描述：拿一份真实 PDF 跑一轮 FileParser._parse_pdf，把每页的文本块连同坐标打印出来，
并对"容易静默出错"的产出做核对：坐标是否落在页面范围内、block_id/para_key 是否为空、
source 是否为 PDF、文档是否既没文本块也没被标成扫描页。

关键约束：
- PDF_PATH 写死的是本机路径，换环境必须改这里或改用命令行传参（传参优先）。
- 这是数据驱动的校验而非纯粹单元测试：结论依赖被解析的那份 PDF，
  换一份文件跑通不代表另一份也通。
- tests 目录被 .gitignore 忽略，本地专属脚本，不进版本库。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 上，使 `mytraxcure` 可导入（无论从哪运行）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mytraxcure.core.file.file_parser import FileParser, ParsedDocument

# ---- 在这里填你本地的 PDF 路径（命令行传参时以传参为准）----
PDF_PATH = Path(r"E:\Download\IDMDownloads\fiz_test_pdf.pdf")

# 每页最多打印多少个文本块，避免大 PDF 刷屏（0 表示全部打印）
MAX_BLOCKS_PER_PAGE = 20

def parse_pdf_and_report(pdf_path: Path) -> tuple[ParsedDocument, list[str]]:
    """解析一份 PDF 并收集全部可疑之处。

    Args:
        pdf_path: PDF 文件路径。

    Returns:
        (解析结果, 问题描述列表)；列表为空即未发现问题。
    """
    problems: list[str] = []

    doc = FileParser()._parse_pdf(pdf_path)

    # ---- 基本字段 ----
    page_count = doc.meta.get("page_count")
    if page_count != len(doc.pages):
        problems.append(f"page_count({page_count}) 与 pages 尺寸数({len(doc.pages)}) 不一致")
    if not doc.blocks and not doc.meta.get("needs_ocr_pages"):
        problems.append("整个文档既没有文本块，也没有标记任何扫描页")

    # ---- 打印每页文本块 ----
    for page_num in range(page_count or 0):
        page_blocks = [b for b in doc.blocks if b.page == page_num]
        w, h = doc.pages[page_num]
        ocr_tag = "  <-- 扫描页(needs_ocr)" if page_num in doc.meta.get("needs_ocr_pages", []) else ""
        print(f"-- 第 {page_num} 页 ({w:.0f}x{h:.0f}pt) 共 {len(page_blocks)} 个文本块{ocr_tag} --")

        limit = MAX_BLOCKS_PER_PAGE or len(page_blocks)
        for b in page_blocks[:limit]:
            print(f"   ({b.x:.1f},{b.y:.1f}) {b.w:.1f}x{b.h:.1f}  {b.text!r}")
        if len(page_blocks) > limit:
            print(f"   ... 省略 {len(page_blocks) - limit} 个块")

        # ---- 坐标合理性（仅对有文本块的页检查）----
        for b in page_blocks:
            if b.w <= 0 or b.h <= 0:
                problems.append(f"p{page_num} 文本块尺寸异常: w={b.w}, h={b.h}（{b.text!r}）")
            if b.x < 0 or b.y < 0 or b.x + b.w > w + 1 or b.y + b.h > h + 1:
                problems.append(f"p{page_num} bbox 超出页面范围: ({b.x:.1f},{b.y:.1f}) {b.w:.1f}x{b.h:.1f}（{b.text!r}）")
            if not b.block_id:
                problems.append(f"p{page_num} block_id 为空（{b.text!r}）")
            if not b.para_key:
                problems.append(f"p{page_num} para_key 为空（{b.text!r}）——段落聚合会退化成单块")
            if b.source != "PDF":
                problems.append(f"p{page_num} source 应为 PDF，实际 {b.source!r}")

    # ---- contains() 命中测试（取第一个块，模拟视线落在文本中心）----
    if doc.blocks:
        first = doc.blocks[0]
        cx, cy = first.x + first.w / 2, first.y + first.h / 2
        if not first.contains(cx, cy):
            problems.append(f"视线中心点 ({cx:.0f},{cy:.0f}) 未命中 TextBlock（{first.text!r}）")

    print()
    print(f"pages 尺寸: {doc.pages}")
    print(f"meta: {doc.meta}")
    print(f"总文本块数: {len(doc.blocks)}")
    return doc, problems

def test_parse_pdf() -> None:
    """pytest 入口：默认 PDF 必须存在且解析无问题；问题描述会作为断言消息整体抛出。"""
    assert PDF_PATH.exists(), f"PDF 不存在：{PDF_PATH}"
    _, problems = parse_pdf_and_report(PDF_PATH)
    assert not problems, "\n".join(problems)

if __name__ == "__main__":
    # 控制台可能是 GBK，强制 UTF-8 输出，避免打印中文时崩
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - 控制台不支持 UTF-8 时静默跳过
        pass

    # 命令行传参优先，否则用顶部 PDF_PATH 常量
    if len(sys.argv) > 1:
        pdf_path = Path(sys.argv[1])
    else:
        pdf_path = PDF_PATH

    if not pdf_path.exists():
        print(f"FAIL: PDF 文件不存在 -> {pdf_path}")
        print("用法: python tests/test_file_parser_pdf.py <本地PDF路径>，或修改脚本顶部 PDF_PATH")
        sys.exit(1)
    if pdf_path.suffix.lower() != ".pdf":
        print(f"FAIL: 不是 .pdf 文件 -> {pdf_path}")
        sys.exit(1)

    print(f"解析文件: {pdf_path}\n")
    _, problems = parse_pdf_and_report(pdf_path)

    print()
    if problems:
        print("发现问题:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("PASS")
