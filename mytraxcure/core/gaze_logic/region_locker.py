from __future__ import annotations

from mytraxcure.core.config import RegionConfig
from mytraxcure.core.types import GazePoint, Region


class RegionLocker:

    def __init__(self, config: RegionConfig) -> None:
        self.config = config

    def lock(
        self,
        point: GazePoint,
        bounds: tuple[int, int, int, int] | None = None,
    ) -> Region:
        cfg = self.config  #获取配置对象

        #基础尺寸
        w = cfg.default_width
        h = cfg.default_height

        #限制矩形尺寸不超过上限
        w = min(w, cfg.max_width)
        h = min(h, cfg.max_height)

        #以凝视点为中心定位,实现居中锁定，用户看哪里，矩形中心就在哪里
        x = point.x - w /2
        y = point.y - h /2

        #如果传入了页面边界，开始约束
        if bounds is not None:
            left,top,right,bottom = bounds
            if right - left < w:
                w = right - left
                x = left
            else:
                x = min(
                    max(x,left),
                    right - w
                )
            if bottom - top < h:
                h = bottom - top
                y = top
            else:
                y = min(
                    max(y,top),
                    bottom-h
                )
        region = Region(
            x=int(x),
            y = int(y),
            w = int(w),
            h = int(h)
        )
        return region

    def lock_block(
        self,
        screen_region: Region,
        bounds: tuple[int, int, int, int] | None = None,
    ) -> Region | None:
        cfg = self.config

        # 1. 尺寸上限校验：超限 = "整页一大块"，由上层回退点模式
        if screen_region.w > cfg.max_width or screen_region.h > cfg.max_height:
            return None

        x, y = screen_region.x, screen_region.y
        w, h = screen_region.w, screen_region.h

        # 2. 边界夹取（矩形大于可用区域时退化为整个 bounds）
        if bounds is not None:
            left, top, right, bottom = bounds
            if right - left < w:
                w = right - left
                x = left
            else:
                x = min(max(x, left), right - w)
            if bottom - top < h:
                h = bottom - top
                y = top
            else:
                y = min(max(y, top), bottom - h)

        # 3. 整数像素 Region
        return Region(x=int(x), y=int(y), w=int(w), h=int(h))
