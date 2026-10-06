from __future__ import annotations

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

from mytraxcure.core.file.file_parser import ParsedDocument
from mytraxcure.core.file.file_viewer import DocumentRenderer
from mytraxcure.core.file.text_mapper import TextMapper
from mytraxcure.core.types import GazePoint


class ViewerView(QWidget):

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent) 
        self._renderer: DocumentRenderer | None = None  #存储加载的文档
        self._mapper: TextMapper | None = None #文本坐标映射器（屏幕→文档命中）
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

    def load_document(self, document: ParsedDocument) -> None:
        self._renderer = DocumentRenderer(document)
        # 注入 mapper：TXT/Word 渲染期回填 bbox 后能调 register_blocks 重新注册
        # （需求 6.8.8 前置依赖，段落模式对 TXT 生效的唯一通路）
        self._renderer.set_mapper(self._mapper)
        self._scroll = QPoint(0,0)
        self.update()# 通知Qt 调度 paintEvent 重画控件

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

    def scroll_contents(self, dx: int, dy: int) -> None:
        if self._scroll_locked:  # 浮层显示期间冻结页面（滚轮/拖动均被拦截）
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
