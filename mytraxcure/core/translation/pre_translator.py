"""模块名称：pre_translator.py

功能描述：后台预翻译。加载文档后按阅读顺序把段落依次翻好、只写进缓存，
用户注视某段按下空格时直接命中缓存，做到"即时出译文"。

关键约束：排队顺序 = 重排版面从上到下 = 用户的阅读顺序，所以先翻好的永远是最可能
先看的；本线程不参与展示，用户自己发起翻译时会被 pause() 让路。
"""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from mytraxcure.core.translation.cache_store import CacheStore, make_segment_key
from mytraxcure.core.translation.translation_worker import translate_text


class PreTranslator(QThread):
    """后台预翻译线程：按阅读顺序逐段翻译并落缓存，中途可暂停、可取消。

    生命周期：主窗口 start() 后逐段推进，全部完成发 all_done；出错或被取消则
    发 failed 后返回；无论走哪条出口，finally 里都会关掉自己的 sqlite 连接。
    信号：progress(已处理, 总数)、all_done()、failed(错误文案)。
    退出方式：cancel() 置标志并解除暂停（唤醒等待循环），run() 在下一段边界返回；
    正在译的那段无法中断，因此主窗口另用 wait(3000) 兜底。
    """

    progress = pyqtSignal(int, int)  # (已处理, 总数)
    all_done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, engine, config, segments, cache_path=None, parent=None) -> None:
        """记下引擎、配置与待译段落；缓存连接在本实例里独立开一条。"""
        super().__init__(parent)
        self._engine = engine
        self._config = config
        self._segments = list(segments) #拷贝一份：主线程再来改段落列表不影响本轮
        self._paused = False
        self._canceled = False
        # 自带一条 sqlite 连接：不与主线程的 CacheStore 抢同一条连接，
        # WAL 模式下写进去的结果主线程照样能读到
        self._cache = CacheStore(cache_path)

    # ---- 控制（用户即时翻译优先）-----------------------------------------
    def pause(self) -> None:
        """暂停：不再开始新的段落（正在译的那段无法中断，最多等它跑完）。

        用户自己发起翻译时由主窗口调用，把本地模型让给即时翻译。
        """
        self._paused = True

    def resume(self) -> None:
        """恢复：从下一段继续；已译过的段靠缓存命中跳过，不会重翻。"""
        self._paused = False

    def cancel(self) -> None:
        """请求停止本轮预翻译：置取消标志并解除暂停，让线程尽快退出。"""
        self._canceled = True
        self._paused = False  # 暂停中的线程也要醒过来，否则会一直睡在等待循环里

    @property
    def total(self) -> int:
        """本轮待译段落总数，即进度信号的分母。"""
        return len(self._segments)

    # ---- 线程体 ----------------------------------------------------------
    def run(self) -> None:
        """线程体（覆写 QThread.run）：取键 → 查缓存 → 翻译 → 写缓存 → 报进度。

        触发时机：主窗口 start() 后由 Qt 在新线程调用。
        三个出口：全部完成（发 all_done）、异常或译文出错（发 failed）、
        被取消（静默返回）；连接关闭由 finally 兜住。
        """
        cfg = self._config
        total = len(self._segments)
        try:
            for done, segment in enumerate(self._segments, start=1):
                #睡眠轮询而非条件变量：50ms 粒度足够让"用户请求优先"立刻生效，又不占 CPU
                while self._paused and not self._canceled:
                    self.msleep(50)
                if self._canceled:
                    return

                key = make_segment_key(segment, cfg, self._engine.name)
                try:
                    if self._cache.get(key) is not None:
                        # 已有译文（上次预翻译留下的，或用户已经翻过这段）：跳过只报进度
                        self.progress.emit(done, total)
                        continue
                    #只传文本不传 context：预翻译没有"当前在读哪段"这回事，
                    #且缓存键不含上下文，两边必须保持一致才不会互相覆盖
                    result = translate_text(
                        self._engine, cfg, segment[1], is_canceled=lambda: self._canceled
                    )
                except Exception as exc:  # 本地服务未起 / 超时等：报一次就停，不刷屏
                    self.failed.emit(f"{type(exc).__name__}: {exc}")
                    return
                if result is None:  # 被取消：静默返回，不发 failed 也不发 all_done
                    return
                if result.error is not None:
                    self.failed.emit(str(result.error))
                    return

                #只写缓存不回传译文：展示是用户按空格那次的事，这里没有可对应的版面位置
                self._cache.put(key, result, file_hash=None)
                self.progress.emit(done, total)
            self.all_done.emit()
        finally:
            # 线程结束就断开 sqlite（否则每次打开文档都积一条连接，Windows 上还锁文件）
            self._cache.close()
