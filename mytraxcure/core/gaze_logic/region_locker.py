"""模块名称：region_locker.py

功能描述：把凝视判定结果换算成屏幕上的整数像素 Region。lock() 以凝视点为中心
生成固定尺寸选区（无文本块可依时的点模式回退路径），lock_block() 校验并夹取段落并集矩形。

关键约束：本模块只做几何换算，不判定凝视是否稳定；尺寸上限取自 RegionConfig，
而该上限是按段落模式调的，逐句阅读模式必须由调用方用 max_size 覆盖，
否则大字号单句会被误判成"整页一大块"而永远选不中。
"""

from __future__ import annotations

from mytraxcure.core.config import RegionConfig
from mytraxcure.core.types import GazePoint, Region


class RegionLocker:
    """屏幕选区几何换算器：按 RegionConfig 的尺寸上限，把凝视点或文本块矩形夹成整数像素 Region。"""

    def __init__(self, config: RegionConfig) -> None:
        """持有区域配置；配置是只读的，换算结果只取决于每次传入的点与边界。

        Args:
            config: RegionConfig，提供默认尺寸、尺寸上限。
        """
        self.config = config

    def lock(
        self,
        point: GazePoint,
        bounds: tuple[int, int, int, int] | None = None,
    ) -> Region:
        """以凝视点为中心生成固定尺寸选区，并夹进可用区域（点模式的回退路径）。

        Args:
            point: 凝视点，视口本地像素坐标；只使用 x / y。
            bounds: (left, top, right, bottom) 可用区域边界；None 表示不夹取，
                允许矩形落在可用区域之外。

        Returns:
            居中于凝视点的整数像素 Region。可用区域比所需尺寸还小时，
            退化为整个可用区域，此时矩形不再以凝视点为中心。
        """
        cfg = self.config

        # 默认尺寸也要过一遍上限：配置文件里的 default_* 可能大于 max_*
        w = cfg.default_width
        h = cfg.default_height

        w = min(w, cfg.max_width)
        h = min(h, cfg.max_height)

        # 矩形中心对齐凝视点，让高亮区与用户当前注视位置一致（点模式没有文本块可依）
        x = point.x - w /2
        y = point.y - h /2

        # 夹取时若矩形大于可用区域，退化为整块可用区域：宁可少框，也不裁掉一侧内容
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
        max_size: tuple[int, int] | None = None,
    ) -> Region | None:
        """校验段落并集矩形是否可锁定，并把越界部分夹回 bounds（段落模式路径）。

        Args:
            screen_region: 段落并集 bbox，视口本地像素坐标。
            bounds: (left, top, right, bottom) 可用区域边界；None 表示不夹取。
            max_size: (max_w, max_h) 尺寸上限覆盖值；None 表示沿用 RegionConfig 的
                max_width / max_height。逐句阅读模式必须传入，理由见 Notes。

        Returns:
            夹取后的整数像素 Region；任一维超过 max_size 时返回 None，
            表示框住的是整页而非一个段落，由上层回退到 lock() 的点模式。

        Notes:
            逐句模式字号远大于正文，单句的并集 bbox 常常超过 RegionConfig 的上限
            （那是按段落模式调的），因此调用方需按当前屏幅传入 max_size。
        """
        cfg = self.config
        # 上限按阅读模式覆盖：逐句模式字号大得多，单句就能超过配置里的
        # max_width/max_height，照配置判会把整句错判成"整页一大块"
        max_w = cfg.max_width if max_size is None else int(max_size[0])
        max_h = cfg.max_height if max_size is None else int(max_size[1])

        # 超限即判为"整页一大块"：此时框住的是整页而非一段，交给上层回退点模式
        if screen_region.w > max_w or screen_region.h > max_h:
            return None

        x, y = screen_region.x, screen_region.y
        w, h = screen_region.w, screen_region.h

        # 矩形大于可用区域时退化为整个 bounds，而不是裁掉一侧内容
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

        # 截断取整而非四舍五入：Region 为整像素类型，进位会让矩形比原块大出 1px
        return Region(x=int(x), y=int(y), w=int(w), h=int(h))
