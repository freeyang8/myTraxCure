"""屏幕尺寸取值：必须与 cv2 全屏窗口实际落点一致。

eyetrax 的 `get_screen_size()` 取的是 `get_monitors()[0]`——列表里的第一块屏，
多屏时并不等于 Windows 主屏。而 cv2 的 `WND_PROP_FULLSCREEN` 窗口固定开在主屏上
（实测：双屏 2560x1600(非主) + 1920x1080(主) 时，全屏窗口拿到 (0,0,1920,1080)）。

两边不一致的后果不是"精度差一点"，而是整体错位：
标定点按前者的版面算、画在后者的窗口里 → 超出部分直接落在屏幕外；
模型输出的是前者的坐标系，应用却按后者的窗口用 → 整体缩放 + 偏移全错。
"""

from __future__ import annotations

from screeninfo import get_monitors


def primary_screen_size() -> tuple[int, int]:
    """Windows 主屏的物理分辨率（cv2 全屏窗口与 Qt 主屏都在这里）。

    找不到带 is_primary 标记的显示器时退回列表首项，行为与 eyetrax 原实现一致。
    """
    monitors = get_monitors()
    for monitor in monitors:
        if getattr(monitor, "is_primary", False):
            return int(monitor.width), int(monitor.height)
    return int(monitors[0].width), int(monitors[0].height)
