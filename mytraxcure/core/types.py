from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

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
        import math

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

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.right and self.y <= py <= self.bottom

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

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

    def contains(self, dx: float, dy: float) -> bool:
        return self.x <= dx <= self.right and self.y <= dy <= self.bottom

    def distance_to(self, dx: float, dy: float) -> float:
        return point_rect_distance(dx, dy, self.x, self.y, self.w, self.h)

    def union(self, other: TextBlock) -> Region:
        x0 = min(self.x, other.x)
        y0 = min(self.y, other.y)
        x1 = max(self.right, other.right)
        y1 = max(self.bottom, other.bottom)
        return Region(x=int(x0), y=int(y0), w=int(x1 - x0), h=int(y1 - y0))

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

@dataclass
class DocumentMeta:

    path: str
    kind: str  # pdf / txt / word（v6.2：不支持 image / 扫描件）
    file_hash: str = ""
    sensitive: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
