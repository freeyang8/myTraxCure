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
        return max(math.degrees(abs(self.yaw)), math.degrees(abs(self.pitch)), math.degrees(abs(self.roll)))

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
