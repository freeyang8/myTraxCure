"""后台预翻译：加载文档后按阅读顺序把段落依次翻好、写进缓存。

用户注视某段按下空格时直接命中缓存，做到"即时出译文"。
排队顺序 = 重排版面从上到下 = 用户的阅读顺序，所以先翻好的永远是最可能先看的。
"""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from mytraxcure.core.translation.cache_store import CacheStore, make_segment_key
from mytraxcure.core.translation.translation_worker import translate_text


class PreTranslator(QThread):

    progress = pyqtSignal(int, int)  # (已处理, 总数)
    all_done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, engine, config, segments, cache_path=None, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config = config
        self._segments = list(segments)
        self._paused = False
        self._canceled = False
        # 自己开一条 sqlite 连接：连接不跨线程共享，写进去的结果主线程照样能读到
        self._cache = CacheStore(cache_path)

    # ---- 控制（用户即时翻译优先）-----------------------------------------
    def pause(self) -> None:
        """暂停：不再开始新的段落（正在译的那段无法中断，最多等它跑完）。"""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def cancel(self) -> None:
        self._canceled = True
        self._paused = False  # 唤醒等待循环，让线程尽快退出

    @property
    def total(self) -> int:
        return len(self._segments)

    # ---- 线程体 ----------------------------------------------------------
    def run(self) -> None:
        cfg = self._config
        total = len(self._segments)
        try:
            for done, segment in enumerate(self._segments, start=1):
                while self._paused and not self._canceled:
                    self.msleep(50)
                if self._canceled:
                    return

                key = make_segment_key(segment, cfg, self._engine.name)
                try:
                    if self._cache.get(key) is not None:
                        # 已有译文（上次预翻译留下的，或用户已经翻过这段）
                        self.progress.emit(done, total)
                        continue
                    result = translate_text(
                        self._engine, cfg, segment[1], is_canceled=lambda: self._canceled
                    )
                except Exception as exc:  # 本地服务未起 / 超时等：报一次就停，不刷屏
                    self.failed.emit(f"{type(exc).__name__}: {exc}")
                    return
                if result is None:  # 被取消
                    return
                if result.error is not None:
                    self.failed.emit(str(result.error))
                    return

                self._cache.put(key, result, file_hash=None)
                self.progress.emit(done, total)
            self.all_done.emit()
        finally:
            # 线程结束就断开 sqlite（否则每次打开文档都积一条连接，Windows 上还锁文件）
            self._cache.close()
