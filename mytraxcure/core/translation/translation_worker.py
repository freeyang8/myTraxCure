from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from mytraxcure.core.translation.translation_engine import (
    TranslationContext,
    TranslationResult,
    split_segments,
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

        #将长文本切割成多个逻辑段
        segments = split_segments(
            self._text,
            cfg.max_chars_per_segment
        ) 
        parts:list[str] = []
        elapsed = 0
        model = ""
        for seg in segments:
            if self._canceled:
                return
            result = self._engine.translate(
                seg,
                cfg.source_lang,
                cfg.target_lang,
                self._context
            )
            if self._canceled:
                return
            #发送失败信号
            if result.error is not None:
                self.failed.emit(
                    f"{cfg.source_lang}→{cfg.target_lang}: {result.error}"
                )
                return
            parts.append(result.text)
            elapsed += result.elapsed_ms
            if result.model:
                model = result.model

            if self._canceled:
                return
        translationResult = TranslationResult(
            text = "".join(parts),
            source = self._text,
            backend = self._engine.name,
            model = model,
            elapsed_ms=elapsed,
            cached=False #False表示->不是从缓存中读取的
        )
        self.finished.emit(translationResult)
