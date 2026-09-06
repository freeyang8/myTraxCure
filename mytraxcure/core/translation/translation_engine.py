from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranslationResult:
    text: str  # 译文
    source: str  # 原文
    backend: str  # 后端名（ollama/builtin）
    model: str  # 模型名
    elapsed_ms: int  # 耗时
    cached: bool  # 是否缓存命中
    error: str | None = None  # 错误信息
    ttft_ms: float |None = None #首token耗时

@dataclass
class TranslationContext:
    previous_paragraph: str | None = None  # 前 1 段（仅作上下文，不翻译）
    next_paragraph: str | None = None  # 后 1 段
    terms: dict[str, str] | None = None  # 术语表：原文→译文
    page: int | None = None
    block_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class TranslationEngine(ABC): #抽象基类，不需要具体实现，继承它的类才需要
    name: str = "base"

    @abstractmethod
    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context: TranslationContext | None = None,
    ) -> TranslationResult:
        ...  # 翻译一段文本。实现需处理超时/重试/取消等。

    def health_check(self) -> bool:
        return True

#处理超长句子
def _hard_split(text:str,max_chars:int)->list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks:list[str] = []
    while text:
        if len(text) <= max_chars:
            chunks.append(text)
            break
        cut = text.rfind(' ',0,max_chars) # 在 [0, max_chars) 范围内找最后一个空格位置（作为英文词边界）
        if cut <= 0:
            cut = max_chars
        chunks.append(text[:cut])
        text = text[cut:].lstrip()#剩余部分去掉前导空格
    return chunks

#句子分割
def split_segments(text: str, max_chars: int = 800) -> list[str]:
    if not text or max_chars <= 0:
        return [text] if text else []
    
    sentence_end = re.compile( #进行编译，提高匹配效率
        "[^。！？!?…\\n]*[。！？!?…]+[\"\"\\)\\]》]*|\\n",
        re.UNICODE #让 \w、\s 等匹配 Unicode 字符
    )
    segments:list[str] = []
    #按两个换行符分割段落（空行分隔）
    for para in text.split('\n\n'):
        buf = "" #记录当前段落
        pos = 0 #记录正则匹配处理到的位置
        #遍历段落中每个句子匹配对象
        for m in sentence_end.finditer(para):
            sentence = m.group(0)
            if len(sentence) > max_chars:
                if buf:
                    segments.append(buf)
                    buf = ""
                segments.extend(_hard_split(sentence, max_chars))
                pos = m.end()
                continue
            if len(buf) + len(sentence) > max_chars:
                if buf:
                    segments.append(buf)
                    buf = ""
                buf = sentence
            else:
                buf += sentence
            pos = m.end()
        
        tail = para[pos:]
        if tail:
            if len(buf) + len(tail) > max_chars:
                if buf:
                    segments.append(buf)
                    buf = ""
                segments.extend(_hard_split(tail, max_chars))
            else:
                buf += tail
        if buf:
            segments.append(buf)
            buf = ""
    return segments
