from __future__ import (
    annotations,  #让你写类型注解（变量或函数的返回类型）时，不用加引号 " "
)

from pathlib import Path

from eyetrax.utils.screen import get_screen_size
from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QCursor, QGuiApplication, QImage, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from mytraxcure.core.config import AppConfig
from mytraxcure.core.fallback import FallbackManager
from mytraxcure.core.file.file_parser import FileParser, ParsedDocument
from mytraxcure.core.file.text_mapper import TextMapper
from mytraxcure.core.gaze import GazeWrapper, SmootherAdapter
from mytraxcure.core.gaze.calibration_store import (
    CalibrationEnvironment,
    CalibrationStore,
)
from mytraxcure.core.gaze.camera import CameraThread
from mytraxcure.core.gaze.startup_calibration import run_and_save
from mytraxcure.core.gaze_logic.gaze_detector import GazeDetector
from mytraxcure.core.gaze_logic.region_locker import RegionLocker
from mytraxcure.core.translation.cache_store import CacheKey, CacheStore
from mytraxcure.core.translation.translation_engine import (
    TranslationEngine,
    TranslationResult,
)
from mytraxcure.core.translation.translation_worker import TranslationWorker
from mytraxcure.core.types import GazePoint, Region
from mytraxcure.ui.feedback_view import FeedbackView
from mytraxcure.ui.gaze_cursor_view import GazeCursorView
from mytraxcure.ui.overlay_view import OverlayMode, OverlayView
from mytraxcure.ui.viewer_view import ViewerView


class MainWindow(QMainWindow):

    def __init__(self, config: AppConfig) -> None:
        super().__init__() #调用父类构造
        self.config = config
        self.setWindowTitle("myTraxCure")
        self._last_hover_key:str | None = None
        # 核心对象
        self.parser = FileParser() #文件解析器
        self.mapper = TextMapper() #文本坐标映射器
        gaze_cfg = config.gaze #眼动相关参数
        self.detector = GazeDetector(
            dwell_seconds= gaze_cfg.dwell_seconds, #凝视判定
            max_variance_px = gaze_cfg.max_variance_px, #视线抖动允许范围
            max_offset_px = gaze_cfg.max_offset_px, #单个点离中心的最大距离
            dominant_ratio=gaze_cfg.dominant_ratio, #用于段落级判定
            min_dominant_frames=gaze_cfg.min_dominant_frames  #切到新段落前必须连续命中 N 帧，防止边界闪烁
        )
        self.locker = RegionLocker(config.region) 
        self._engine: TranslationEngine | None = None #懒加载，用到再加载
        self._document: ParsedDocument | None = None #存当前打开的文档
        self._locked_region: Region | None = None #存当前锁定的矩形选区
        self._cache = CacheStore() #翻译缓存
        self._worker:TranslationWorker | None = None #异步翻译工作线程
        self._translation_key: CacheKey | None =None  #本次任务的缓存键
        self._translation_region: Region | None = None  # 本次任务的浮层位置

        self._build_ui() #界面布局
        self._build_menu() #菜单栏
        self._build_status_bar() #状态栏
        self._gaze_timer = QTimer(self) #创建一个定时器实例
        self._gaze_timer.setInterval(33) #设置定时器触发间隔
        self._gaze_timer.timeout.connect(self._poll_cursor) #当定时器超时时，调用 _poll_cursor 方法
        self._gaze_timer.start() #开始计时循环

        self._camera: CameraThread | None = None
        self._fallback = FallbackManager()
        self._gaze_wrapper: GazeWrapper | None = None
        self._smoother: SmootherAdapter | None = None
        self._gaze_cursor: GazeCursorView | None = None #眼动->屏幕映射

    #抓取鼠标位置作为凝视点数据
    def _poll_cursor(self)->None:
        pos = QCursor.pos()
        gazepoint = GazePoint(
            x = pos.x(),
            y = pos.y()
        )
        self.on_gaze_point(gazepoint)
    #窗口关闭
    def closeEvent(self,event)->None:
        if self._camera is not None:
            self._camera.stop()
            self._camera.wait(1500)
        if self._gaze_wrapper is not None:
            self._gaze_wrapper.close() #释放眼动算法引擎占用的资源
        if self._gaze_cursor is not None:
            self._gaze_cursor.close()
            self._gaze_cursor = None
        super().closeEvent(event) #完成窗口实际销毁
    # ---- 装配 ------------------------------------------------------------
    def _build_ui(self) -> None:
        self._stack = QStackedWidget() #堆叠窗口，一次只显示一个子控件
        self.viewer = ViewerView() #文件查看器控件，显示文档内容区域
        self.viewer.set_mapper(self.mapper)  #坐标映射用刚才创建的那个 mapper 对象
        self.viewer.set_gaze_callback(self.on_gaze_point)  # P0：鼠标移动模拟视线
        self._stack.addWidget(self.viewer) #把 viewer 添加到堆叠窗口中

        self.feedback = FeedbackView(self.viewer) #高亮反馈层，父控件是 viewer
        self.overlay = OverlayView(self.viewer) #译文覆盖层，父控件是 viewer
        
        # 摄像头预览面板：进入布局体系，viewer 自动为它让位（物理上不重叠）
        self._cam_panel = QWidget()
        panel_layout = QVBoxLayout(self._cam_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        self._cam_label = QLabel(self._cam_panel)          # 父控件 = 面板
        self._cam_label.setFixedSize(320, 240)
        self._cam_label.setScaledContents(True)
        self._cam_label.setStyleSheet("background: black; border: 2px solid #555;")
        panel_layout.addWidget(
            self._cam_label,
            0,
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight,  # 钉面板右下角（状态栏正上方）
        )
        self._cam_label.hide()   # 眼动模式开启才显示
        self._cam_size_fixed = False  # 首帧按摄像头宽高比调整尺寸

        self.viewer.installEventFilter(self)
        self.overlay.resize(self.viewer.size())
        self.feedback.resize(self.viewer.size())
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
        self.feedback.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.overlay.hide()

        # 中央区：横向布局 = viewer 栈（stretch=1 吃掉剩余空间）+ 预览面板
        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._stack, 1)
        h.addWidget(self._cam_panel)
        self.setCentralWidget(central) #QMainWindow的内置方法，把中央容器设置为主窗口的中央控件，
    
    #调用eyetrax库执行眼动校准
    def _run_inline_calibration(self, store, env) -> None:
        if self._camera is not None:
            self._camera.stop()
            self._camera.wait(1500)
            self._camera = None
        self._status_bar.showMessage("校准中…跟随屏幕上 9 个点（ESC 取消），界面暂时无响应属正常")
        ok = run_and_save(store, env, self.config.gaze.camera_index)
        self._status_bar.showMessage("校准完成" if ok else "校准已取消/失败，继续未校准运行")
        if ok and self._gaze_wrapper is not None:
            # run_and_save 训练的是它内部的临时 estimator；必须把新 pkl 立即
            # 加载回主线程的 wrapper，否则 wrapper 仍是未 fit 状态（predict 会炸）
            store.load(self._gaze_wrapper, env)
        if ok:
            self._run_noise_tuning()

    #滤波微调：3 个点各盯 0.5s 收集 predict 抖动 → 方差写入 measurementNoiseCov。
    #前提：摄像头空闲（CameraThread 未启动）、wrapper 已加载校准模型。
    def _run_noise_tuning(self) -> None:
        if self._gaze_wrapper is None or self._smoother is None:
            return
        self._status_bar.showMessage("滤波微调中…盯着 3 个点直到消失（ESC 跳过）")
        try:
            tuned = self._smoother.tune(
                self._gaze_wrapper.estimator,
                camera_index=self.config.gaze.camera_index,
            )
        except Exception as exc:
            # 微调失败不能带崩整个程序（Qt 槽里未捕获异常会直接退出进程）
            self._status_bar.showMessage(f"滤波微调失败：{exc}")
            tuned = False
        self._status_bar.showMessage(
            "滤波微调完成" if tuned else "滤波微调失败/跳过，使用默认滤波参数"
        )
    def _on_gaze_frame(self,frame)->None:
        if self._gaze_wrapper is None or self._smoother is None:
            return
        #特征提取
        sample = self._gaze_wrapper.extract(frame)
        #过滤无效帧（丢脸/眨眼 → 红点隐藏，对应 demo 的淡出）
        if sample is None or sample.blink:
            if self._gaze_cursor is not None:
                self._gaze_cursor.clear()
            return
        #坐标预测
        try:
            point = self._gaze_wrapper.predict(sample)
        except Exception:
            return
        #平滑滤波
        x,y = self._smoother.step(point.x,point.y)
        # DPI 换算：eyetrax 校准/预测用物理像素（screeninfo），Qt 全按逻辑像素渲染。
        # 不换算时红点会被放大 dpr 倍 → Windows 缩放≠100% 时红点整体偏右下（y 偏下明显）。
        x, y = self._eyetrax_to_qt(x, y)
        #更新红点映射（必须在 on_gaze_point 之前：浮层显示期间它会提前 return）
        if self._gaze_cursor is not None:
            self._gaze_cursor.set_gaze_point(x, y)
        #复用 P0 主链路（mapper 的 mapToGlobal 也是逻辑坐标，正好统一）
        self.on_gaze_point(GazePoint(x=x, y=y))

    @staticmethod
    def _eyetrax_to_qt(x: float, y: float) -> tuple[float, float]:
        scr = QGuiApplication.primaryScreen()
        dpr = scr.devicePixelRatio() if scr is not None else 1.0
        return x / dpr, y / dpr

    #摄像头预览：把 BGR 帧显示到右侧灰色区域
    def _on_camera_frame(self, frame) -> None:
        if getattr(self, "_cam_label", None) is None or frame is None:
            return
        h, w = frame.shape[:2]
        # 首帧：按摄像头宽高比调整预览尺寸（布局自动重排，viewer 同步变窄）
        if not self._cam_size_fixed:
            self._cam_size_fixed = True
            self._cam_label.setFixedSize(max(160, int(240 * w / h)), 240)
        img = QImage(frame.data, w, h, w * 3, QImage.Format.Format_BGR888)
        self._cam_label.setPixmap(
            QPixmap.fromImage(img).scaled(
                self._cam_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )

    #启动默认模式：程序启动后自动进入眼动。不再弹校准询问——
    #启动前的 ensure_calibration 已完成校准决策；摄像头不可用时 try_enable_gaze 自动留鼠标。
    #tune_noise：本次启动刚做了 9 点校准（NEWLY_CALIBRATED）→ 进眼动前先收集噪声微调滤波
    def start_default_mode(self, tune_noise: bool = False) -> None:
        self._enable_gaze_mode(ask_calibration=False, tune_noise=tune_noise)

    #启动眼动模式
    def _enable_gaze_mode(self,ask_calibration:bool = True, tune_noise:bool = False)->None:
        if self._camera is not None:
            return
        if not self._fallback.try_enable_gaze(self.config.gaze):
            self._status_bar.showMessage(f"眼动不可用：{self._fallback.last_reason}")
            self._mode_label.setText("模式: 鼠标")
            return
        wrapper = self._fallback.gaze_wrapper
        if wrapper is None:
            self._status_bar.showMessage("眼动初始化失败：未获得 GazeWrapper")
            self._fallback.switch_to_mouse("gaze_wrapper is None")
            self._enable_mouse_mode()
            return
        self._gaze_wrapper = wrapper
        self._smoother = SmootherAdapter(
            self.config.gaze.smoother,
            ema_alpha=self.config.gaze.ema_alpha,
        )

        #准备校准环境
        sw,sh = get_screen_size()  # eyetrax 物理分辨率：仅用于 env 三元组命名/校准，勿用于 Qt 坐标
        # GazeCursorView 是 Qt 顶层窗口 → 用 Qt 逻辑几何（物理分辨率会让窗口超出屏幕 dpr 倍）
        _scr = QGuiApplication.primaryScreen()
        if _scr is not None:
            _geo = _scr.geometry()
            self._gaze_cursor = GazeCursorView(_geo.x(), _geo.y(), _geo.width(), _geo.height())
        else:
            self._gaze_cursor = GazeCursorView(0, 0, sw, sh)
        env = CalibrationEnvironment(
            (sw,sh),
            camera_id= self.config.gaze.camera_index,
            user_id = self.config.user_id
        )
        store = CalibrationStore()

        #询问用户是否启动眼动校准
        if ask_calibration:
            has_pkl = store.exists(env) #检查是否存在当前用户/摄像头组合的校准文件
            ans = QMessageBox.question(
                self, "眼动校准",
                "是否先进行 9 点眼动校准？\n\n"
                "[是] 运行校准后启用（全屏窗口跟随 9 个点，ESC 取消）\n"
                "[否] " + ("使用已有校准直接启用" if has_pkl else "跳过校准启用（视线预测不准）"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._run_inline_calibration(store,env)
            
        #加载校准数据（无论选"是"还是"否"都执行）
        if store.exists(env):  #尝试加载已有的校准文件
            store.load(self._gaze_wrapper,env)
            self._status_bar.showMessage("已加载校准模型，眼动模式开启")
        else:
            self._status_bar.showMessage("未校准：视线预测可能不准（重启可重新校准）")

        #启动期刚校准过 → 先收集噪声微调滤波（必须在 CameraThread 启动前：摄像头只能被一方占用）
        if tune_noise:
            self._run_noise_tuning()

        #停止鼠标轮询
        self._gaze_timer.stop()
        self._mode_label.setText("模式: 眼动")

        #实例化一个独立线程，专门负责循环读取摄像头画面
        self._camera = CameraThread(
            self.config.gaze.camera_index,
            parent = self
        )
        self._camera.frame_ready.connect(self._on_gaze_frame)
        self._camera.frame_ready.connect(self._on_camera_frame)
        self._camera.error_occurred.connect(self._on_camera_error)
        self._camera.fps_updated.connect(
            lambda fps: self._fps_label.setText(f"FPS: {fps:.0f}")
        )
        self._cam_label.show()      # 标签可见
        self._cam_panel.show()      # 面板可见（布局立刻把 viewer 压窄，不再重叠）
        self._camera.start()
    
    #摄像头异常,降级到鼠标并提示原因
    def _on_camera_error(self, message: str) -> None:
        self._fallback.switch_to_mouse(message)
        self._status_bar.showMessage(f"摄像头错误：{message}，已降级鼠标模式")
        self._enable_mouse_mode()

    #切回鼠标模式
    def _enable_mouse_mode(self) -> None:
        if self._camera is not None:
            self._camera.stop()      # 只置标志位，run() 循环退出后自动 release()
            self._camera.wait(1500)  # 等 run() 真正退出（含释放摄像头）
            self._camera = None
        self._gaze_wrapper = None
        self._smoother = None
        self._fps_label.setText("FPS: -")
        self._gaze_timer.start()     # 恢复鼠标轮询（S2 行为）
        self._mode_label.setText("模式: 鼠标")
        if self._gaze_cursor is not None:
            self._gaze_cursor.close()
            self._gaze_cursor = None
        if getattr(self, "_cam_panel", None) is not None:
            self._cam_panel.hide()   # 布局立刻把空间还给 viewer
        if getattr(self, "_cam_label", None) is not None:
            self._cam_label.hide()
            self._cam_label.clear()
    def _build_menu(self) -> None:
        #添加文件按钮
        menu_bar = self.menuBar()
        assert menu_bar is not None  # QMainWindow 首次调用 menuBar() 必然创建菜单栏
        menu = menu_bar.addMenu("文件(&F)")
        assert menu is not None  # addMenu 对非空菜单栏必然返回有效 QMenu
        open_action = QAction("打开文件…", self)
        open_action.triggered.connect(self._open_file_dialog)
        menu.addAction(open_action)

        #添加“模式”按钮
        mode_menu = menu_bar.addMenu("模式(&M)")
        assert mode_menu is not None
        gaze_action = QAction("启动眼动",self)
        gaze_action.triggered.connect(
            lambda checked = False: self._enable_gaze_mode(True)
            )
        mode_menu.addAction(gaze_action)

        mouse_action = QAction("鼠标模式", self)
        mouse_action.triggered.connect(self._enable_mouse_mode)
        mode_menu.addAction(mouse_action) 

        #recal_action = QAction("重新校准…", self)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        assert bar is not None  # QMainWindow 首次调用 statusBar() 必然创建状态栏
        self._status_bar: QStatusBar = bar
        self._mode_label = QLabel("模式: 鼠标")
        self._fps_label = QLabel("FPS: -")
        self._trans_label = QLabel("翻译: 空闲")
        bar.addWidget(self._mode_label)
        bar.addPermanentWidget(self._fps_label)
        bar.addPermanentWidget(self._trans_label)

    # ---- 文件 ------------------------------------------------------------
    def _open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "打开文件")
        if not path:
            return
        self.load_file(Path(path))

    #加载文档并初始化所有相关状态
    def load_file(self, path: Path) -> None:
        self._document = self.parser.parse(path)
        self.mapper.register_blocks(self._document.blocks)#注册文本块到映射器
        self.detector.reset() #重置眼动检测器
        self._locked_region = None #清空锁定区域
        self.viewer.load_document(self._document) #视图层加载文档
        self._status_bar.showMessage(f"已加载: {path.name}")

    # ---- 视线 / 鼠标 -----------------------------------------------------
    def on_gaze_point(self, point: GazePoint) -> None:
        gaze_cfg = self.config.gaze

        # 浮层显示期间冻结凝视交互：只有空格/Esc 能关闭译文，
        # 鼠标移动不再清空选区/高亮或触发新锁定
        if self.overlay.is_showing:
            return

        # 0. 遮挡/失焦 → 暂停采样
        if self.mapper.is_occluded((point.x, point.y)):
            return

        # 1~2. 带容差命中测试（校准误差未知时先用配置默认值）
        # block = self.mapper.hit_test(
        #     (point.x, point.y), tolerance_px=gaze_cfg.hit_tolerance_px
        # )

        #带容差命中（需求 6.8.6 两段式）：视线抖动 + 校准误差下，精确命中(tolerance=0)几乎必失败。
        #v6：校准误差不可获取 → 容差恒用 GazeConfig.hit_tolerance_px
        block = self.mapper.hit_test(
            (point.x, point.y),
            tolerance_px=gaze_cfg.hit_tolerance_px,
        )
        #未命中才做"最近块回退"
        if block is None and self.mapper._blocks:
            dx,dy = self.mapper.screen_to_document((point.x,point.y))
            nearest = min(
                self.mapper._blocks,
                key = lambda b : b.distance_to(dx,dy)
            )
            tol = min(
                gaze_cfg.hit_tolerance_px, #默认容差
                nearest.h * gaze_cfg.tolerance_ratio_of_block_h #块高度乘以比例系数
            )
            if nearest.distance_to(dx,dy) <= tol:
                block = nearest
            
        # 3. 段落聚合 + 并集 bbox 文档坐标 → 屏幕（全局）坐标
        group = self.mapper.paragraph_union_bbox(block) if block is not None else None
        screen_region: Region | None = None
        if group is not None:
            x0, y0 = self.mapper.document_to_screen((group.bbox.x, group.bbox.y))
            x1, y1 = self.mapper.document_to_screen((group.bbox.right, group.bbox.bottom))
            screen_region = Region(
                x=int(x0), y=int(y0), w=int(x1 - x0), h=int(y1 - y0)
            )

        # 4. 凝视稳定判定（block 为 None 时 block_key=None，detector 自动走点模式）
        stability = self.detector.update(
            point,
            block_key=group.para_key if group else None,
            block_height_px=float(screen_region.h) if screen_region else 0.0,
        )

        #视线（凝视）交互
        if not stability.stable: 
            hover_key = group.para_key if group else None #获取当前凝视所指的“段落键”
            
            #比较当前悬停的段落键与上一次记录的最后悬停键是否不同
            if hover_key != self._last_hover_key: 
                self._locked_region = None
                self.feedback.hide_region()

            self._last_hover_key = hover_key
            return

        # 5. 双模式选路。feedback/overlay 是 viewer 的子控件 → 矩形用 viewer 本地坐标
        origin = self.viewer.mapToGlobal(QPoint(0, 0))
        bounds = (0, 0, self.viewer.width(), self.viewer.height())

        region: Region | None = None
        if stability.mode == "paragraph" and screen_region is not None:
            local = Region(
                x=screen_region.x - origin.x(),
                y=screen_region.y - origin.y(),
                w=screen_region.w,
                h=screen_region.h,
            )
            region = self.locker.lock_block(local, bounds=bounds)
            # 返回 None = 段落并集超限（"整页一大块"）→ 落到点模式回退
        if region is None:
            local_point = GazePoint(
                x=point.x - origin.x(),
                y=point.y - origin.y(),
                timestamp=point.timestamp,
            )
            region = self.locker.lock(
                local_point,
                calibration_error_px=None,
                zoom=1.0,  # v6.2：不做缩放，视口缩放恒为 1.0
                bounds=bounds,
            )

        self._locked_region = region
        self.feedback.show_region(region)
        self.detector.reset()  # 0.8s 只触发一次：锁定后清窗重新计

    def mouseMoveEvent(self, event) -> None:
        pos = event.globalPosition()
        self.on_gaze_point(GazePoint(x=pos.x(), y=pos.y()))

    # ---- 键盘 ------------------------------------------------------------
    def on_space(self) -> None:
        if self.overlay.is_showing:
            # 显示/加载中都允许空格关闭；清掉任务键后，worker 完成回调会
            # 因 key/region 为 None 直接返回，取消的结果不会写缓存
            self.overlay.hide_overlay()
            self.viewer.set_scroll_locked(False)  # 解锁滚动
            self._translation_key = None
            self._translation_region = None
            return
        if self._locked_region is None:
            return
        self._trigger_translation(self._locked_region)

    def on_esc(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self.overlay.hide_overlay()
        self.viewer.set_scroll_locked(False)
        self._translation_key = None
        self._translation_region = None
        self._trans_label.setText("翻译: 空闲")

    # ---- 翻译 ------------------------------------------------------------
    def _get_engine(self) -> TranslationEngine:
        if self._engine is None:
            from mytraxcure.core.translation.engine_factory import create_engine

            self._engine = create_engine(self.config.translation)
        return self._engine

    #显示译文 + 主线程写缓存 + 更新状态栏
    def _on_translation_done(self, result: TranslationResult) -> None:
        key = self._translation_key
        region = self._translation_region
        if key is None or region is None:
            return
        #显示译文（与缓存开关无关，否则关缓存时翻译完成不显示）
        self.overlay.show_translation(region, result, OverlayMode.TRANSLUCENT)
        self.viewer.set_scroll_locked(True)  # 浮层显示期间锁定滚动
        self._trans_label.setText(f"翻译: 完成 {result.elapsed_ms}ms")
        #缓存开启且非敏感文档 -> 写入缓存
        if self.config.cache.enabled and not self.config.sensitive_documents:
            self._cache.put(key, result, file_hash=None)
        self._translation_key = None
        self._translation_region = None
    
    #隐藏浮层 + 状态栏报错（不写缓存）
    def _on_translation_failed(self, message: str) -> None:
        self.overlay.hide_overlay() #：隐藏翻译浮层
        self.viewer.set_scroll_locked(False)  # 解锁滚动
        self._trans_label.setText("翻译: 失败")
        self._status_bar.showMessage(f"翻译失败: {message}")
        self._translation_key = None
        self._translation_region = None

    def _trigger_translation(self, region: Region) -> None:
        origin = self.viewer.mapToGlobal(QPoint(0,0)) #将部件内本地坐标映射为全局屏幕坐标
        global_region = Region(
            x = region.x + origin.x(),
            y=region.y + origin.y(),
            w=region.w,
            h=region.h,
        )
        #获取区域内的文本块
        blocks = self.mapper.blocks_in_region(global_region)
        if not blocks:
            if not blocks:
                self._status_bar.showMessage("未检测到可翻译文本")
            return
        text = "".join(b.text for b in blocks)
        cfg = self.config.translation
        
        #唯一标识一条翻译缓存
        key = CacheKey(
            source_text = text,
            source_lang = cfg.source_lang,
            target_lang = cfg.target_lang,
            backend=self._get_engine().name,
            model=cfg.model,
            page=blocks[0].page,
            block_id=blocks[0].block_id
        )

        #检查全局缓存开关是否启用
        if self.config.cache.enabled:
            cached = self._cache.get(key)
            #缓存命中,显示浮层展示翻译结果
            if cached is not None:
                self.overlay.show_translation(
                    region,
                    cached,
                    OverlayMode.TRANSLUCENT
                )
                self.viewer.set_scroll_locked(True)  # 浮层显示期间锁定滚动
                self._trans_label.setText(f"翻译: 缓存 {cached.elapsed_ms}ms")
                return
        #启动工作线程
        if self._worker is None:
            self._worker = TranslationWorker(
                self._get_engine(),
                cfg,
                parent = self
            )
            self._worker.finished.connect(self._on_translation_done)
            self._worker.failed.connect(self._on_translation_failed)
        self._translation_key = key
        self._translation_region = region
        self.overlay.show_loading(region)
        self.viewer.set_scroll_locked(True)  # 浮层显示期间锁定滚动
        self._trans_label.setText("翻译: 进行中…")
        self._worker.request(text) #调用工作线程的 request 方法，传入源文本，启动线程执行翻译

    #----------------事件分发---------------------
    #键盘响应
    def keyPressEvent(self,event)->None:
        if event.isAutoRepeat():  # 过滤按住空格的自动重复，避免显示/隐藏来回闪烁
            event.accept()
            return
        if event.key() == Qt.Key.Key_Space:
            self.on_space()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.on_esc()
            event.accept()
        else:
            super().keyPressEvent(event)
    
    def eventFilter(self,obj,event)->bool:
        if obj is self.viewer:
            if event.type() == event.Type.KeyPress:
                if event.isAutoRepeat():
                    return True  # 过滤自动重复，避免显示/隐藏来回闪烁
                if event.key() == Qt.Key.Key_Space:
                    self.on_space()
                    return True
                if event.key() == Qt.Key.Key_Escape:
                    self.on_esc()
                    return True
            elif event.type() == event.Type.Resize:
                self.overlay.resize(self.viewer.size())
                self.feedback.resize(self.viewer.size())
        return super().eventFilter(obj,event) #不关心的事件（如鼠标点击）交给父类默认处理
                    
