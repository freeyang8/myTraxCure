from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np


def point_rect_distance(
    px: float, py: float, x: float, y: float, w: float, h: float
) -> float:
    dx = max(x - px, 0.0, px - (x + w))
    dy = max(y - py, 0.0, py - (y + h))
    return (dx * dx + dy * dy) ** 0.5

@dataclass
class GazePoint:

    x: float
    y: float
    timestamp: float = field(default_factory=time.time)

@dataclass
class HeadPose:

    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0

    def max_abs_deg(self) -> float:
        # 只判 yaw/pitch：eyetrax 的 roll 正脸≈±π 且在 ±π 间环绕跳变，
        # 拿绝对值做阈值会把所有帧（含正脸）判成超限。
        # 单纯歪头本身被 eyetrax 的人脸坐标系归一化抵消掉大部分，故不参与门控。
        return max(math.degrees(abs(self.yaw)), math.degrees(abs(self.pitch)))

@dataclass
class GazeSample:

    features: np.ndarray
    blink: bool
    head_pose: HeadPose
    timestamp: float = field(default_factory=time.time)

@dataclass
class Region:

    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

@dataclass
class TextBlock:

    text: str
    page: int
    x: float
    y: float
    w: float
    h: float
    block_id: str = ""
    source: str = ""  # pdf / txt / word（v6.2：不支持图片，无 ocr 来源）
    para_key: str = ""  # 段落聚合键，解析期写入；为空时回退用 block_id
    # 容差上限（文档坐标）：由版面决定，保证与相邻块的容差域不相交。
    # None = 不限制（连续滚动模式整篇是连续排版，用全局容差即可）
    hit_pad_max: float | None = None

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def contains(self, dx: float, dy: float, tol: float = 0.0) -> bool:
        # 命中判定的唯一出处；tol 为眼动容差（bbox 外膨胀），由 hit_test 传入
        return (
            self.x - tol <= dx <= self.right + tol
            and self.y - tol <= dy <= self.bottom + tol
        )

    def distance_to(self, dx: float, dy: float) -> float:
        return point_rect_distance(dx, dy, self.x, self.y, self.w, self.h)

@dataclass
class ParagraphGroup:

    para_key: str
    page: int
    bbox: Region  # 文档坐标下的并集 bbox
    block_ids: list[str] = field(default_factory=list)

    @property
    def width(self) -> int:
        return self.bbox.w

    @property
    def height(self) -> int:
        return self.bbox.h
