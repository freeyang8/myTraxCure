from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from mytraxcure.core.types import ParagraphGroup, Region, TextBlock


@dataclass #数据容器，自动初始化常用方法
class ViewportState:
    #必须是元组且有两个int类型
    window_origin: tuple[int, int] = (0, 0)  # 窗口左上在屏幕逻辑坐标中的位置
    scroll_offset: tuple[int, int] = (0, 0)  # 视口滚动偏移（逻辑像素），屏幕当前滚动到了哪里
    zoom: float = 1.0 
    dpi_scale: float = 1.0  # 物理/逻辑像素比，屏幕上每英寸长度内能显示多少个像素点
    current_page: int = 0  # 当前可见页；分页文档只在本页内做命中与聚合

class TextMapper:

    def __init__(self) -> None:
        self._blocks: list[TextBlock] = []
        self._viewport = ViewportState() #用来管理当前屏幕的位置、缩放等状态
        self._target_hwnd:int | None = None # 目标阅读窗口句柄（Windows）
        self._occlusion_provider = None
        # 段落聚合缓存：(para_key, page) → ParagraphGroup，翻页/重排/换文档时清空
        self._paragraph_cache: dict[tuple[str, int], ParagraphGroup] = {}

    #获取遮挡检测的窗口来源
    def set_target_window(self,hwnd:int | None = None,provider = None)->None:
        self._target_hwnd = hwnd #用于标识要检测的窗口
        self._occlusion_provider = provider #自定义的遮挡检测提供者

    # ---- 注册 / 清理 -----------------------------------------------------
    #用于从外部 接收并保存 解析好的文本块
    def register_blocks(self, blocks: Iterable[TextBlock]) -> None: #Iterable：表示 blocks 参数可以是列表、元组、生成器等任何可迭代对象
        self._blocks = list(blocks)
        self._paragraph_cache.clear() #块集合变了，段落聚合结果全部失效

    def clear(self) -> None:
        self._blocks.clear()
        self._paragraph_cache.clear()

    def update_viewport(   #“可选参数”设计
        self,
        window_origin: tuple[int, int] | None = None,
        scroll_offset: tuple[int, int] | None = None,
        zoom: float | None = None,
        dpi_scale: float | None = None,
        current_page: int | None = None
    ) -> None:
        if window_origin is not None:
            self._viewport.window_origin = window_origin
        if scroll_offset is not None:
            self._viewport.scroll_offset = scroll_offset
        if zoom is not None:
            self._viewport.zoom = zoom
        if dpi_scale is not None:
            self._viewport.dpi_scale = dpi_scale
        #翻页：命中范围与段落聚合都按页隔离，缓存必须失效
        if current_page is not None and current_page != self._viewport.current_page:
            self._viewport.current_page = current_page
            self._paragraph_cache.clear()

    # 屏幕坐标（Screen Point）转换为文档坐标
    def screen_to_document(self, screen_point: tuple[float, float]) -> tuple[float, float]:
        sx,sy = screen_point #鼠标在屏幕上的物理逻辑坐标
        vp = self._viewport #当前视口状态

        #把鼠标的屏幕坐标，减去窗口左上角在屏幕上的坐标,得到鼠标在窗口内部
        wx = sx - vp.window_origin[0]
        wy = sy - vp.window_origin[1]

        #鼠标在窗口内的位置 + 当前滚动的偏移量 = 鼠标在原始文档里的坐标
        vx = wx + vp.scroll_offset[0]
        vy = wy + vp.scroll_offset[1]

        if vp.zoom <= 0:
            raise ValueError (f"zoom 必须为正数，当前 {vp.zoom}")
        dx = vx / vp.zoom
        dy = vy / vp.zoom
        return (dx,dy)

    # 文档坐标 → 屏幕逻辑坐标（段落高亮矩形定位，需求 6.2 反向链路）
    def document_to_screen(self, doc_point: tuple[float, float]) -> tuple[float, float]:
        dx, dy = doc_point
        vp = self._viewport
        sx = dx * vp.zoom - vp.scroll_offset[0] + vp.window_origin[0]
        sy = dy * vp.zoom - vp.scroll_offset[1] + vp.window_origin[1]
        return (sx, sy)

    #命中测试,鼠标点击后，拿文档坐标，在文本块列表里查命中
    def hit_test(
        self,
        screen_point: tuple[float, float],
        tolerance_px: float = 0.0,
    ) -> TextBlock | None:
        dx, dy = self.screen_to_document(screen_point)
        tol = max(tolerance_px, 0.0)

        # 1. bbox 外膨胀 tolerance 后 contains → 直接命中（后注册的块优先，同原逻辑）
        for block in reversed(self._blocks):
            if (
                block.x - tol <= dx <= block.right + tol
                and block.y - tol <= dy <= block.bottom + tol
            ):
                return block

        # 2. 回退"最近块"：距离最小且 ≤ tolerance 才承认命中
        if tol > 0 and self._blocks:
            nearest = min(self._blocks, key=lambda b: b.distance_to(dx, dy))
            if nearest.distance_to(dx, dy) <= tol:
                return nearest
        return None

    # ---- 段落聚合（需求 6.8.7） ---------------------------------------
    @staticmethod
    def paragraph_key(block: TextBlock) -> str:
        # 解析期已写入 para_key（TXT/Word/PDF v4 解析器）→ 直接用
        if block.para_key:
            return block.para_key
        bid = block.block_id
        if not bid:
            return ""
        if block.source == "pdf":
            # p{page}-b{n}-l{l}-s{s} → p{page}-b{n}
            parts = bid.split("-")
            return "-".join(parts[:2]) if len(parts) >= 2 else bid
        if "-para" in bid:
            # TXT l{n}-para{p} → para{p}
            return bid.split("-", 1)[1]
        # Word para{p} / 其他 → block_id 本身
        return bid

    def paragraph_union_bbox(self, block: TextBlock) -> ParagraphGroup | None:
        key = self.paragraph_key(block)
        if not key:
            return None
        page = block.page
        cache_key = (key, page)
        cached = self._paragraph_cache.get(cache_key)
        if cached is not None:
            return cached

        members = [
            b for b in self._blocks
            if b.page == page and self.paragraph_key(b) == key
        ]
        if not members:
            return None

        x0 = min(b.x for b in members)
        y0 = min(b.y for b in members)
        x1 = max(b.right for b in members)
        y1 = max(b.bottom for b in members)
        group = ParagraphGroup(
            para_key=key,
            page=page,
            bbox=Region(x=int(x0), y=int(y0), w=int(x1 - x0), h=int(y1 - y0)),
            block_ids=[b.block_id for b in members],
        )
        self._paragraph_cache[cache_key] = group
        return group

    def blocks_in_region(self, region) -> list[TextBlock]:
        dx0,dy0 = self.screen_to_document( ( float(region.x),float(region.y) ) ) #左上角
        dx1,dy1 = self.screen_to_document( ( float(region.right), float(region.bottom) ) ) #右下角
        
        #边界框规范化
        left,right = min(dx0,dx1),max(dx0,dx1)
        top,bottom = min(dy0,dy1),max(dy0,dy1)
        
        #只考虑当前可见页，避免跨页命中
        page = self._viewport.current_page
        candidates = [b for b in self._blocks if b.page == page]

        hit = [
            b for b in candidates
            if b.x < right and b.x + b.w > left
            and b.y < bottom and b.y + b.h > top
        ]

        #AABB碰撞测试
        #b代表每一个元素，先按照b.y进行排序，再按照b.x进行排序
        hit.sort(key=lambda b : (b.y,b.x) )

        return hit

    # ---- 遮挡 ------------------------------------------------------------
    def is_occluded(self, screen_point: tuple[float, float]) -> bool:
        if self._target_hwnd is None:
            if self._occlusion_provider is not None:
                return bool ( self._occlusion_provider() )
            return False
        try:
            import win32gui
        except ImportError:
            return self._occlusion_provider() if self._occlusion_provider else False

        hwnd = self._target_hwnd

        #最小化
        if win32gui.IsIconic(hwnd):
            return True
        
        #失焦
        if win32gui.GetForegroundWindow() != hwnd:
            return True
        
        #点不在窗口范围内
        try:
            left,top,right,bottom = win32gui.GetWindowRect(hwnd)
        except win32gui.error:
            return True
        sx,sy = screen_point
        if not (left <= sx <= right and top <= sy <= bottom):
            return True
        
        #点级遮挡：取屏幕该点最顶层窗口，比对是否本窗口
        try:
            top_hwnd = win32gui.WindowFromPoint( #获取屏幕指定坐标点处最顶层窗口的句柄
                ( int(sx),int(sy) )
            )
            #获取指定窗口的根窗口
            root = win32gui.GetAncestor( top_hwnd,win32gui.GA_ROOT )#GA_ROOT,追溯到根窗口
            if root != hwnd:
                return True
        except win32gui.error:
            return True

        return False
