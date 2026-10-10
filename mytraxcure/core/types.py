"""模块名称：types.py

功能描述：全项目共享的轻量数据结构——凝视点与凝视样本、头部姿态、矩形区域、
    文本块与段落聚合体，外加一个点到矩形的距离函数。

关键约束：
    - 处在依赖树底部，被 gaze / gaze_logic / file / ui 共同引用；加字段务必给
      默认值，否则所有构造点都要跟着改。
    - GazePoint 与 Region 是不带坐标系语义的纯容器：同一个类型在不同链路里分别
      承载主屏物理像素、控件本地坐标和文档坐标；换算只在 TextMapper 与
      main_window 里做，不要在这里混用。
    - 只依赖标准库与 numpy，避免把重依赖传导给所有引用方。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np


def point_rect_distance(
    px: float, py: float, x: float, y: float, w: float, h: float
) -> float:
    """点到矩形的最短欧氏距离：点落在矩形内部时为 0。

    Args:
        px: 点的横坐标；须与矩形同一坐标系（契约由调用方保证，本函数不校验）。
        py: 点的纵坐标。
        x: 矩形左上角横坐标。
        y: 矩形左上角纵坐标。
        w: 矩形宽度。
        h: 矩形高度。

    Returns:
        float：点到矩形边界的最短距离；点在内部返回 0.0。

    Notes:
        写法上取两轴各自的外溢量再合成距离，比逐边求距离少一层分支——命中判定
        热路径每帧对每个块都要跑一次距离，故用这个形式。
        目前唯一的调用方是 TextBlock.distance_to（文档坐标）。
    """
    dx = max(x - px, 0.0, px - (x + w))
    dy = max(y - py, 0.0, py - (y + h))
    return (dx * dx + dy * dy) ** 0.5

@dataclass
class GazePoint:
    """一次凝视预测的结果点：坐标 + 采样时刻，是驻留判定的最小单位。"""

    x: float
    y: float
    # 采样时刻（time.time 墙钟秒）：驻留时长取窗口内首尾点 timestamp 之差
    timestamp: float = field(default_factory=time.time)

@dataclass
class HeadPose:
    """头部姿态的三个欧拉角，单位弧度，由 eyetrax 每帧给出。"""

    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0

    def max_abs_deg(self) -> float:
        """取 yaw / pitch 中偏转较大的那个角，换算成度。

        Returns:
            float：max(|yaw|, |pitch|) 的度数，恒 ≥ 0；默认姿态下为 0.0。

        Notes:
            只看 yaw/pitch：eyetrax 的 roll 在正脸时接近 ±π 且会在 ±π 之间环绕
            跳变，拿它取绝对值做阈值会把正脸也判成超限。而单纯歪头已被 eyetrax
            的人脸坐标系归一化抵消掉大部分，故不参与门控。
            方法名里的 abs 指"两个角各自取绝对值"，不代表三个角都参与判定。
        """
        return max(math.degrees(abs(self.yaw)), math.degrees(abs(self.pitch)))

@dataclass
class GazeSample:
    """摄像头一帧的原始采样：特征向量、是否眨眼、头部姿态与采样时刻。"""

    # eyetrax 的特征向量；末三个分量依次是 yaw/pitch/roll（弧度）
    features: np.ndarray
    blink: bool
    head_pose: HeadPose
    timestamp: float = field(default_factory=time.time)

@dataclass
class Region:
    """整数像素矩形，附带 right / bottom 便捷属性；本身不带坐标系语义。"""

    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        """右边界 x + w（不含该列）。"""
        return self.x + self.w

    @property
    def bottom(self) -> int:
        """下边界 y + h（不含该行）。"""
        return self.y + self.h

@dataclass
class TextBlock:
    """文档坐标下的一个文本块：命中判定、段落聚合与译文缓存键的共同载体。"""

    text: str
    page: int
    x: float
    y: float
    w: float
    h: float
    block_id: str = ""
    source: str = ""  # pdf / txt / word；不支持图片，无 ocr 来源
    para_key: str = ""  # 段落聚合键，解析期写入；为空时回退用 block_id
    # 容差上限（文档坐标）：由版面决定，保证与相邻块的容差域不相交。
    # None = 不限制（连续滚动模式整篇是连续排版，用全局容差即可）
    hit_pad_max: float | None = None

    @property
    def right(self) -> float:
        """右边界 x + w。"""
        return self.x + self.w

    @property
    def bottom(self) -> float:
        """下边界 y + h。"""
        return self.y + self.h

    def contains(self, dx: float, dy: float, tol: float = 0.0) -> bool:
        """判定文档坐标点是否落在本块内（含容差外膨胀）。

        Args:
            dx: 点的文档横坐标。
            dy: 点的文档纵坐标。
            tol: 命中容差（文档坐标像素），把 bbox 四边各向外撑开 tol；
                0 即精确命中，负值会反过来收缩 bbox。

        Returns:
            bool：True 表示命中本块。

        Notes:
            这里是全局唯一的命中判定实现，精确命中与带容差命中都走它，改判定
            规则只需改一处。tol 由上层按阅读模式算出后传入（如 TextMapper.hit_test），
            本方法不读配置。
        """
        return (
            self.x - tol <= dx <= self.right + tol
            and self.y - tol <= dy <= self.bottom + tol
        )

    def distance_to(self, dx: float, dy: float) -> float:
        """点到本块矩形的距离；多候选时用它挑最近的块（块内为 0）。"""
        return point_rect_distance(dx, dy, self.x, self.y, self.w, self.h)

@dataclass
class ParagraphGroup:
    """同一段落（para_key）聚合后的结果：文档坐标并集 bbox + 成员 block_id。"""

    para_key: str
    page: int
    bbox: Region  # 文档坐标下的并集 bbox
    block_ids: list[str] = field(default_factory=list)

    @property
    def width(self) -> int:
        """并集 bbox 宽度（文档坐标）。"""
        return self.bbox.w

    @property
    def height(self) -> int:
        """并集 bbox 高度（文档坐标）。"""
        return self.bbox.h
