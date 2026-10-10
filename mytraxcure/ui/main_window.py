"""模块名称：main_window.py

功能描述：应用的主窗口与全局编排中枢。装配各子视图（viewer / overlay / feedback / 光标层），
持有配置、映射器、检测器、缓存与翻译线程，把"输入的注视点"一路编排成"落位的译文浮层"。

关键约束：
- 坐标有三套：屏幕全局（Qt 逻辑像素）、viewer 本地（浮层/高亮是 viewer 的子控件，
  用的是 viewer 相对坐标）、文档坐标（命中与聚合）。三者转换只走 TextMapper 与
  mapToGlobal，不要就地手算偏移。
- eyetrax 的校准与预测用物理像素，Qt 全按逻辑像素渲染，两域之间必须经
  _eyetrax_to_qt 换算；漏换算时 Windows 缩放不等于 100% 会让红点整体偏右下。
- 本模块大量代码跑在 Qt 的槽与定时器回调里，这些位置抛出的异常不会有人接，
  会直接结束进程，因此每个外部 API 调用点都就地兜住。
- 眼动模式独占摄像头：噪声微调必须在 CameraThread 启动之前做，画面预览与 gaze
  预测共用同一个 frame_ready 信号。
"""

from __future__ import (
    annotations,  #延迟注解求值：让 X | None 这类写法在运行时不被当作表达式求值
)

from pathlib import Path

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

from mytraxcure.core.config import AppConfig, ConfigManager
from mytraxcure.core.fallback import FallbackManager
from mytraxcure.core.file.file_parser import FileParser, ParsedDocument
from mytraxcure.core.file.segmenter import make_ocr_notices, segment_blocks
from mytraxcure.core.file.text_mapper import TextMapper
from mytraxcure.core.gaze import NOISE_TUNE_POINTS, GazeWrapper, SmootherAdapter
from mytraxcure.core.gaze.calibration_runner import describe_mode
from mytraxcure.core.gaze.calibration_store import (
    CalibrationEnvironment,
    CalibrationStore,
)
from mytraxcure.core.gaze.camera import CameraThread
from mytraxcure.core.gaze.screen_utils import primary_screen_size
from mytraxcure.core.gaze.startup_calibration import run_and_save
from mytraxcure.core.gaze_logic.gaze_detector import GazeDetector
from mytraxcure.core.gaze_logic.region_locker import RegionLocker
from mytraxcure.core.translation.cache_store import (
    CacheKey,
    CacheStore,
    make_segment_key,
)
from mytraxcure.core.translation.engine_factory import create_engine
from mytraxcure.core.translation.pre_translator import PreTranslator
from mytraxcure.core.translation.translation_engine import (
    TranslationEngine,
    TranslationResult,
)
from mytraxcure.core.translation.translation_worker import TranslationWorker
from mytraxcure.core.types import GazePoint, Region
from mytraxcure.ui.feedback_view import FeedbackView
from mytraxcure.ui.gaze_cursor_view import GazeCursorView
from mytraxcure.ui.gaze_settings_dialog import GazeSettingsDialog
from mytraxcure.ui.overlay_view import OverlayMode, OverlayView
from mytraxcure.ui.viewer_view import ViewerView

# 逐句模式的翻句键（长按连发不拦，作为滚轮之外的第二条通路）
_STEP_DOWN_KEYS = (Qt.Key.Key_Down, Qt.Key.Key_PageDown)
_STEP_UP_KEYS = (Qt.Key.Key_Up, Qt.Key.Key_PageUp)
_STEP_KEYS = _STEP_DOWN_KEYS + _STEP_UP_KEYS


class MainWindow(QMainWindow):
    """主窗口：整个阅读链路的编排者，Qt 对象树的根。

    层级位置：QMainWindow 的直接实例，中央区是 ViewerView（外加摄像头面板），
    浮层 OverlayView 与高亮 FeedbackView 都是 viewer 的子控件——因此它们用的矩形
    是 viewer 本地坐标，不是屏幕坐标。

    状态机：输入模式在"鼠标轮询"与"眼动"之间二选一（互斥使用 _gaze_timer 与
    CameraThread），阅读模式在"逐句步进"与"连续滚动"之间二选一。两条轴线独立切换，
    但切换阅读模式要重置检测窗与锁定状态，因为块粒度变了。
    """

    def __init__(
        self, config: AppConfig, config_manager: ConfigManager | None = None
    ) -> None:
        """建立主窗口：造齐核心对象、搭界面、启动鼠标轮询定时器。

        Args:
            config: 应用配置，各阈值的唯一来源。
            config_manager: 配置管理器，用于把界面上的改动落盘；传 None 表示不落盘。
        """
        super().__init__()
        self.config = config
        self.config_manager = config_manager
        self.setWindowTitle("myTraxCure")
        self._last_hover_key:str | None = None
        # 核心对象
        self.parser = FileParser() #文件解析器
        self.mapper = TextMapper() #文本坐标映射器
        gaze_cfg = config.gaze
        # 阈值全部显式注入而非让 GazeDetector 用默认值：那里的默认值与 GazeConfig 的
        # 实测放宽值不一致，默认值一旦发生作用，驻留判定的手感会随调用方式漂移
        self.detector = GazeDetector(
            dwell_seconds= gaze_cfg.dwell_seconds, #凝视判定
            max_variance_px = gaze_cfg.max_variance_px, #视线抖动允许范围
            max_offset_px = gaze_cfg.max_offset_px, #单个点离中心的最大距离
            dominant_ratio=gaze_cfg.dominant_ratio, #用于段落级判定
            min_dominant_frames=gaze_cfg.min_dominant_frames  #切到新段落前必须连续命中 N 帧，防止边界闪烁
        )
        self.locker = RegionLocker(config.region)
        self._engine: TranslationEngine | None = None #懒加载：翻译引擎会拉起外部依赖
        self._document: ParsedDocument | None = None #存当前打开的文档
        self._locked_region: Region | None = None #存当前锁定的矩形选区
        self._cache = CacheStore() #翻译缓存
        self._worker:TranslationWorker | None = None #异步翻译工作线程
        self._translation_key: CacheKey | None =None  #本次任务的缓存键
        self._translation_region: Region | None = None  # 本次任务的浮层位置

        self._build_ui() #界面布局
        self._build_menu() #菜单栏
        self._build_status_bar() #状态栏
        self._gaze_timer = QTimer(self) #鼠标轮询定时器：鼠标模式下模拟 ~30fps 的注视流
        self._gaze_timer.setInterval(33) #约 30fps：再高没必要，更低则驻留判定的时间分辨率偏钝
        self._gaze_timer.timeout.connect(self._poll_cursor)
        self._gaze_timer.start()

        self._camera: CameraThread | None = None
        self._fallback = FallbackManager()
        self._gaze_wrapper: GazeWrapper | None = None
        self._smoother: SmootherAdapter | None = None
        self._gaze_cursor: GazeCursorView | None = None #眼动->屏幕映射
        self._locked_para_key: str | None = None #当前锁定段落（段落模式下翻译取文本用）
        self._pre_translator: PreTranslator | None = None #后台预翻译线程

    def _poll_cursor(self)->None:
        """定时器槽：抓鼠标位置当凝视点，喂进与眼动完全相同的主链路。"""
        pos = QCursor.pos()
        gazepoint = GazePoint(
            x = pos.x(),
            y = pos.y()
        )
        self.on_gaze_point(gazepoint)

    def closeEvent(self,event)->None:
        """窗口关闭：按"先停翻译、再停采集、最后放资源"的顺序收尾。

        Qt 在用户点关闭、Alt+F4、进程退出时都会调用它，是所有后台线程的唯一收口。
        顺序不能反：camera.stop() 后置 None 之前必须 wait()，否则 QThread 对象被
        提前销毁会触发 "QThread: Destroyed while thread is still running"。

        Args:
            event: Qt 的关闭事件。
        """
        self._stop_pre_translation()
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
        """搭出中央区：viewer 栈 + 摄像头面板，并挂上浮层与高亮层。

        Notes:
            浮层与高亮都以 viewer 为父控件，这样它们随 viewer 一起搬迁，矩形也统一用
            viewer 本地坐标；同时设为鼠标事件透明，避免吃掉触发它们自己的输入。
            给 viewer 装了 eventFilter：聚焦在 viewer 上的按键（空格/Esc/方向键）不会
            自动冒泡到主窗口，必须靠过滤器转发。
        """
        self._stack = QStackedWidget() #堆叠窗口，一次只显示一个子控件
        self.viewer = ViewerView() #文件查看器控件，显示文档内容区域
        self.viewer.set_mapper(self.mapper)  #坐标映射用刚才创建的那个 mapper 对象
        self.viewer.set_gaze_callback(self.on_gaze_point)  # P0：鼠标移动模拟视线
        self.viewer.set_reading_config(self.config.reading)  # 逐句步进参数
        self.viewer.set_reading_mode(self.config.reading.step_mode)
        self.viewer.set_step_callback(self._on_reading_step)  # 换屏 → 更新阅读进度
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
        self.setCentralWidget(central)

    def _run_inline_calibration(self, store, env) -> None:
        """在当前线程里跑一次 eyetrax 校准并把结果写回 wrapper。

        Args:
            store: 校准存储，负责保存与后续加载 pkl。
            env: 校准环境（屏幕分辨率 / 摄像头 id / 用户 id）。

        Notes:
            摄像头只能被一方占用，所以先停掉 CameraThread 再开 cv2 全屏窗口；
            校准期间界面无响应是有意为之——eyetrax 的全屏交互窗口要独占主线程。
            跑完必须立刻把新 pkl 加载回主线程的 wrapper：run_and_save 训练的是它内部
            的临时 estimator，不重载的话 wrapper 仍是未 fit 状态，predict 会抛。

            Qt 槽里抛出的异常会直接结束进程，所以整段兜在 try 里，失败只降级为
            "未校准继续运行"。
        """
        if self._camera is not None:
            self._camera.stop()
            self._camera.wait(1500)
            self._camera = None
        mode = self.config.calibration.mode
        self._status_bar.showMessage(
            f"校准中…跟随屏幕上的{describe_mode(mode)}校准点（ESC 取消），界面暂时无响应属正常"
        )
        try:
            report = run_and_save(
                store,
                env,
                self.config.gaze.camera_index,
                self.config.calibration,
                self.config.gaze,
            )
        except Exception as exc:
            # 摄像头/建窗/模型训练都可能抛；Qt 槽里未捕获异常会直接结束进程
            report = None
            self._status_bar.showMessage(f"校准失败：{exc}")
            return
        self._status_bar.showMessage(
            f"校准完成：{report.summary()}"
            if report is not None
            else "校准已取消/失败，继续未校准运行"
        )
        if report is not None and self._gaze_wrapper is not None:
            # run_and_save 训练的是它内部的临时 estimator；必须把新 pkl 立即
            # 加载回主线程的 wrapper，否则 wrapper 仍是未 fit 状态（predict 会炸）
            store.load(self._gaze_wrapper, env)
        if report is not None:
            self._run_noise_tuning()

    def _run_noise_tuning(self) -> None:
        """滤波微调：让人依次盯几个定点，收集 predict 抖动算方差写回 noise covariance。

        前置条件是摄像头空闲（CameraThread 未启动）且 wrapper 已加载校准模型，
        任一不满足就静默跳过——微调失败不影响眼动本身可用。

        Notes:
            同样要兜异常：参数再怎么不合适也只是"滤波不该这样调"，不能带崩主流程。
        """
        if self._gaze_wrapper is None or self._smoother is None:
            return
        self._status_bar.showMessage(
            f"滤波微调中…依次盯着 {NOISE_TUNE_POINTS} 个点直到它们消失（ESC 跳过）"
        )
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
        """摄像头每帧的处理链路：特征提取 → 有效性过滤 → 预测 → 滤波 → 喂给主链路。

        Args:
            frame: BGR 帧（numpy 数组），由 CameraThread 通过帧就绪信号送来。

        Notes:
            这是本模块对外的第一条入口；跑在信号回调里而非主线程定时任务里。
            丢帧（丢脸/眨眼/转头）都必须先更新红点状态再 return，否则红点会停在最后
            一个有效位置上，看上去像"卡住了"而不是"没看到你"。
            predict 单独兜异常：模型未 fit 或内部 shapes 对不上都可能抛，
            而这里一帧出错不代表下一帧不能用。
        """
        if self._gaze_wrapper is None or self._smoother is None:
            return
        sample = self._gaze_wrapper.extract(frame) #特征提取
        #过滤无效帧（丢脸/眨眼 → 红点隐藏，对应 demo 的淡出）
        if sample is None or sample.blink:
            if self._gaze_cursor is not None:
                self._gaze_cursor.clear()
            return
        #头部姿态门控：yaw/pitch 超阈值（转头看别处）时本帧丢弃，与眨眼同处理。
        #不判 roll：eyetrax 正脸 roll≈±π 且环绕跳变，绝对阈值会永久拒帧
        if not self.detector.head_angle_allowed(
            self.config.gaze.max_head_angle_deg, sample.head_pose
        ):
            if self._gaze_cursor is not None:
                self._gaze_cursor.clear()
            self.detector.reset()  # 清窗：不许转头前后的点跨间隙凑成一次驻留
            return
        #坐标预测
        try:
            point = self._gaze_wrapper.predict(sample)
        except Exception:
            return
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
        """eyetrax 的物理像素坐标 → Qt 的逻辑像素坐标。

        Args:
            x: eyetrax 输出的横坐标（物理像素）。
            y: eyetrax 输出的纵坐标（物理像素）。

        Returns:
            同一点在 Qt 逻辑坐标下的 (x, y)。

        Notes:
            eyetrax 的校准与预测都基于 screeninfo 给的物理分辨率，Qt 全链路按逻辑像素
            渲染，两者差一个 devicePixelRatio。Windows 缩放不是 100% 时必须换算，
            否则红点整体偏右下、凝视永远打不中文字。
        """
        scr = QGuiApplication.primaryScreen()
        dpr = scr.devicePixelRatio() if scr is not None else 1.0
        return x / dpr, y / dpr

    def _on_camera_frame(self, frame) -> None:
        """摄像头预览：把 BGR 帧缩放到预览标签里。

        Args:
            frame: BGR 帧（numpy 数组）；为 None 时丢弃。

        Notes:
            首帧才定预览尺寸：摄像头宽高比要拿到第一帧才知道，按它调整后布局会自动把
            viewer 压窄一层，预览与正文互不遮挡。
            QImage 复用 numpy 的内存，只在本次 setPixmap 期间有效。
        """
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

    def start_default_mode(self, tune_noise: bool = False) -> None:
        """启动后自动进眼动：不再弹校准询问，由启动期决策结果决定。

        Args:
            tune_noise: 本次启动刚做完校准时为 True，进眼动前先做一轮噪声微调。

        Notes:
            校准决策已在启动前的 ensure_calibration 完成；摄像头不可用时
            try_enable_gaze 会自己退回鼠标模式，这里不需要再问一次。
        """
        self._enable_gaze_mode(ask_calibration=False, tune_noise=tune_noise)

    def _enable_gaze_mode(self,ask_calibration:bool = True, tune_noise:bool = False)->None:
        """切到眼动模式：拿 Wrapper、按需校准、建光标层、起摄像头线程。

        Args:
            ask_calibration: 是否先弹校准询问；程序自动启动时传 False。
            tune_noise: 是否在启动采集前做一轮滤波微调。

        Notes:
            整条链路的失败都必须降级而非抛出：拿不到 Wrapper 就退回鼠标模式并给出原因，
            因为这里是唯一的 Qt 菜单回调出口，异常会导致进程直接退出。
            摄像头是独占资源，噪声微调必须排在 CameraThread 启动之前。
        """
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
        self._smoother = SmootherAdapter(ema_alpha=self.config.gaze.ema_alpha)

        #准备校准环境
        sw,sh = primary_screen_size()  # 主屏物理分辨率：校准与 cv2 全屏窗口都在主屏，坐标才对得上
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

        # 是否弹校准询问由调用方决定：菜单手动入口传 True，程序自动启动传 False
        if ask_calibration:
            has_pkl = store.exists(env) #检查是否存在当前用户/摄像头组合的校准文件
            mode_label = describe_mode(self.config.calibration.mode)
            ans = QMessageBox.question(
                self, "眼动校准",
                f"是否先进行{mode_label}眼动校准？\n\n"
                f"[是] 运行校准后启用（全屏窗口跟随{mode_label}校准点，ESC 取消）\n"
                "[否] " + ("使用已有校准直接启用" if has_pkl else "跳过校准启用（视线预测不准）"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._run_inline_calibration(store,env)

        # 选"是"还是"否"都要走这一步：前者刚生成 pkl，后者要用旧 pkl
        if store.exists(env):  #尝试加载已有的校准文件
            store.load(self._gaze_wrapper,env)
            self._status_bar.showMessage("已加载校准模型，眼动模式开启")
        else:
            self._status_bar.showMessage("未校准：视线预测可能不准（重启可重新校准）")

        #启动期刚校准过 → 先收集噪声微调滤波（必须在 CameraThread 启动前：摄像头只能被一方占用）
        if tune_noise:
            self._run_noise_tuning()

        # 两个数据源互斥：停下鼠标轮询，凝视点改由摄像头帧驱动
        self._gaze_timer.stop()
        self._mode_label.setText("模式: 眼动")

        # 摄像头采集必须离开主线程：OpenCV 的 read() 会阻塞几十毫秒
        self._camera = CameraThread(
            self.config.gaze.camera_index,
            parent = self
        )
        self._camera.frame_ready.connect(self._on_gaze_frame)
        # 同一个帧信号接两个槽：预测链路与预览互不阻塞，各走各的
        self._camera.frame_ready.connect(self._on_camera_frame)
        self._camera.error_occurred.connect(self._on_camera_error)
        self._camera.fps_updated.connect(
            lambda fps: self._fps_label.setText(f"FPS: {fps:.0f}")
        )
        self._cam_label.show()      # 标签可见
        self._cam_panel.show()      # 面板可见（布局立刻把 viewer 压窄，不再重叠）
        self._camera.start()

    def _on_camera_error(self, message: str) -> None:
        """摄像头出错：走 FallbackManager 登记降级原因，再整体切回鼠标模式。

        Args:
            message: 摄像头线程给出的错误描述，会一并显示给用户。
        """
        self._fallback.switch_to_mouse(message)
        self._status_bar.showMessage(f"摄像头错误：{message}，已降级鼠标模式")
        self._enable_mouse_mode()

    def _enable_mouse_mode(self) -> None:
        """切回鼠标模式：停采集、放资源、把空间还给 viewer、重启鼠标轮询。

        Notes:
            stop() 只置标志位，必须 wait() 等 run() 真正退出并释放摄像头，
            否则立刻再开眼动模式会因设备仍被占用而失败。
            gaze_wrapper 与 smoother 一并置空是刻意的：它们的内部状态绑定这一次
            校准这一路设备，留着会让下一次启用用到过期模型。
        """
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
        """搭菜单栏：文件 / 模式 / 设置三组动作，全部连到本类的槽。

        Notes:
            addMenu / statusBar 这类工厂方法的返回值在 PyQt6 的类型桩里标成了可能为空，
            运行期其实必然非空，用 assert 既消除类型告警又表达了"这里不会失败"的判断。
            断言会在 python -O 下被剥离，不会影响发布版性能。
        """
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

        # 阅读方式：逐句步进（一屏 N 句，滚轮一次一句）/ 连续滚动
        self._step_action = QAction("逐句步进阅读", self)
        self._step_action.setCheckable(True)
        self._step_action.setChecked(bool(self.config.reading.step_mode))
        self._step_action.toggled.connect(self._set_reading_mode)
        mode_menu.addAction(self._step_action)

        #添加“设置”菜单
        settings_menu = menu_bar.addMenu("设置(&S)")
        assert settings_menu is not None
        gaze_settings_action = QAction("眼动滤波…", self)
        gaze_settings_action.triggered.connect(self._open_gaze_settings)
        settings_menu.addAction(gaze_settings_action)

        # 重新校准的入口曾经在这里，目前改由启动流程决定是否校准，
        # 界面侧保留这段位置便于需要时重新挂上去

    def _open_gaze_settings(self) -> None:
        """打开滤波设置对话框：滑条拖动即生效（红点可见便于当场判断），确定才落盘。"""
        dialog = GazeSettingsDialog(
            self.config,
            config_manager=self.config_manager,
            on_ema_changed=self._apply_ema_alpha,
            parent=self,
        )
        dialog.exec()  # 模态执行：返回时对话框内部的还原/落盘逻辑已经走完

    def _apply_ema_alpha(self, alpha: float) -> None:
        """把对话框的新 alpha 推给正在运行的平滑器。

        Args:
            alpha: 新的平滑系数。

        Notes:
            平滑器不在状态时静默忽略：此刻没有红点反馈，改了也看不出效果，
            而配置值本身已经被对话框写进内存。
        """
        if self._smoother is not None:
            self._smoother.set_ema_alpha(alpha)

    def _on_reading_step(self, screen, total: int) -> None:
        """换屏回调：把"当前读到第几句"写进状态栏常驻标签。

        Args:
            screen: 新的 StepScreen，取它的首句下标与本屏句数。
            total: 全篇句数。

        Notes:
            用常驻标签而不是 showMessage：临时消息几十毫秒后就被下一次提示盖掉，
            而逐句模式一屏只有一两句，进度恰恰需要一直看得见。
        """
        last = min(screen.top + screen.n, total)
        percent = int(round(last / total * 100)) if total else 0
        self._reading_label.setText(
            f"阅读: 第 {screen.top + 1}–{last} 句 / 共 {total} 句（{percent}%）"
        )

    def _set_reading_mode(self, step: bool) -> None:
        """切换阅读方式：逐句步进（句子为单位）↔ 连续滚动（段落为单位）。

        Args:
            step: True 切到逐句步进。

        Notes:
            块粒度一变，检测窗 / 选区 / 悬停状态全部失效，必须逐项清空——
            检测器窗口里还留着句子级的 key，会让下一次判定直接命中旧 key。
            切回连续滚动时要重注册整篇段落块，因为步进模式期间 mapper 里装的是句子块。
        """
        self.config.reading.step_mode = bool(step)
        self.viewer.set_reading_mode(bool(step))
        #块粒度变了（句子 ↔ 段落），检测窗/选区/悬停状态全部失效
        self.detector.reset()
        self._last_hover_key = None
        self._locked_region = None
        self._locked_para_key = None
        self.feedback.hide_region()
        if self._document is not None and not step:
            #切回连续滚动：整篇段落块重新注册（步进模式注册的是句子块）
            self.mapper.register_blocks(self._document.blocks)
        self.viewer.prime_reading_blocks()
        self.viewer.update()
        if not step:
            self._reading_label.setText("阅读: 连续滚动")
        self._status_bar.showMessage("阅读方式：逐句步进（滚轮一次一句）" if step else "阅读方式：连续滚动")

    def _build_status_bar(self) -> None:
        """搭状态栏：四个常驻标签（模式 / 阅读进度 / FPS / 翻译状态）。

        Notes:
            四个都用 addPermanentWidget：临时消息走 showMessage 会把普通控件盖掉，
            而这几项是需要一直盯着的运行态，不能被一次提示挤没。
        """
        bar = self.statusBar()
        assert bar is not None  # QMainWindow 首次调用 statusBar() 必然创建状态栏
        self._status_bar: QStatusBar = bar
        self._mode_label = QLabel("模式: 鼠标")
        self._reading_label = QLabel("阅读: 连续滚动")  # 逐句模式下显示"第 i–j 句 / 共 n 句"
        self._fps_label = QLabel("FPS: -")
        self._trans_label = QLabel("翻译: 空闲")
        # 模式与阅读进度用常驻控件：addWidget 的会被 statusBar().showMessage() 临时盖掉，
        # 而"读到哪一句"恰恰是随时要有、不能被提示消息挤没的信息
        bar.addPermanentWidget(self._mode_label)
        bar.addPermanentWidget(self._reading_label)
        bar.addPermanentWidget(self._fps_label)
        bar.addPermanentWidget(self._trans_label)

    # ---- 文件 ------------------------------------------------------------
    def _open_file_dialog(self) -> None:
        """弹文件选择框，把选中的路径交给 load_file。

        Raises:
            ValueError: 由 FileParser 抛出，扩展名不在白名单时直接透传给调用方
                （菜单回调里未捕获）。
        """
        path, _ = QFileDialog.getOpenFileName(self, "打开文件")
        if not path:
            return
        self.load_file(Path(path))

    def load_file(self, path: Path) -> None:
        """装载文档：解析 → 补占位 → 归一顺序 → 注册 → 重启预翻译。

        Args:
            path: 文件路径；不支持的扩展名会让 FileParser 抛 ValueError。

        Notes:
            第一步就停掉预翻译：它手里握着上一份文档的段落，继续跑会把旧译文写进
            新文档的缓存键里。
            提示行要先插再归一，这样它们才会被排进正确的阅读位置；
            此时注册的 bbox 仍是零值，真正坐标要等首次 render。
        """
        self._stop_pre_translation()
        document = self.parser.parse(path)
        #扫描页（无文本层）插入占位提示行：重排视图里照样占位，不静默丢内容
        notices = make_ocr_notices(document.meta.get("needs_ocr_pages") or [])
        document.blocks = segment_blocks(list(document.blocks) + notices)
        self._document = document
        if not self.viewer.step_mode:
            #逐句模式下由 StepRenderer 注册句子级块（每屏重写一次屏内坐标）
            self.mapper.register_blocks(self._document.blocks)#注册文本块到映射器
        self.detector.reset() #重置眼动检测器
        self._locked_region = None #清空锁定区域
        self._locked_para_key = None
        self.viewer.load_document(self._document) #视图层加载文档（首次绘制时回填 bbox）
        self.viewer.prime_reading_blocks()  #步进模式：先注册句子块，预翻译才有段可取
        self._status_bar.showMessage(f"已加载: {path.name}")
        self._start_pre_translation()

    # ---- 预翻译 ----------------------------------------------------------
    def _start_pre_translation(self) -> None:
        """按配置量启动后台预翻译，把前 N 段的译文提前灌进缓存。

        Notes:
            刻意让预翻译和主流程共用同一个 CacheStore 实例（传 db_path）：只有落在同一个
            库里，用户按空格时才能立即命中预翻译的结果，否则等于白翻一遍。
            取的是 mapper.segments()，它按阅读顺序排列，所以先翻的一定是用户先读到的。
        """
        cfg = self.config.translation
        if not cfg.pre_translate:
            return
        segments = self.mapper.segments()[: max(cfg.pre_translate_max_segments, 0)]
        if not segments:
            return
        # 必须与主窗口用同一个缓存库：预翻译写进去的，用户按空格时才能立刻命中
        self._pre_translator = PreTranslator(
            self._get_engine(), cfg, segments,
            cache_path=self._cache.db_path,
            parent=self,
        )
        self._pre_translator.progress.connect(self._on_pre_progress)
        self._pre_translator.all_done.connect(
            lambda: self._trans_label.setText("预翻译: 完成")
        )
        self._pre_translator.failed.connect(
            lambda msg: self._status_bar.showMessage(f"预翻译中断: {msg}")
        )
        self._trans_label.setText(f"预翻译: 0/{len(segments)}")
        self._pre_translator.start()

    def _stop_pre_translation(self) -> None:
        """停掉并销毁预翻译线程；关窗口、换文档、退出前都要调用。

        Notes:
            wait(3000) 是给正在进行的 HTTP 请求留的收尾窗口；超时后仍继续销毁，
            因为本线程的任务是"加速"而非"必须完成"，卡住主线程代价更高。
        """
        if self._pre_translator is None:
            return
        self._pre_translator.cancel()
        self._pre_translator.wait(3000)
        self._pre_translator.deleteLater()
        self._pre_translator = None

    def _resume_pre_translation(self) -> None:
        """恢复被用户请求压下去的预翻译；没有在跑的实例时静默返回。"""
        if self._pre_translator is not None:
            self._pre_translator.resume()

    def _on_pre_progress(self, done: int, total: int) -> None:
        """更新预翻译进度标签。

        Args:
            done: 已完成段数。
            total: 总段数。

        Notes:
            用户刚发起的即时翻译优先：后台进度不该把"翻译: 进行中…"盖掉，
            否则用户会以为自己的请求被忽略了。
        """
        #用户自己的翻译请求优先，别让预翻译进度盖掉它
        if self._worker is not None and self._worker.isRunning():
            return
        self._trans_label.setText(f"预翻译: {done}/{total}")

    # ---- 视线 / 鼠标 -----------------------------------------------------
    def on_gaze_point(self, point: GazePoint) -> None:
        """整条交互主链路：一个注视点 → 命中块 → 聚合段落 → 稳定判定 → 锁定高亮。

        鼠标轮询与眼动两条输入源最终都汇聚到这里，因此对上游而言二者行为完全一致。

        Args:
            point: 屏幕逻辑坐标下的注视点（鼠标模式下由 _poll_cursor 或 mouseMoveEvent 造出，
                眼动模式下由 _on_gaze_frame 换算后喂入）。

        Notes:
            流程是六步：浮层冻结 → 遮挡过滤 → 带容差命中（未命中再退到最近块）→
            段落聚合并把文档坐标换成屏幕坐标 → 稳定判定 → 选区锁定。
            未稳定时不锁定，而是维护"悬停段落变了就清空上一次高亮"的行为——
            视线扫过一排段落时不该每帧闪一次高亮框。
        """
        gaze_cfg = self.config.gaze

        # 浮层显示期间冻结凝视交互：只有空格/Esc 能关闭译文，
        # 鼠标移动不再清空选区/高亮或触发新锁定
        if self.overlay.is_showing:
            return

        # 0. 遮挡/失焦 → 暂停采样
        if self.mapper.is_occluded((point.x, point.y)):
            return

        #以下是被当前实现取代的单容差写法：不再对所有模式统一用 hit_tolerance_px，
        #逐句模式要单独给一个更大的请求容差
        # block = self.mapper.hit_test(
        #     (point.x, point.y), tolerance_px=gaze_cfg.hit_tolerance_px
        # )

        #带容差命中（两段式）：视线抖动 + 校准误差下，精确命中(tolerance=0)几乎必失败。
        #校准误差不可获取 → 容差恒用配置值；逐句模式用更大的请求容差，
        #实际会被块自己的 hit_pad_max（半个空隙）夹住，保证相邻句命中域不相交
        tolerance = (
            gaze_cfg.step_hit_tolerance_px if self.viewer.step_mode else gaze_cfg.hit_tolerance_px
        )
        block = self.mapper.hit_test(
            (point.x, point.y),
            tolerance_px=tolerance,
        )
        #未命中才做"最近块回退"：段间距只 0.5 行高，容差让相邻段命中域重叠，
        #不回退的话停在两行之间就什么都选不中
        if block is None and self.mapper._blocks:
            dx,dy = self.mapper.screen_to_document((point.x,point.y))
            nearest = min(
                self.mapper._blocks,
                key = lambda b : b.distance_to(dx,dy)
            )
            if nearest.hit_pad_max is not None:
                tol = nearest.hit_pad_max        #逐句模式：该块允许的最大不越界容差
            else:
                # 段落模式：请求容差与"块高 × 比例"取小，
                # 让容差随字号缩放——大块该更宽松，小块不该被无限放大
                tol = min(
                    tolerance,                       #当前模式的请求容差
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

        #未稳定（驻留帧数还没攒够）：只更新悬停态，不锁定
        if not stability.stable:
            hover_key = group.para_key if group else None #当前凝视所指的段落键
            #换段才清：视线扫过一整屏时，逐帧清空会让高亮框闪成一片
            if hover_key != self._last_hover_key:
                self._locked_region = None
                self._locked_para_key = None
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
            # 逐句模式：句框按 44px 大字号算，一句就能超过 RegionConfig 的上限，
            # 那里是给段落模式调的，这里改用"屏内就这么大"作为上限
            block_max = (
                (self.viewer.width(), self.viewer.height()) if self.viewer.step_mode else None
            )
            region = self.locker.lock_block(local, bounds=bounds, max_size=block_max)
            # 返回 None = 段落并集超限（"整页一大块"）→ 落到点模式回退
        if region is None:
            local_point = GazePoint(
                x=point.x - origin.x(),
                y=point.y - origin.y(),
                timestamp=point.timestamp,
            )
            region = self.locker.lock(
                local_point,
                bounds=bounds,
            )

        self._locked_region = region
        self._locked_para_key = group.para_key if group is not None else None
        self.feedback.show_region(region)
        # 锁完立刻清窗：一次驻留只触发一次翻译，否则每帧都会再锁一遍同一个区域
        self.detector.reset()

    def mouseMoveEvent(self, event) -> None:
        """鼠标模式下的第二条输入通路：移动即视为一次注视样本。

        Args:
            event: Qt 鼠标事件，取 globalPosition 保证与凝视点同一坐标系。
        """
        pos = event.globalPosition()
        self.on_gaze_point(GazePoint(x=pos.x(), y=pos.y()))

    # ---- 键盘 ------------------------------------------------------------
    def on_space(self) -> None:
        """空格：浮层开着就关掉它，否则翻译当前锁定的选区。

        Notes:
            关闭时把任务键与浮层位置一并置空是关键：worker 的完成回调会检查这两项，
            全是 None 就直接返回，于是"取消了的译文"不会被写进缓存。
        """
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
        """Esc：取消正在进行的翻译、收起浮层、解锁滚动并恢复预翻译。

        Notes:
            与空格的区别是它额外 cancel 了 worker，且一定会把翻译标签复位。
            这里的"取消"只在段与段的间隙生效——已经发出的 HTTP 请求拉不回来。
        """
        if self._worker is not None:
            self._worker.cancel()
        self.overlay.hide_overlay()
        self.viewer.set_scroll_locked(False)
        self._translation_key = None
        self._translation_region = None
        self._trans_label.setText("翻译: 空闲")
        self._resume_pre_translation()

    # ---- 翻译 ------------------------------------------------------------
    def _get_engine(self) -> TranslationEngine:
        """惰性获取翻译引擎，全局一个实例。

        Returns:
            共享的 TranslationEngine。

        Notes:
            懒加载而非构造时创建：引擎工厂可能拉起较重的依赖，
            用户不打开文件、不触发翻译时不该为此付出启动代价。
        """
        if self._engine is None:
            self._engine = create_engine(self.config.translation)
        return self._engine

    def _on_translation_done(self, result: TranslationResult) -> None:
        """翻译完成：显示译文 + 主线程写缓存 + 解锁预翻译。

        Args:
            result: worker 给出的翻译结果。

        Notes:
            显示与写缓存彼此独立：关掉缓存时同样要把译文画出来，只是不落库。
            进来先看任务键是否还在，为 None 说明已经被空格/Esc 取消了，
            此时的结果不该再回填。
        """
        key = self._translation_key
        region = self._translation_region
        if key is None or region is None:
            return
        #显示译文（与缓存开关无关，否则关缓存时翻译完成不显示）
        self.overlay.show_translation(region, result, OverlayMode.TRANSLUCENT)
        self.viewer.set_scroll_locked(True)  # 浮层显示期间锁定滚动
        self._trans_label.setText(f"翻译: 完成 {result.elapsed_ms}ms")
        #写入缓存（默认启用缓存）
        self._cache.put(key, result, file_hash=None)
        self._translation_key = None
        self._translation_region = None
        self._resume_pre_translation()

    def _on_translation_failed(self, message: str) -> None:
        """翻译失败：收起浮层、解锁滚动，并把原因放进状态栏。

        Args:
            message: worker 给出的错误描述。

        Notes:
            失败不写缓存是刻意的：同样的源文本下一次按下应该重试，
            而不是被一条失败记录永久挡住。
        """
        self.overlay.hide_overlay()  #失败就不再显示翻译浮层
        self.viewer.set_scroll_locked(False)  # 解锁滚动
        self._trans_label.setText("翻译: 失败")
        self._status_bar.showMessage(f"翻译失败: {message}")
        self._translation_key = None
        self._translation_region = None
        self._resume_pre_translation()

    def _trigger_translation(self, region: Region) -> None:
        """对一个 viewer 本地矩形发起翻译：查缓存 → 命中即显示，未命中则起线程。

        Args:
            region: viewer 本地坐标下的选区矩形（来自 RegionLocker 的输出）。

        Notes:
            取文本优先走"段落文本唯一出处"，点模式才退到区域内 blocks——
            这与预翻译取文本走的是同一个函数，缓存键才可能对上。
            键用统一的 make_segment_key 构造，两条路径（预翻译、即时翻译）因此共享同一份缓存。
        """
        origin = self.viewer.mapToGlobal(QPoint(0,0)) #将部件内本地坐标映射为全局屏幕坐标
        global_region = Region(
            x = region.x + origin.x(),
            y=region.y + origin.y(),
            w=region.w,
            h=region.h,
        )
        #取译文源文本：段落模式走"段落文本唯一出处"（与预翻译同一函数、同一缓存键），
        #点模式回退到区域内的 blocks
        parts = None
        if self._locked_para_key is not None:
            parts = self.mapper.segment_key_parts(self._locked_para_key)
        if parts is None:
            parts = self.mapper.region_key_parts(global_region)
        if parts is None:
            self._status_bar.showMessage("未检测到可翻译文本")
            return
        text = parts[1]
        cfg = self.config.translation

        #唯一标识一条翻译缓存（与预翻译共用构造函数，键必然一致）
        key = make_segment_key(parts, cfg, self._get_engine().name)

        #检查缓存（默认启用）
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
            self._resume_pre_translation()
            return
        #用户请求优先：预翻译先让路，等这次翻完再继续
        if self._pre_translator is not None:
            self._pre_translator.pause()
        # worker 复用同一个实例：它内部有 running 保护且 finish 信号只接一次
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
        self._worker.request(text) #提交源文本，线程已在跑则本次不会被受理

    # ---- 事件分发 --------------------------------------------------------
    def keyPressEvent(self,event)->None:
        """主窗口自身的按键分发：方向键翻句 → 过滤连发 → 空格/Esc。

        Args:
            event: Qt 按键事件。

        Notes:
            连发必须在空格/Esc 之前过滤：按住空格会不断重复触发，浮层会来回闪烁。
            方向键则故意不过滤连发，长按要能连续翻句。
        """
        # 逐句模式下 ↑↓/PgUp/PgDn 等价于滚轮；长按连发不拦（滚轮之外的第二条通路）
        if self.viewer.step_mode and event.key() in _STEP_KEYS:
            self.viewer.step_reading(1 if event.key() in _STEP_DOWN_KEYS else -1)
            event.accept()
            return
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
        """把落在 viewer 上的按键与尺寸变化转发进来。

        Args:
            obj: 事件来源对象。
            event: Qt 事件。

        Returns:
            True 表示事件已被消费；其余情况交回父类处理。

        Notes:
            viewer 获得焦点时按键不会冒泡到主窗口，因此空格/Esc/方向键必须在这里
            再判一遍；这里的分支要与 keyPressEvent 保持同步，否则焦点位置不同
            会导致按键行为不一致。
            尺寸变化时浮层与高亮层还要跟着 resize——它们是普通子控件，不会自动跟随。
        """
        if obj is self.viewer:
            if event.type() == event.Type.KeyPress:
                if self.viewer.step_mode and event.key() in _STEP_KEYS:
                    self.viewer.step_reading(1 if event.key() in _STEP_DOWN_KEYS else -1)
                    return True
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

