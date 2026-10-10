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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document: ParsedDocument | None = None
        self._renderer: DocumentRenderer | StepRenderer | None = None  #存储加载的文档
        self._mapper: TextMapper | None = None #文本坐标映射器（屏幕→文档命中）
        self._reading = ReadingConfig()  # 逐句步进参数（MainWindow 用配置覆盖）
        self._step_mode = False          # True = 逐句步进；False = 连续滚动
        self._step_callback = None       # 换屏回调（主窗口更新阅读进度）
        self._step_acc = 0.0             # 亚格滚轮量累加（触控板）
        self._scroll = QPoint(0, 0) #滚动偏移
        self._gaze_callback = None #视线回调（P0：鼠标模拟视线）
        self._scroll_locked = False #滚动锁：译文浮层显示期间禁止滚动
        self.setMouseTracking(True)#鼠标在控件上移动就持续触发

    # ---- 装配 ------------------------------------------------------------
    def set_mapper(self, mapper: TextMapper) -> None:
        self._mapper = mapper

    def set_gaze_callback(self, callback) -> None:
        self._gaze_callback = callback

    def set_scroll_locked(self, locked: bool) -> None:
        self._scroll_locked = bool(locked)

    def set_reading_config(self, reading: ReadingConfig) -> None:
        self._reading = reading

    def set_step_callback(self, callback) -> None:
        """每次换屏（翻句/装载/切模式）后回调，主窗口据此更新阅读进度。"""
        self._step_callback = callback

    def current_screen(self):
        renderer = self._renderer
        return renderer.screen if isinstance(renderer, StepRenderer) else None

    def _notify_step(self) -> None:
        renderer = self._renderer
        if self._step_callback is not None and isinstance(renderer, StepRenderer):
            screen = renderer.screen
            if screen is not None and screen.n > 0:
                self._step_callback(screen, len(renderer.units))

    @property
    def step_mode(self) -> bool:
        return self._step_mode

    def set_reading_mode(self, step_mode: bool) -> None:
        """切换连续滚动 / 逐句步进。步进模式用独立的版面渲染器（无全局滚动坐标）。"""
        self._step_mode = bool(step_mode)
        self._rebuild_renderer()
        self._scroll = QPoint(0, 0)
        self._step_acc = 0.0
        self.update()

    def prime_reading_blocks(self) -> None:
        """先算一遍版面并注册块，不等第一帧（预翻译取的是 mapper.segments()）。"""
        renderer = self._renderer
        if isinstance(renderer, StepRenderer):
            renderer.ensure_screen(max(self.width(), 1), max(self.height(), 1))
            self._notify_step()

    def load_document(self, document: ParsedDocument) -> None:
        self._document = document
        self._rebuild_renderer()
        self._scroll = QPoint(0,0)
        self._step_acc = 0.0
        self.update()# 通知Qt 调度 paintEvent 重画控件

    def _rebuild_renderer(self) -> None:
        if self._document is None:
            self._renderer = None
            return
        if self._step_mode:
            renderer: DocumentRenderer | StepRenderer = StepRenderer(self._document, self._reading)
        else:
            renderer = DocumentRenderer(self._document)
        # 注入 mapper：TXT/Word 渲染期回填 bbox 后能调 register_blocks 重新注册
        # （需求 6.8.8 前置依赖，段落模式对 TXT 生效的唯一通路）
        renderer.set_mapper(self._mapper)
        self._renderer = renderer

    #实现页面滚动
    def _handle_scroll(self,wheel_dx:int,wheel_dy:int) -> None:
        dx = int(-wheel_dx)
        dy = int(-wheel_dy)
        self.scroll_contents(dx,dy)

    # ---- 交互 ------------------------------------------------------------
    def mouseMoveEvent(self, event) -> None:
        if self._gaze_callback is not None:
            pos = event.globalPosition()
            self._gaze_callback(GazePoint(x=pos.x(), y=pos.y()))
        super().mouseMoveEvent(event)

    def wheelEvent(self, event) -> None:
        if self._step_mode:
            self._handle_step_wheel(event)
            event.accept()
            return
        # 滚轮只负责滚动，滚动后同步 mapper.update_viewport
        #super().wheelEvent(event)

        if self._renderer is None:
            super().wheelEvent(event)
            return

        #无滚轮缩放大小
        delta = event.angleDelta() #调用 QWheelEvent 对象的方法，返回一个 QPoint 对象,对象内容是滚轮转动量
        self._handle_scroll(
            delta.x(), #水平滚动量
            delta.y() #垂直滚动量
        )
        event.accept() #将事件的 accepted 标志设为 True，阻止时间转发给父级，避免触发双重滚动

    # ---- 逐句步进 --------------------------------------------------------
    def _handle_step_wheel(self, event) -> None:
        """一格滚轮 = 一句。整格事件不累计余数（否则滚 5 格会多走一步）。"""
        angle = event.angleDelta().y()
        if angle:
            steps = self._angle_to_steps(angle)
        else:
            steps = self._pixel_to_steps(event.pixelDelta().y())
        if steps:
            self.step_reading(steps)

    def _angle_to_steps(self, angle: int) -> int:
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
        if not dy:
            return 0
        self._step_acc += dy
        if abs(self._step_acc) < _WHEEL_PIXEL_STEP:
            return 0
        step = int(self._step_acc / _WHEEL_PIXEL_STEP)
        self._step_acc = 0.0
        return max(-_STEP_MAX_STEPS, min(_STEP_MAX_STEPS, step))

    def step_reading(self, steps: int) -> bool:
        """按句步进（正数向下）。返回是否有过位移。"""
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
        if self._mapper is not None:

            origin = self.mapToGlobal( QPoint(0, 0) )  # Widget 本地坐标系的左上角，将其转换为屏幕绝对坐标

            self._mapper.update_viewport(
                window_origin=(origin.x(), origin.y()), #屏幕绝对位置
                scroll_offset=(self._scroll.x(), self._scroll.y()), #内容滚动偏移
            )

    #将后端渲染器生成的文档图像绘制到界面上，并支持滚动
    def paintEvent(self, event) -> None:
        if self._renderer is None:
            # 无文档时不获取画笔，避免空绘制破坏 Qt 原生态
            return
        self._sync_viewport()

        # 只渲染可视区：返回的图像就是控件大小，滚多深都不会分配超大位图
        img = self._renderer.render_viewport(
            self._scroll.x(), self._scroll.y(), self.width(), self.height()
        )

        h,w = img.shape[:2] #读取图像的宽高 (h, w)
        qimg = QImage(
            img.data, #不会拷贝内容，仅引用
            w,
            h,
            w*3, #每行字节数
            QImage.Format.Format_RGB888 #像素格式，每像素24bit,且按RGB顺序排列
        )
        painter = QPainter(self) #创建画笔并绑定到指定绘图设备
        painter.drawPixmap(0, 0, QPixmap.fromImage(qimg))  # 已是视口切片，不再需要负偏移
        painter.end()
