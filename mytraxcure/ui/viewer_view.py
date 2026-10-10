"""模块名称：viewer_view.py

功能描述：阅读区控件，也是 DocumentRenderer / StepRenderer 的唯一宿主。把渲染器产出的
numpy 图像贴到 QWidget 上，处理滚动与滚轮步进，并把鼠标轨迹作为模拟视线回调出去。

关键约束：
- 两种阅读模式用两套渲染器：连续模式有全局滚动坐标，步进模式只有"屏内坐标"
  （scroll_offset 恒 0）。切换模式必须 _rebuild_renderer，两者不可混用。
- 换文档或换模式都要把滚动偏移和触控板累加重置：它们属于旧版面的状态，
  带过去会让新版面起点错位。
- paintEvent 里把 numpy 图像包成 QImage 时复用的是同一块内存，不做拷贝，因此该
  QImage 只在本次绘制期间有效，不能缓存起来跨帧使用。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

from mytraxcure.core.config import ReadingConfig
from mytraxcure.core.file.file_parser import ParsedDocument
from mytraxcure.core.file.file_viewer import DocumentRenderer, StepRenderer
from mytraxcure.core.file.text_mapper import TextMapper
from mytraxcure.core.types import GazePoint

_WHEEL_NOTCH = 120       # 鼠标滚轮一格（Qt angleDelta 单位：1/8 度）
_WHEEL_PIXEL_STEP = 100  # 触控板/平滑滚动：累计这么多像素走一句
_STEP_MAX_STEPS = 3      # 单次滚轮事件最多跨几步，防甩飞
_SOLO_SCROLL_PX = 100    # 超长句独占一屏时，一格滚轮在句内的滚动量


class ViewerView(QWidget):
    """阅读区：持有当前渲染器并把它产出的图像画到控件上。

    Qt 层面的位置：MainWindow 的下属控件，不理解业务，装配项全部由主窗口注入
    （set_mapper / set_reading_config / set_step_callback 等）。
    覆写 mouseMoveEvent、wheelEvent、paintEvent：前两个把输入转成本控件的语义动作，
    paintEvent 负责把渲染结果贴出来。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """建立空的阅读区，等待 load_document 装载文档。

        Args:
            parent: 父窗口。
        """
        super().__init__(parent)
        self._document: ParsedDocument | None = None
        self._renderer: DocumentRenderer | StepRenderer | None = None  #两种模式二选一
        self._mapper: TextMapper | None = None #文本坐标映射器（屏幕→文档命中）
        self._reading = ReadingConfig()  # 逐句步进参数（MainWindow 用配置覆盖）
        self._step_mode = False          # True = 逐句步进；False = 连续滚动
        self._step_callback = None       # 换屏回调（主窗口更新阅读进度）
        self._step_acc = 0.0             # 亚格滚轮量累加（触控板）
        self._scroll = QPoint(0, 0) #滚动偏移
        self._gaze_callback = None #视线回调（P0：鼠标模拟视线）
        self._scroll_locked = False #滚动锁：译文浮层显示期间禁止滚动
        self.setMouseTracking(True)#鼠标在控件上移动就持续触发，无需按下按键

    # ---- 装配 ------------------------------------------------------------
    def set_mapper(self, mapper: TextMapper) -> None:
        """挂载映射器，并会在下次重建渲染器时一并注入。

        Args:
            mapper: 命中与段落聚合用的映射器。
        """
        self._mapper = mapper

    def set_gaze_callback(self, callback) -> None:
        """登记鼠标移动回调：P0 阶段用鼠标位置模拟视线。

        Args:
            callback: 接收 GazePoint 的可调用对象。
        """
        self._gaze_callback = callback

    def set_scroll_locked(self, locked: bool) -> None:
        """设置滚动锁；译文浮层显示期间冻结页面，避免文字滑动造成浮层错位。"""
        self._scroll_locked = bool(locked)

    def set_reading_config(self, reading: ReadingConfig) -> None:
        """替换逐句步进参数；下一次重建渲染器时生效。

        Args:
            reading: 阅读配置，提供字号、行高、边距等自适应参数。
        """
        self._reading = reading

    def set_step_callback(self, callback) -> None:
        """每次换屏（翻句/装载/切模式）后回调，主窗口据此更新阅读进度。

        Args:
            callback: 接收 (StepScreen, 全篇句数) 的可调用对象。
        """
        self._step_callback = callback

    def current_screen(self):
        """当前 StepScreen；不在步进模式或尚未排版时返回 None。"""
        renderer = self._renderer
        return renderer.screen if isinstance(renderer, StepRenderer) else None

    def _notify_step(self) -> None:
        """把当前屏信息推给回调；屏幕为空（n == 0）时不打扰主窗口。"""
        renderer = self._renderer
        if self._step_callback is not None and isinstance(renderer, StepRenderer):
            screen = renderer.screen
            if screen is not None and screen.n > 0:
                self._step_callback(screen, len(renderer.units))

    @property
    def step_mode(self) -> bool:
        """当前是否处于逐句步进模式。"""
        return self._step_mode

    def set_reading_mode(self, step_mode: bool) -> None:
        """切换连续滚动 / 逐句步进。步进模式用独立的版面渲染器（无全局滚动坐标）。

        Args:
            step_mode: True 切到逐句步进，False 切回连续滚动。
        """
        self._step_mode = bool(step_mode)
        self._rebuild_renderer()
        self._scroll = QPoint(0, 0)
        self._step_acc = 0.0
        self.update()

    def prime_reading_blocks(self) -> None:
        """先算一遍版面并注册块，不等第一帧（预翻译取的是 mapper.segments()）。

        若等到首次 paintEvent 才排版，预翻译队列会因拿不到 segments 而空跑一轮。
        """
        renderer = self._renderer
        if isinstance(renderer, StepRenderer):
            renderer.ensure_screen(max(self.width(), 1), max(self.height(), 1))
            self._notify_step()

    def load_document(self, document: ParsedDocument) -> None:
        """装载文档并重建渲染器。

        Args:
            document: 解析产物；渲染器会就地回填它的 bbox。
        """
        self._document = document
        self._rebuild_renderer()
        self._scroll = QPoint(0,0)
        self._step_acc = 0.0
        self.update()# 通知 Qt 调度 paintEvent 重画控件

    def _rebuild_renderer(self) -> None:
        """按当前模式造一个全新的渲染器并注入映射器。

        Notes:
            每次都重建而不是复用：两个渲染器给同一批 block 写的坐标语义不同
            （文档坐标 vs 屏内坐标），复用的旧实例会把错误坐标留给命中。
            mapper 注入进去后，TXT/Word 才能在渲染期回填 bbox 时自动重注册——
            这是段落模式对 TXT 生效的唯一通路。
        """
        if self._document is None:
            self._renderer = None
            return
        if self._step_mode:
            renderer: DocumentRenderer | StepRenderer = StepRenderer(self._document, self._reading)
        else:
            renderer = DocumentRenderer(self._document)
        # 注入 mapper：TXT/Word 渲染期回填 bbox 后能调 register_blocks 重新注册
        renderer.set_mapper(self._mapper)
        self._renderer = renderer

    def _handle_scroll(self,wheel_dx:int,wheel_dy:int) -> None:
        """把滚轮位移折算成内容位移；Qt 的 angleDelta 向下为正，与"向下读"相反，故取负。

        Args:
            wheel_dx: 水平滚轮量。
            wheel_dy: 垂直滚轮量。
        """
        dx = int(-wheel_dx)
        dy = int(-wheel_dy)
        self.scroll_contents(dx,dy)

    # ---- 交互 ------------------------------------------------------------
    def mouseMoveEvent(self, event) -> None:
        """鼠标移动 → 模拟视线回调。

        回调里给的是 globalPosition：凝视判定要在屏幕坐标下做，使用窗口内坐标会少一个
        窗口原点偏移。原型阶段借此把鼠标输入接到与眼动完全相同的后级链路上。
        """
        if self._gaze_callback is not None:
            pos = event.globalPosition()
            self._gaze_callback(GazePoint(x=pos.x(), y=pos.y()))
        super().mouseMoveEvent(event)

    def wheelEvent(self, event) -> None:
        """滚轮：步进模式下转成按句步进，连续模式下转成像素滚动。"""
        if self._step_mode:
            self._handle_step_wheel(event)
            event.accept()
            return
        # 滚轮只负责滚动，滚动后同步 mapper.update_viewport
        #super().wheelEvent(event)

        if self._renderer is None:
            super().wheelEvent(event)
            return

        delta = event.angleDelta()
        self._handle_scroll(
            delta.x(), #水平滚动量
            delta.y() #垂直滚动量
        )
        event.accept() #阻止事件继续转发给父级，避免触发双重滚动

    # ---- 逐句步进 --------------------------------------------------------
    def _handle_step_wheel(self, event) -> None:
        """一格滚轮 = 一句。整格事件不累计余数（否则滚 5 格会多走一步）。

        Args:
            event: QWheelEvent。优先读 angleDelta，为零（触控板）才退到 pixelDelta。
        """
        angle = event.angleDelta().y()
        if angle:
            steps = self._angle_to_steps(angle)
        else:
            steps = self._pixel_to_steps(event.pixelDelta().y())
        if steps:
            self.step_reading(steps)

    def _angle_to_steps(self, angle: int) -> int:
        """机械滚轮的量 → 步数；满一格直接换算，不足一格才累加。

        Args:
            angle: angleDelta 的垂直分量，向上为正。

        Returns:
            有符号步数，绝对值不超过 _STEP_MAX_STEPS；不足一格时返回 0。
        """
        if abs(angle) >= _WHEEL_NOTCH:
            self._step_acc = 0.0
            count = min(abs(angle) // _WHEEL_NOTCH, _STEP_MAX_STEPS)
            return count if angle > 0 else -count
        self._step_acc += angle
        if abs(self._step_acc) < _WHEEL_NOTCH:
            return 0
        step = int(self._step_acc / _WHEEL_NOTCH)
        self._step_acc = 0.0
        return step if step else (1 if angle > 0 else -1)

    def _pixel_to_steps(self, dy: int) -> int:
        """触控板像素量 → 步数：累计满 _WHEEL_PIXEL_STEP 才走一步。

        Args:
            dy: pixelDelta 的垂直分量。

        Returns:
            有符号步数；不足一个阈值或无位移时返回 0。
        """
        if not dy:
            return 0
        self._step_acc += dy
        if abs(self._step_acc) < _WHEEL_PIXEL_STEP:
            return 0
        step = int(self._step_acc / _WHEEL_PIXEL_STEP)
        self._step_acc = 0.0
        return max(-_STEP_MAX_STEPS, min(_STEP_MAX_STEPS, step))

    def step_reading(self, steps: int) -> bool:
        """按句步进（正数向下）。返回是否有过位移。

        Args:
            steps: 带符号的步数；0 视为无操作。

        Returns:
            True 表示至少推进了一句（或有屏内滚动），调用方可能需要更新进度显示。

        Notes:
            超长句独占一屏时先消耗它自己的内部滚动，滚到边界才开始换句——
            否则一句内部的内容会被整句跳过。
        """
        renderer = self._renderer
        if self._scroll_locked or not isinstance(renderer, StepRenderer) or not steps:
            return False
        direction = 1 if steps > 0 else -1
        moved = False
        for _ in range(abs(steps)):
            # 超长句独占一屏：先走它自己的内部滚动，滚到边界才换句
            if renderer.solo_scroll(direction * _SOLO_SCROLL_PX) or renderer.step(direction):
                moved = True
            else:
                break
        if moved:
            self._renderer.ensure_screen(max(self.width(), 1), max(self.height(), 1))
            self._notify_step()
            self.update()
        return moved

    def scroll_contents(self, dx: int, dy: int) -> None:
        """按像素滚动内容，并夹在版面范围内。

        Args:
            dx: 水平位移，向右为正。
            dy: 垂直位移，向下为正。
        """
        if self._scroll_locked:  # 浮层显示期间冻结页面（滚轮/拖动均被拦截）
            return
        if self._step_mode:      # 步进模式没有连续滚动
            return
        max_x = max_y = 0
        if self._renderer is not None:
            cw, ch = self._renderer.content_size()
            max_x, max_y = max(cw - self.width(), 0), max(ch - self.height(), 0)
        # 夹在版面范围内：别滚进空白，也别滚出文档尾部
        self._scroll.setX(min(max(self._scroll.x() + dx, 0), max_x))
        self._scroll.setY(min(max(self._scroll.y() + dy, 0), max_y))
        self._sync_viewport()
        self.update()

    def _sync_viewport(self) -> None:
        """把控件当前的屏幕位置与滚动偏移同步给映射器。

        每次滚动与每次重绘都要调：窗口被拖动、分辨率变化都会改变 window_origin，
        而映射器只认同步进来的值，不同步就会整体偏移一个窗口位置。
        """
        if self._mapper is not None:

            origin = self.mapToGlobal( QPoint(0, 0) )  # 控件左上角 → 屏幕绝对坐标

            self._mapper.update_viewport(
                window_origin=(origin.x(), origin.y()), #屏幕绝对位置
                scroll_offset=(self._scroll.x(), self._scroll.y()), #内容滚动偏移
            )

    def paintEvent(self, event) -> None:
        """渲染当前视口并贴到控件上。

        顺序固定：先同步视口（命中要用），再向渲染器要这一屏的图像。numpy 图像被包成
        QImage 时复用其内存而非拷贝，因此只能在本次绘制内使用。

        Args:
            event: Qt 重绘事件。
        """
        if self._renderer is None:
            # 无文档时不获取画笔，避免空 QPainter 告警
            return
        self._sync_viewport()

        # 只渲染可视区：返回的图像就是控件大小，滚多深都不会分配超大位图
        img = self._renderer.render_viewport(
            self._scroll.x(), self._scroll.y(), self.width(), self.height()
        )

        h,w = img.shape[:2] #numpy 数组形状是 (行=高, 列=宽)
        qimg = QImage(
            img.data, #复用 numpy 内存，不拷贝；QImage 仅在本帧有效
            w,
            h,
            w*3, #每行字节数（RGB888 每像素 3 字节）
            QImage.Format.Format_RGB888 #每像素 24bit，按 RGB 顺序排列
        )
        painter = QPainter(self)
        painter.drawPixmap(0, 0, QPixmap.fromImage(qimg))  # 已是视口切片，不再需要负偏移
        painter.end()
