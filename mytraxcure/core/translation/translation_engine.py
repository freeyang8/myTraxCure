"""模块名称：translation_engine.py

功能描述：翻译层的抽象与公共数据结构——TranslationResult / TranslationContext
两个数据类、引擎抽象基类 TranslationEngine，以及两条翻译路径共用的切段函数。

关键约束：切段结果就是一次请求送出去的原文，而缓存键里存的正是原文，
所以改动 split_segments 的切法会让此前缓存的段落全部命不中。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TranslationResult:
    """一次翻译的产出；text 与 error 互斥——error 非空时 text 为空串，不可展示。"""
    text: str  # 译文
    source: str  # 原文：浮层回显与缓存定位都靠它
    backend: str  # 后端名（当前仅 ollama）；换后端要换一批缓存
    model: str  # 出译文的模型名，用来判断这条缓存是哪次生成的
    elapsed_ms: int  # 墙钟耗时（毫秒）；缓存命中时是上次那次的值，不是本次
    cached: bool  # 是否缓存命中：True 时别拿 elapsed_ms 当速度指标
    error: str | None = None  # 失败原因（"异常类型:异常信息"）；None 表示成功
    ttft_ms: float |None = None #首 token 延迟（毫秒）；缓存命中恒为 None

@dataclass
class TranslationContext:
    """译一段时的附加输入：前后段落与术语表，全部可选。

    约束：指纹里没有上下文字段，所以同一段文本的两条路径必须用同一套 context，
    否则预翻译（不带上下文）与即时翻译（带上下文）会撞同一个键互相覆盖。
    """
    previous_paragraph: str | None = None  # 前 1 段（仅作上下文，不翻译）
    next_paragraph: str | None = None  # 后 1 段
    terms: dict[str, str] | None = None  # 术语表：原文→译文
    page: int | None = None
    block_id: str | None = None


class TranslationEngine(ABC): #抽象基类，不需要具体实现，继承它的类才需要
    """翻译引擎的统一接口：子类只需实现 translate()。

    约定：超时、重试、取消都在实现内部消化成 result.error，不向外抛异常。
    """
    name: str = "base"

    @abstractmethod
    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context: TranslationContext | None = None,
    ) -> TranslationResult:
        """译一段文本（抽象方法，由具体后端实现）。

        Args:
            text: 已切好段的原文，长度不超过 cfg.max_chars_per_segment。
            source_lang: 源语言名，直接进提示词。
            target_lang: 目标语言名，直接进提示词。
            context: 术语表与上下文，可为 None。

        Returns:
            TranslationResult: 成功与失败都返回实例，失败体现在 result.error。
            不抛异常是因为调用方是 QThread.run()，异常在那里会直接消失、无从提示。
        """
        ...  # 译一段文本；超时/重试/取消均由实现内部处理

    def health_check(self) -> bool:
        """引擎当前是否可用；基类恒 True，依赖外部服务的后端自行覆写。"""
        return True

#超长单句的最后兜底：按字数硬切
def _hard_split(text:str,max_chars:int)->list[str]:
    """把一句超长文本按 max_chars 切成若干片。

    Args:
        text: 单个超长句子（可能不含句末标点）。
        max_chars: 每片字符数上限，来自 cfg.max_chars_per_segment。

    Returns:
        list[str]: 按原文顺序排列的切片；未超长时返回只含原文的单元素列表。

    Notes:
        优先在空格处断开以保住英文词边界；中文等无空格文本找不到词边界，
        只能退化为按字数硬切——这是无标点长句会从词中间断开的原因。
    """
    if len(text) <= max_chars:
        return [text]
    chunks:list[str] = []
    while text:
        if len(text) <= max_chars:
            chunks.append(text)
            break
        #在 [0, max_chars) 内取最后一个空格：英文按词边界断，不把单词劈成两半
        cut = text.rfind(' ',0,max_chars)
        if cut <= 0:
            cut = max_chars #无空格可断（中文长句）：退化为按字数硬切
        chunks.append(text[:cut])
        text = text[cut:].lstrip()#切点上的空格已被消费，去掉剩余部分的前导空格
    return chunks

def split_segments(text: str, max_chars: int = 800) -> list[str]:
    """把整段文本切成一批不超过 max_chars 的请求单元。

    先按空行分段，再按句末标点切句，短句在 max_chars 内尽量合并，
    单个超长句交给 _hard_split 硬切。

    Args:
        text: 段落或全文文本。
        max_chars: 每片字符数上限；<= 0 时不做任何切分。

    Returns:
        list[str]: 按原文顺序排列的片段；文本为空时返回空列表。

    Notes:
        默认 800 与 TranslationConfig.max_chars_per_segment 对齐：本地小模型的
        上下文与生成速度都有限，单次请求过长会明显变慢并更容易触发超时。
        切点落在句末标点上，是为了让每片都语义完整，逐片翻译再拼接不会串味。
    """
    if not text or max_chars <= 0:
        return [text] if text else []

    sentence_end = re.compile( #预编译：一篇文档会反复调用，不预编译等于重复解析
        "[^。！？!?…\\n]*[。！？!?…]+[\"\"\\)\\]》]*|\\n",
        re.UNICODE #显式 Unicode 语义，中文标点才能被正确识别
    )
    segments:list[str] = []
    #空行分段：段落是天然语义边界，绝不跨段合并
    for para in text.split('\n\n'):
        buf = "" #当前累积片：短句先攒着，凑满 max_chars 才落一片
        pos = 0 #已消费到段内哪个位置，收尾时用它取正则未覆盖的残留
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
