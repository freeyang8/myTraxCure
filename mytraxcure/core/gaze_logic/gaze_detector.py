"""模块名称：gaze_detector.py

功能描述：凝视稳定性判定的唯一出处。维护一个按时间裁剪的视线点滑动窗口，逐帧判定
"用户是否稳定停在某个文本块上"，输出 GazeStability 供上层决定是否锁定选区、走段落还是点路径。

关键约束：
- 眨眼、丢脸、头部姿态超限的帧由上层直接丢弃，不调用 update；窗口里一旦出现时间间隙，
  间隙前后的点会被拼成一次跨越数秒的假驻留，因此上层遇到这些帧必须先 reset()。
- 判定阈值全部由构造参数注入（实际取值来自 GazeConfig），本模块不解释业务含义。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from mytraxcure.core.types import GazePoint


@dataclass
class GazeStability:
    """一帧凝视判定的结果快照：stable 表示可否锁定选区，mode 说明结论出自哪套阈值。"""

    stable: bool
    # "paragraph" 按段落归属判定，"point" 按点散布判定；上层据此选 RegionLocker 的路径
    mode: str = "point"

class GazeDetector:
    """凝视稳定性判定器：在时间窗口内累计视线点，判定一次"有效驻留"是否成立。

    生命周期：逐帧 update() → stable 后由上层锁定选区并立即 reset()，
    保证一次驻留只触发一次选中；转头、眨眼造成的时间间隙同样必须先 reset()。
    """

    def __init__(
        self,
        dwell_seconds: float = 0.8,
        max_variance_px: float = 60.0,
        max_offset_px: float = 40.0,
        dominant_ratio: float = 0.6,
        min_dominant_frames: int = 4,
        window_size: int = 60,
    ) -> None:
        """按凝视阈值初始化判定器，窗口与主导段落记忆均为空。

        Args:
            dwell_seconds: 判定为驻留所需的最短时间跨度（秒）。
            max_variance_px: 点模式下窗口点相对质心距离的平方均值上限（像素²）。
            max_offset_px: 点模式下单个点偏离质心的最大距离上限（像素）。
            dominant_ratio: 段落模式下主导段落占窗口帧数的最小比例（0~1）。
            min_dominant_frames: 段落模式下新段落取代主导段落所需的连续命中帧数。
            window_size: 滑动窗口的帧数上限，只用于限制内存，实际跨度由 dwell_seconds 裁剪。

        Notes:
            此处的默认值与 GazeConfig 的同名默认值并不一致（GazeConfig 已整体放宽），
            实际运行以 main_window 从 GazeConfig 注入的取值为准。
        """
        self.dwell_seconds = dwell_seconds
        self.max_variance_px = max_variance_px
        self.max_offset_px = max_offset_px
        self.dominant_ratio = dominant_ratio
        self.min_dominant_frames = min_dominant_frames
        self.window_size = window_size
        # 元素为 (GazePoint, para_key | None)；para_key 为 None 表示该帧未命中任何块
        self._buffer: deque[tuple[GazePoint, str | None]] = deque(maxlen=window_size)
        # 换段磁滞的当前主导段落 key：窗口内众数变化不立刻改判，避免段落边界闪烁
        self._dominant_key: str | None = None

    def reset(self) -> None:
        """清空窗口与主导段落记忆：一次驻留只应触发一次锁定，锁定后必须由上层调用。"""
        self._buffer.clear()
        self._dominant_key = None

    def update(
        self,
        point: GazePoint,
        block_key: str | None = None,
        block_height_px: float = 0.0,
    ) -> GazeStability:
        """喂入一帧视线点，返回当前是否构成一次稳定驻留。

        Args:
            point: 本帧视线点（像素坐标）；timestamp 必须随帧单调递增，裁剪逻辑依赖它。
            block_key: 本帧命中的段落聚合键；None 表示未命中任何块，本帧走点模式。
            block_height_px: 命中块在屏幕上的高度（像素）。段落模式用它放宽偏移阈值，
                点模式不使用；未命中时传 0.0。

        Returns:
            GazeStability：stable 为 True 表示可以锁定选区；
            mode 为 "paragraph" 或 "point"，说明结论由哪套阈值得出。

        Notes:
            眨眼、丢脸、头部姿态超限的帧不要调用本方法——窗口里留下时间间隙后，
            间隙前后的点会被拼成一次跨越数秒的假驻留。上层遇到这些帧应先 reset()。
        """
        # 无效帧（眨眼/丢脸/转头）由上层直接跳过，窗口内不出现时间间隙
        self._buffer.append((point, block_key))

        # 按时间戳裁剪旧点，但要多留一帧刚越界的锚点：否则窗口跨度永远略小于
        # dwell_seconds，驻留时长永远判不达标。len > 2 保证锚点自身不会被弹出
        cutoff = point.timestamp - self.dwell_seconds
        while len(self._buffer) > 2 and self._buffer[1][0].timestamp < cutoff:
            self._buffer.popleft()

        # 命中块才走段落模式；未命中时 block_height_px 无意义，退化为点散布判定
        if block_key is not None:
            return self._update_paragraph(block_key, block_height_px)
        return self._update_point()

    # ---- 内部：双模式判定 ----------------------------------------------
    @staticmethod
    def _window_stats(points: list[GazePoint]) -> tuple[float, float]:
        """统计点集相对其质心的散布程度，同时给出"整体抖"和"偶尔飞"两个指标。

        Args:
            points: 同一窗口内的视线点，至少 1 个；空列表会触发除零，由调用方保证。

        Returns:
            (variance, offset)：variance 是各点到质心距离的平方均值（像素²），
            对持续抖动敏感；offset 是离质心最远点的距离（像素），对偶发跳点敏感。
        """
        n = len(points)
        cx = sum(p.x for p in points) / n
        cy = sum(p.y for p in points) / n
        variance = sum((p.x - cx) ** 2 + (p.y - cy) ** 2 for p in points) / n
        offset = max(((p.x - cx) ** 2 + (p.y - cy) ** 2) ** 0.5 for p in points)
        return variance, offset

    def _update_paragraph(
        self, block_key: str, block_height_px: float
    ) -> GazeStability:
        """段落模式判定：视线是否稳定停在同一段落上。

        三条件同时成立才判稳定——末尾连续命中该段落的时长达标、该段落在窗口内占比达标、
        整个窗口的质心偏移在容差内；任一不满足即为不稳定。

        Args:
            block_key: 本帧命中的段落键，用于磁滞改判与占比统计。
            block_height_px: 命中块的屏幕高度（像素），用于放宽质心偏移阈值。

        Returns:
            GazeStability(stable=..., mode="paragraph")。
        """
        buf = list(self._buffer)
        total = len(buf)

        # 磁滞：新段落要在窗口末尾连续命中 min_dominant_frames 帧才取代当前主导段落。
        # 视线扫过段落边界时会来回跳，直接取众数会让选区在两个段落间反复闪烁
        tail_run = 0
        for _, key in reversed(buf):
            if key == block_key:
                tail_run += 1
            else:
                break
        if self._dominant_key is None or tail_run >= self.min_dominant_frames:
            self._dominant_key = block_key
        dom_key = self._dominant_key

        # 占比而非全票：眼动固有的落空帧（落在行距、页边距上）不该否掉一次真实注视
        dom_count = sum(1 for _, key in buf if key == dom_key)
        ratio = dom_count / total if total else 0.0

        # 驻留时长取"末尾连续命中主导段落"的时间跨度：中途被打断就重新计，不算连续注视
        run_points: list[GazePoint] = []
        for p, key in reversed(buf):
            if key != dom_key:
                break
            run_points.append(p)
        dwell = (
            run_points[0].timestamp - run_points[-1].timestamp
            if len(run_points) >= 2
            else 0.0
        )

        # 段落模式只看质心偏移不看方差：段落内正常扫视会让方差天然偏大，用方差判会把
        # 长段落全部否掉；阈值随块高放宽——块越高，允许的漂移越大
        _, offset = self._window_stats([p for p, _ in buf])
        threshold = max(self.max_offset_px, block_height_px)

        stable = (
            dwell >= self.dwell_seconds
            and ratio >= self.dominant_ratio
            and offset <= threshold
        )
        return GazeStability(stable=stable, mode="paragraph")

    def _update_point(self) -> GazeStability:
        """点模式判定：无文本块可依时，改判视线点本身是否聚成一团且持续够久。"""
        buf = list(self._buffer)
        variance, offset = self._window_stats([p for p, _ in buf])
        # 跨度含裁剪时多留的那一帧锚点，因此刚够 dwell_seconds 即算达标
        span = buf[-1][0].timestamp - buf[0][0].timestamp if len(buf) >= 2 else 0.0

        stable = (
            variance <= self.max_variance_px
            and offset <= self.max_offset_px
            and span >= self.dwell_seconds
        )
        return GazeStability(stable=stable, mode="point")

    # ---- 便捷：判断头部角度是否允许采样 --------------------------------
    def head_angle_allowed(self, max_abs_deg: float, head_pose) -> bool:
        """头部姿态门控：转头看别处时的帧不可用于采样。

        Args:
            max_abs_deg: yaw/pitch 允许的最大偏转角（度），来自 GazeConfig.max_head_angle_deg。
            head_pose: 提供 max_abs_deg() 的头部姿态对象（core.types.HeadPose）。

        Returns:
            True 表示本帧姿态在允许范围内，可以参与驻留判定。

        Notes:
            门控只比较 yaw/pitch，不含 roll：eyetrax 的 roll 在正脸时接近 ±π 且会环绕跳变，
            取绝对值判定会把正脸也判成超限，导致所有帧都被拒。
        """
        return head_pose.max_abs_deg() <= max_abs_deg
