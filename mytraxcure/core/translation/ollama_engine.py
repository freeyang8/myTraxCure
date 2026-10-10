"""模块名称：ollama_engine.py

功能描述：TranslationEngine 的 Ollama 实现——把翻译请求打到本地 Ollama 的
/api/generate（流式），把分片拼成一条 TranslationResult 返回。

关键约束：translate() 是同步阻塞的，最长会占满 timeout_s × (max_retries + 1)，
只能在 TranslationWorker / PreTranslator 这类工作线程里调用；
所有异常都在本层收敛成 result.error，不外抛，否则会在 QThread.run() 里消失。
"""

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

# 术语表与上下文都是可选块：没有时插空串而不是留空标题，
# 否则模型会对着"【术语表】"这种空壳自己编内容。
# 改这个模板等同于换了一种译法，需要同步 bump CacheKey.prompt_version 让旧缓存失效。
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
    """Ollama 后端：无连接、无状态，只持有配置，实例可在整个进程里长期复用。"""

    name = "ollama"

    def __init__(self, config: TranslationConfig) -> None:
        self.config = config

    def _build_prompt(
        self,
        text:str,
        src:str,
        tgt:str,
        context:TranslationContext|None
    )->str:
        """把原文、术语表与前后段落填进 _PROMPT_TMPL。

        Args:
            text: 待译原文，调用方已按 cfg.max_chars_per_segment 切好段。
            src: 源语言名，进模板首行的 {src}。
            tgt: 目标语言名，进模板首行的 {tgt}。
            context: 术语表与前后段落；None 时只译原文。

        Returns:
            str: 拼好的完整提示词。

        Notes:
            上下文两条都显式标注"供参考，勿译"：不写清用途，模型很容易把前后段落
            一并译出来或照抄进正文。前文给译文、后文给原文，是让模型对齐已译术语
            又能顺着原文语义往下接。
        """
        #术语表整块给出：成表呈现时模型的遵循度明显高于散落在指令里
        terms_block = ""
        if context and context.terms:
            lines = [f"- {k} -> {v}" for k , v in context.terms.items()]
            terms_block = "【术语表】\n" + "\n".join(lines)

        #前后各取一段即可：再多会挤占本地小模型本就有限的上下文
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
        """译一段文本：流式请求 /api/generate，失败按配置重试后仍失败则返回错误结果。

        Args:
            text: 待译原文，已按 cfg.max_chars_per_segment 切好段。
            source_lang: 源语言名，直接写进提示词。
            target_lang: 目标语言名，直接写进提示词。
            context: 术语表与上下文，可为 None。

        Returns:
            TranslationResult: 成功时 text 为拼接后的译文、error 为 None；
            重试全部失败时 text 为空串、error 形如 "异常类型:异常信息"。

        Notes:
            同步阻塞，最坏占满 timeout_s × (max_retries + 1)，只能在后台线程调用。
            elapsed_ms 只量当次请求的墙钟时间，不含重试间隔；ttft_ms 是首个非空
            分片的到达延迟，全程无内容时为 None，用于区分"模型慢"和"网络慢"。
            请求一旦发出就无法取消：取消只在段与段之间生效。
        """
        #提示词要在发请求前一次性拼完：重试时复用同一份，避免内容不一致
        prompt = self._build_prompt(
            text,
            source_lang,
            target_lang,
            context
        )
        url = f"{self.config.ollama_url}/api/generate" #流式生成（NDJSON，逐行一个对象）

        #请求体
        payload = {
            "model": self.config.model, 
            "prompt": prompt,
            "stream": True,
            #低温求稳：译文要可复现，同一段两次结果不同会让缓存失去意义
            "options":{"temperature" : 0.3}
        }
        last_err = ""
        #共 1 + max_retries 次尝试；每次重试前先 sleep，给本地模型一个喘息窗口
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                #固定间隔而非指数退避：本地服务抖动多半是模型刚被占用，等 1s 就够
                time.sleep(self.config.retry_interval_s)
            try:
                t0 = time.perf_counter() #perf_counter 不受系统时间调整影响，只量墙钟
                ttft:float | None = None #首字延迟；全程无内容时保持 None
                chunks : list[str] = []  #按到达顺序累积，顺序即生成顺序

                #必须显式关闭响应：重试会反复建连接，不关会耗尽本地端口
                with requests.post(
                    url,
                    json = payload,
                    stream = True,
                    timeout = self.config.timeout_s
                )as resp:
                    resp.raise_for_status() #非 2xx 直接抛，交给下面的重试
                    for line in resp.iter_lines(): #流式响应是一行一个 JSON 对象
                        if not line:
                            continue
                        obj = json.loads(line)
                        token = obj.get("response","")

                        #只在首个非空分片记一次：之后的分片反映的是生成速度而非首字延迟
                        if token and ttft is None:
                            ttft = time.perf_counter() - t0
                        chunks.append(token)

                        #done 为真即生成结束，此后不会再有 response 内容
                        if obj.get("done"):
                            break

                    #strip：模型常在末尾多吐一个换行，不去掉会污染拼接结果
                    result_text = "".join(chunks).strip()

                    #耗时算到收完最后一个分片为止，自然包含网络传输时间
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
            #三类都要接：连接失败/超时(RequestException)、socket 层(OSError)、
            #以及响应体不是合法 JSON(ValueError)——本地服务被抢占时这三类都出过
            except (requests.RequestException,OSError,ValueError) as e:
                last_err = f"{type(e).__name__}:{e}"
                continue

        #重试耗尽：交回空译文 + 错误串，是否提示用户由调用方决定
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
        """探测本地 Ollama 是否可用、目标模型是否已下载。

        Returns:
            bool: True 表示服务可达且模型在列表中；任何网络或解析异常都返回 False。

        Notes:
            超时取 min(timeout_s, 5.0)：这是启动期的轻量探测，不该让用户等满
            一次翻译的完整超时。
            模型名用子串匹配（config.model in model），所以配置里写不带 tag 的
            短名也能匹配上 "xxx:latest" 这类全名——代价是短名可能误匹配到别的模型。
        """
        try:
            resp = requests.get(
                f"{self.config.ollama_url}/api/tags", #列出本地已下载的模型
                timeout = min(self.config.timeout_s,5.0)
            )
            resp.raise_for_status()
            models = {model["name"] for model in resp.json().get("models",[])}
            return any(self.config.model in model for model in models)
        except (requests.RequestException,OSError,ValueError):
            return False
