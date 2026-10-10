"""模块名称：translation_worker.py

功能描述：即时翻译的主流程与工作线程——translate_text 负责"按段切分、逐段请求、
拼接返回"，TranslationWorker 把它包成一个可取消的 QThread。

关键约束：translate_text 同时被后台预翻译（PreTranslator）复用，两条路径必须
产出完全一致的译文，缓存才可能命中；它是同步阻塞的，只能在线程里调用。
"""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from mytraxcure.core.translation.translation_engine import (
    TranslationContext,
    TranslationResult,
    split_segments,
)


def translate_text(engine, cfg, text, context=None, is_canceled=None):
    """按 max_chars_per_segment 切段依次翻译并拼接；返回 None = 中途被取消。

    即时翻译（TranslationWorker）与后台预翻译（PreTranslator）共用同一份实现，
    两条路径产出的译文与写进缓存的内容才会完全一致。

    Args:
        engine: TranslationEngine 实例。
        cfg: TranslationConfig，取语言对与 max_chars_per_segment。
        text: 待译全文。
        context: 术语表与上下文，可为 None。
        is_canceled: 可选回调，返回 True 表示放弃本次翻译。

    Returns:
        TranslationResult | None: 成功或失败的结果；None 表示中途被取消，
        调用方此时不要发任何信号、也不要改 UI 状态。

    Notes:
        段前段后各查一次取消：已在飞行中的请求无法中断，只能在段之间撤销，
        段后再查一次是为了避免把撤消后返回的结果拼进译文（或被写进缓存）。
        任一段出错立即返回错误结果：半截译文没有展示价值，更不该入库。
        返回的 cached 恒为 False：是否命中缓存由调用方先查库决定，这里只管真翻译。
    """
    segments = split_segments(text, cfg.max_chars_per_segment)
    parts: list[str] = []
    elapsed = 0
    model = ""
    for seg in segments:
        if is_canceled is not None and is_canceled():
            return None
        result = engine.translate(seg, cfg.source_lang, cfg.target_lang, context)
        if is_canceled is not None and is_canceled():
            return None
        if result.error is not None:
            return TranslationResult(
                text="",
                source=text,
                backend=engine.name,
                model=model,
                elapsed_ms=elapsed,
                cached=False,
                error=result.error,
            )
        parts.append(result.text)
        elapsed += result.elapsed_ms
        if result.model:
            model = result.model
    return TranslationResult(
        text="".join(parts),
        source=text,
        backend=engine.name,
        model=model,
        elapsed_ms=elapsed,
        cached=False,
    )


class TranslationWorker(QThread):
    """即时翻译的工作线程：一次只跑一个请求，跑完自然结束，实例由主窗口复用。

    生命周期：request() 里 start() → Qt 新建线程调用 run() → 发一个信号后返回，
    线程随即结束；运行期间的重复提交会被静默丢弃（见 request）。
    信号：finished(object) 带 TranslationResult，failed(str) 带展示用的错误文案，
    两者互斥，被取消时都不发。
    退出方式：cancel() 只置标志位，不打断已在飞行中的那一段；run() 自己返回即
    线程结束，不需要 quit() / terminate()。
    """

    #成功回调，参数是 TranslationResult
    #注意：它遮蔽了 QThread 自带的 finished()，别再依赖原生的"线程已结束"信号
    finished = pyqtSignal(object)
    #失败回调，参数是带语言对前缀的错误文案
    failed = pyqtSignal(str)

    def __init__(self,engine,config,parent = None)->None:
        """持有引擎与配置；待译文本、上下文、取消标志每次 request() 重新填。"""
        super().__init__(parent)
        self._engine = engine
        self._config = config
        self._text = ""
        self._context:TranslationContext | None = None
        self._canceled = False

    def request(
        self,
        text:str,
        context:TranslationContext|None = None
        )->None:
        """提交一次翻译任务并启动线程。

        Args:
            text: 待译全文。
            context: 术语表与上下文，可为 None。

        Notes:
            线程正在跑时这次提交被直接丢弃：本 worker 只有一个 run()，
            并发提交会覆盖 _text 并丢掉上一段译文。调用方应先自己判断 isRunning()。
        """
        if self.isRunning():
            return
        self._text = text
        self._context = context
        self._canceled = False
        self.start() #start() 由 Qt 新建线程后回调 run()；直接调 run() 等于在主线程阻塞

    def cancel(self)->None:
        """置取消标志；已在飞行中的那一段无法中断，最多等它自然返回。"""
        self._canceled = True

    def run(self)->None:
        """线程体（覆写 QThread.run）：调 translate_text 并把结果转成信号。

        触发时机：request() 里 start() 之后，由 Qt 在新线程中调用。
        文本为空直接返回；被取消时不发任何信号，保持"取消即无副作用"。
        """
        if not self._text:
            return
        cfg = self._config

        result = translate_text(
            self._engine,
            cfg,
            self._text,
            self._context,
            is_canceled=lambda: self._canceled,
        )
        #取消：两个信号都不发，调用方不必区分"取消"和"没结果"，不会误弹错误提示
        if result is None:
            return
        #失败信号带上语言对：一眼能看出是哪条配置/哪次请求出的问题
        if result.error is not None:
            self.failed.emit(
                f"{cfg.source_lang}→{cfg.target_lang}: {result.error}"
            )
            return
        self.finished.emit(result)
