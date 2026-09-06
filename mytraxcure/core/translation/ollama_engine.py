from __future__ import annotations

import json
import time

import requests

from mytraxcure.core.config import TranslationConfig
from mytraxcure.core.translation.translation_engine import (
    TranslationContext,
    TranslationEngine,
    TranslationResult,
)

_PROMPT_TMPL = """你是一名专业翻译。请将【原文】中的{src}文本翻译为{tgt}。
要求：
1. 只输出译文，不要解释。
2. 保持原文的段落与换行结构。
{terms_block}{context_block}
【原文】
{text}

【译文】
"""

class OllamaEngine(TranslationEngine):

    name = "ollama"

    def __init__(self, config: TranslationConfig) -> None:
        self.config = config

    #组装提示词
    def _build_prompt(
        self,
        text:str,
        src:str,
        tgt:str,
        context:TranslationContext|None
    )->str:
        #术语表
        terms_block = ""
        if context and context.terms:
            lines = [f"- {k} -> {v}" for k , v in context.terms.items()]
            terms_block = "【术语表】\n" + "\n".join(lines)

        #要翻译的段落的前后段落（供翻译模型参考）
        context_block = ""
        if context and (context.previous_paragraph or context.next_paragraph):
            parts = []
            if context.previous_paragraph:
                parts.append(f"（前文译文供参考，勿译）{context.previous_paragraph}")
            if context.next_paragraph:
                parts.append(f"（后文原文供参考，勿译）{context.next_paragraph}")
            context_block = "【上下文】\n" + "\n".join(parts)
        _prompt_tmpl = _PROMPT_TMPL.format(
            src = src,
            tgt = tgt,
            terms_block = terms_block,
            context_block = context_block,
            text = text
        )
        return _prompt_tmpl
    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context: TranslationContext | None = None,
    ) -> TranslationResult:
        #生成提示词
        prompt = self._build_prompt( 
            text,
            source_lang, 
            target_lang,
            context 
        )
        url = f"{self.config.ollama_url}/api/generate" #流式生成
        
        #请求体
        payload = {
            "model": self.config.model, 
            "prompt": prompt,
            "stream": True,
            "options":{"temperature" : 0.3}
        }
        last_err = ""
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                time.sleep(self.config.retry_interval_s)
            try:
                t0 = time.perf_counter() #记录请求开始前的高精度时间戳
                ttft:float | None = None #首字延迟
                chunks : list[str] = []  #存储所有接收到的文本片段
                
                #使用上下文管理器，确保响应对象正确关闭
                with requests.post( 
                    url,
                    json = payload,
                    stream = True,
                    timeout = self.config.timeout_s
                )as resp:
                    resp.raise_for_status() #如果状态码不是 2xx，抛出异常
                    for line in resp.iter_lines(): #逐行读取响应，每行是一个JSON对象
                        if not line:
                            continue
                        obj = json.loads(line) #JSON解析为字典
                        token = obj.get("response","") #提取当前 token 文本

                        #记录首token获取时间
                        if token and ttft is None:
                            ttft = time.perf_counter() - t0 
                        chunks.append(token)

                        #生成完毕
                        if obj.get("done"):
                            break

                    result_text = "".join(chunks).strip() #join()->用指定的分隔符(这里是使用""，空字符串)将chunks中的元素连接为一个字符串

                    #总耗时
                    elapsed = int(
                        (time.perf_counter() - t0)*1000
                    )
                    translationResult = TranslationResult(
                        text = result_text,
                        source = text,
                        backend = self.name,
                        model = self.config.model,
                        elapsed_ms = elapsed,
                        cached = False,
                        ttft_ms = ttft
                    )
                    return translationResult
            except (requests.RequestException,OSError,ValueError) as e:
                last_err = f"{type(e).__name__}:{e}"
                continue

        return TranslationResult(
            text="",
            source=text,
            backend=self.name,
            model=self.config.model,
            elapsed_ms=0,
            cached=False,
            error=last_err,          
        )

    def health_check(self) -> bool:
        try:
            resp = requests.get(
                f"{self.config.ollama_url}/api/tags", #获取所有已下载模型的列表
                timeout = min(self.config.timeout_s,5.0)
            )
            resp.raise_for_status()
            models = {model["name"] for model in resp.json().get("models",[])} #提取模型名称
            return any(self.config.model in model for model in models)
        except (requests.RequestException,OSError,ValueError):
            return False
