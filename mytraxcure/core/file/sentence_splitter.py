"""模块名称：sentence_splitter.py

功能描述：把文档切成"一句一段"的显示单位，供逐句步进阅读模式使用。切分顺序与重排渲染
保持一致：先按段落（`para_key` 粒度）分，再在段内按句末标点切，输出带字符区间的
Sentence 列表。

关键约束：
- 先分段是必要的——`## Introduction` 这类标题没有句末标点，不先分段就会跟下一段
  正文粘成一句。
- 切出来的字符区间是相对于段落文本的偏移量，不是全文偏移。回填坐标时要用它去
  段落文本的坐标系里定位，直接当全文下标用会错。
- 规则来自原型 tools/step_reader_prototype.html（浏览器里逐条实测过），要点：

  1. 终止符：中文 `。！？…` 一律成立；英文 `.` 需要过反例保护，避免在
     小数（3.14）、缩写（Mr. / U.S. / e.g.）、单字母缩写（J. R. R.）处误切；
  2. 连续终止符（`！！`、`?!`、`。。。`、`...`）算一个切点；
  3. 终止符后紧跟的收尾符（`"` `”` `）` `]`）归上一句，不另起一句。
"""

from __future__ import annotations

from dataclasses import dataclass

CLOSERS = "”’\"'）)】]》」』〕｝"
CJK_STOP = "。！？…"
EN_STOP = ".!?"
HEADING_PREFIX = "#"

# 英文句点后不换句的缩写词（去点、小写后比对）
ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "fig", "no",
    "inc", "ltd", "co", "corp", "eg", "ie", "al", "vol", "pp", "ed", "eds",
    "approx", "dept", "univ", "gov", "sen", "rep", "gen", "col", "capt", "lt", "sgt",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "am", "pm", "us", "uk", "eu", "un",
}


@dataclass(frozen=True)
class Sentence:
    """切出的一句话及其在所属段落文本中的字符区间。

    start / end 是相对段落文本的半开区间 [start, end)，供渲染期把句子位置回填到
    TextBlock 的 bbox 上；para_index 指向该句所属段落。

    冻结实例：句子会被多处持有（步进状态、翻译队列、缓存键），可变字段会让
    "同一句两处内容不同"成为可能。
    """

    text: str
    start: int
    end: int
    para_index: int
    heading: bool = False


def is_heading(paragraph: str) -> bool:
    """是否为标题段：以 `#` 开头（Markdown 风格），显示时去掉井号并加粗。"""
    return paragraph.lstrip().startswith(HEADING_PREFIX)


def is_separator(line: str) -> bool:
    """是否为纯分隔符行（`---` / `***` 之类）：不承载文字，逐句模式下整段丢弃。"""
    return len(line.strip()) >= 2 and all(c in "-—*_=~ \t" for c in line)


def strip_heading(paragraph: str) -> str:
    """去掉标题段的井号与两侧的空白；非标题原样返回。"""
    return paragraph.lstrip().lstrip(HEADING_PREFIX).strip() if is_heading(paragraph) else paragraph


def split_sentences(paragraph: str, para_index: int = 0, heading: bool = False) -> list[Sentence]:
    """把一段文本切成句子；纯空白片段丢弃，终止符保留在句尾。

    Args:
        paragraph: 单个段落的文本（不含跨段内容）。
        para_index: 该段在全篇中的下标，原样写进每个 Sentence。
        heading: 该段是否为标题，决定 Sentence.heading。

    Returns:
        Sentence 列表，按出现顺序排列；文本为纯空白时返回空列表。
    """
    out: list[Sentence] = []
    start = 0
    i = 0
    n = len(paragraph)
    while i < n:
        ch = paragraph[i]
        if ch not in CJK_STOP and ch not in EN_STOP:
            i += 1
            continue
        if not _stop_allowed(paragraph, i):
            i += 1
            continue
        j = _terminator_run_end(paragraph, i)
        while j < n and paragraph[j] in CLOSERS:
            j += 1
        _emit(paragraph, start, j, para_index, heading, out)
        start = i = j
    if start < n:
        _emit(paragraph, start, n, para_index, heading, out)
    return out


def split_document(text: str) -> list[Sentence]:
    """把整篇纯文本切句：空行分段，段内单换行合并（英文 txt 常见硬折行）。

    Args:
        text: 整篇纯文本。

    Returns:
        全篇的 Sentence 列表，para_index 为段落下标。
    """
    out: list[Sentence] = []
    for index, paragraph in enumerate(split_paragraphs(text)):
        heading = is_heading(paragraph)
        body = strip_heading(paragraph) if heading else paragraph
        out.extend(split_sentences(body, index, heading))
    return out


def split_paragraphs(text: str) -> list[str]:
    """空行分段；`#` 开头的行独立成段；段内软换行拼成一行。

    Args:
        text: 原始整篇文本，行尾 \r\n / \r / \n 均兼容。

    Returns:
        段落文本列表，每段内部已把软换行折成单行。
    """
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        # 先按空格把软换行接成一行，再用 split/join 折叠连续空白：
        # 硬折行留下的多个空格若不折叠，会让段落文本与所见不一致
        joined = " ".join(" ".join(buf).split())
        if joined:
            out.append(joined)
        buf.clear()

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith(HEADING_PREFIX) or is_separator(line):
            flush()
            out.append(line)
        else:
            buf.append(line)
    flush()
    return out


def _emit(
    paragraph: str, start: int, end: int, para_index: int, heading: bool, out: list[Sentence]
) -> None:
    """把一个字符区间追加为一句话；纯空白或空区间直接丢弃。

    区间先剥掉前后空白再记录，因此 start/end 指向的是有效文本本身，
    回填坐标时不必再处理缩进。
    """
    if end <= start:
        return
    seg = paragraph[start:end]
    lead = len(seg) - len(seg.lstrip())
    text = seg.strip()
    if text:
        out.append(
            Sentence(text=text, start=start + lead, end=start + lead + len(text),
                     para_index=para_index, heading=heading)
        )


def _terminator_run_end(text: str, i: int) -> int:
    """从 i 起算连续终止符的结束位置（不含）；支持中英文终止符混排。"""
    j = i + 1
    while j < len(text) and (text[j] in CJK_STOP or text[j] in EN_STOP):
        j += 1
    return j


def _stop_allowed(text: str, i: int) -> bool:
    """该位置的终止符是否真的断句（中文终止符恒真，英文句点走反例保护）。

    反例按顺序排除：小数 3.14 → 省略号 run → 版本号 v1.2 → 缩写表 → 单字母缩写
    → 句点后紧跟小写字母/数字（更像编号或缩写的延续）。缩写比对前先去点转小写，
    所以 U.S. 与 us 都能命中同一条目。
    """
    ch = text[i]
    if ch in CJK_STOP or ch != ".":
        return True

    prev = text[i - 1] if i else ""
    nxt = text[i + 1] if i + 1 < len(text) else ""

    if _is_digit(prev) and _is_digit(nxt):
        return False                      # 小数 3.14
    if nxt == ".":
        return False                      # 省略号 run，交给 run 结束处理
    if _is_digit(nxt):
        return False                      # 版本号 v1.2

    # 缩写：`.` 前连续 ASCII 字母/点组成的 token，去点后查表
    s = i
    while s > 0 and (_is_alpha(text[s - 1]) or text[s - 1] == "."):
        s -= 1
    token = text[s:i]
    if token:
        key = token.lower().replace(".", "")
        if key in ABBREV:
            return False                  # Mr. / U.S. / e.g.
        if len(key) == 1 and _is_alpha(key):
            return False                  # 单字母缩写 J. R. R.

    # 句点后是小写字母/数字 → 更像编号或缩写的延续，不断句
    j = i + 1
    while j < len(text) and text[j] in " \t":
        j += 1
    return not (j < len(text) and (_is_lower(text[j]) or _is_digit(text[j])))


def _is_alpha(c: str) -> bool:
    """是否为 ASCII 字母：缩写判定只认 ASCII，避免把 CJK 字符当单词的一部分。"""
    return ("a" <= c <= "z") or ("A" <= c <= "Z")


def _is_lower(c: str) -> bool:
    """是否为 ASCII 小写字母。"""
    return "a" <= c <= "z"


def _is_digit(c: str) -> bool:
    """是否为 ASCII 数字。"""
    return "0" <= c <= "9"
