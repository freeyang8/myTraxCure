from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from mytraxcure.core.types import GazePoint


@dataclass
class GazeStability:

    stable: bool
    mode: str = "point"  # "paragraph" / "point"：上层据此选择 RegionLocker 路径
    block_key: str | None = None  # 稳定段落的 para_key（段落模式）
    center: GazePoint | None = None
    variance: float = 0.0  # 窗口内坐标方差（px² 量级，口径自定义）
    offset: float = 0.0  # 窗口内相对质心的最大偏移（px）
    dominant_ratio: float = 0.0  # 段落模式：窗口内主导 para_key 的占比
    dwell_seconds: float = 0.0  # 段落模式：末尾连续驻留时长；点模式：窗口时间跨度

class GazeDetector:

    def __init__(
        self,
        dwell_seconds: float = 0.8,
        max_variance_px: float = 60.0,
        max_offset_px: float = 40.0,
        dominant_ratio: float = 0.6,
        min_dominant_frames: int = 4,
        window_size: int = 60,
    ) -> None:
        self.dwell_seconds = dwell_seconds
        self.max_variance_px = max_variance_px
        self.max_offset_px = max_offset_px
        self.dominant_ratio = dominant_ratio
        self.min_dominant_frames = min_dominant_frames
        self.window_size = window_size
        # 元素为 (GazePoint, para_key | None)；para_key 为 None 表示该帧未命中任何块
        self._buffer: deque[tuple[GazePoint, str | None]] = deque(maxlen=window_size)
        # 换段磁滞状态：当前认定的主导段落 key（需求 6.8.4）
        self._dominant_key: str | None = None

    def reset(self) -> None:
        self._buffer.clear()
        self._dominant_key = None

    def update(
        self,
        point: GazePoint,
        block_key: str | None = None,
        block_height_px: float = 0.0,
    ) -> GazeStability:
        # 1. 追加到滑动窗口（眨眼/丢脸帧由上层直接不调用本方法）
        self._buffer.append((point, block_key))

        # 2. 按时间戳裁剪旧点：窗口主体保持 dwell_seconds 跨度，
        #    但多保留一帧"越界锚点"（否则窗口跨度永远 < dwell_seconds，
        #    连续驻留时长永远判不达标）
        cutoff = point.timestamp - self.dwell_seconds
        while len(self._buffer) > 2 and self._buffer[1][0].timestamp < cutoff:
            self._buffer.popleft()

        if block_key is not None:
            return self._update_paragraph(block_key, block_height_px)
        return self._update_point()

    # ---- 内部：双模式判定 ----------------------------------------------
    @staticmethod
    def _window_stats(points: list[GazePoint]) -> tuple[GazePoint, float, float]:
        n = len(points)
        cx = sum(p.x for p in points) / n
        cy = sum(p.y for p in points) / n
        variance = sum((p.x - cx) ** 2 + (p.y - cy) ** 2 for p in points) / n
        offset = max(((p.x - cx) ** 2 + (p.y - cy) ** 2) ** 0.5 for p in points)
        return GazePoint(x=cx, y=cy, timestamp=points[-1].timestamp), variance, offset

    def _update_paragraph(
        self, block_key: str, block_height_px: float
    ) -> GazeStability:
        buf = list(self._buffer)
        total = len(buf)

        # 换段磁滞：新 key 需在窗口末尾连续命中 ≥ min_dominant_frames 帧才取代 dominant
        tail_run = 0
        for _, key in reversed(buf):
            if key == block_key:
                tail_run += 1
            else:
                break
        if self._dominant_key is None or tail_run >= self.min_dominant_frames:
            self._dominant_key = block_key
        dom_key = self._dominant_key

        # 窗口内 dominant key 的众数占比
        dom_count = sum(1 for _, key in buf if key == dom_key)
        ratio = dom_count / total if total else 0.0

        # 末尾连续命中 dominant key 的时间跨度 = 连续驻留时长
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

        # 整个窗口的质心偏移（相对阈值：段落越高容忍越大）
        center, variance, offset = self._window_stats([p for p, _ in buf])
        threshold = max(self.max_offset_px, block_height_px)

        stable = (
            dwell >= self.dwell_seconds
            and ratio >= self.dominant_ratio
            and offset <= threshold
        )
        return GazeStability(
            stable=stable,
            mode="paragraph",
            block_key=dom_key if stable else None,
            center=center,
            variance=variance,
            offset=offset,
            dominant_ratio=ratio,
            dwell_seconds=dwell,
        )

    def _update_point(self) -> GazeStability:
        buf = list(self._buffer)
        center, variance, offset = self._window_stats([p for p, _ in buf])
        span = buf[-1][0].timestamp - buf[0][0].timestamp if len(buf) >= 2 else 0.0

        stable = (
            variance <= self.max_variance_px
            and offset <= self.max_offset_px
            and span >= self.dwell_seconds
        )
        return GazeStability(
            stable=stable,
            mode="point",
            center=center,
            variance=variance,
            offset=offset,
            dwell_seconds=span,
        )

    # ---- 便捷：判断头部角度是否允许采样 --------------------------------
    def head_angle_allowed(self, max_abs_deg: float, head_pose) -> bool:
        return head_pose.max_abs_deg() <= max_abs_deg
