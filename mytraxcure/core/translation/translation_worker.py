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


#负责执行翻译任务
class TranslationWorker(QThread):

    #信号定义
    finished = pyqtSignal(object) #携带TranslationResult对象
    failed = pyqtSignal(str)

    def __init__(self,engine,config,parent = None)->None:
        super().__init__(parent)
        self._engine = engine
        self._config = config
        self._text = ""
        self._context:TranslationContext | None = None
        self._canceled = False
    
    #任务提交方法
    def request(
        self,
        text:str,
        context:TranslationContext|None = None
        )->None:
        if self.isRunning():
            return
        self._text = text
        self._context = context
        self._canceled = False
        self.start() #调用start()触发run()方法，这里会向系统申请一个线程
    
    #取消方法
    def cancel(self)->None:
        self._canceled = True

    #线程执行体
    def run(self)->None:
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
        if result is None:  # 取消：不发任何信号（与原实现一致）
            return
        #发送失败信号
        if result.error is not None:
            self.failed.emit(
                f"{cfg.source_lang}→{cfg.target_lang}: {result.error}"
            )
            return
        self.finished.emit(result)
