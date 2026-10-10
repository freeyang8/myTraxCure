"""句子级切分：把文档切成"一句一段"的显示单位（逐句步进阅读模式用）。

切分顺序与重排渲染一致：先按段落（`para_key` 粒度）分，再在段内按句末标点切。
先分段是必要的——`## Introduction` 这类标题没有句末标点，不先分段就会跟下一段正文粘成一句。

规则来自原型 tools/step_reader_prototype.html（浏览器里逐条实测过），要点：

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
    """一句 + 它在段落文本中的字符区间（区间供后续回填 block 坐标用）。"""

    text: str
    start: int
    end: int
    para_index: int
    heading: bool = False


def is_heading(paragraph: str) -> bool:
    """标题段：以 `#` 开头（Markdown 风格），显示时去掉井号并加粗。"""
    return paragraph.lstrip().startswith(HEADING_PREFIX)


def is_separator(line: str) -> bool:
    """`---` / `***` 这类纯分隔符行：不承载文字，逐句模式下整段丢弃。"""
    return len(line.strip()) >= 2 and all(c in "-—*_=~ \t" for c in line)


def strip_heading(paragraph: str) -> str:
    return paragraph.lstrip().lstrip(HEADING_PREFIX).strip() if is_heading(paragraph) else paragraph


def split_sentences(paragraph: str, para_index: int = 0, heading: bool = False) -> list[Sentence]:
    """把一段文本切成句子；纯空白片段丢弃，终止符保留在句尾。"""
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
    """把整篇纯文本切句：空行分段，段内单换行合并（英文 txt 常见硬折行）。"""
    out: list[Sentence] = []
    for index, paragraph in enumerate(split_paragraphs(text)):
        heading = is_heading(paragraph)
        body = strip_heading(paragraph) if heading else paragraph
        out.extend(split_sentences(body, index, heading))
    return out


def split_paragraphs(text: str) -> list[str]:
    """空行分段；`#` 开头的行独立成段；段内软换行拼成一行。"""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
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
    j = i + 1
    while j < len(text) and (text[j] in CJK_STOP or text[j] in EN_STOP):
        j += 1
    return j


def _stop_allowed(text: str, i: int) -> bool:
    """该位置的终止符是否真的断句（中文终止符恒真，英文句点走反例保护）。"""
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
    return ("a" <= c <= "z") or ("A" <= c <= "Z")


def _is_lower(c: str) -> bool:
    return "a" <= c <= "z"


def _is_digit(c: str) -> bool:
    return "0" <= c <= "9"
