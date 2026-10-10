"""模块名称：text_mapper.py

功能描述：文档坐标 ↔ 屏幕坐标换算、点/区域命中、段落聚合的统一入口。在渲染期回填好
bbox 的 TextBlock 集合上找出被凝视或被点击的块，并把同一段落的多个块聚合成
ParagraphGroup，供高亮框绘制与翻译单元取用。

关键约束：
- 本模块读到的全部几何信息都来自 TextBlock 上已回填的 bbox。解析期结束时 TXT/Word
  的坐标恒为 0，此时命中与段落聚合的结果都不可用，必须先经过一次重排渲染。
- 段落聚合结果按 (para_key, page) 缓存；凡是改变这两者的操作（注册块、翻页、清空）
  都必须同时清缓存，否则会拿到上一版版面的旧 bbox。
- 遮挡判定依赖 Windows 的 win32gui；未安装或不在 Windows 上时一律降级为"未遮挡"，
  即检测失效时按可读处理，宁可多命中一次也不把整段判成不可读。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from mytraxcure.core.file.segmenter import group_paragraphs, paragraph_text
from mytraxcure.core.types import ParagraphGroup, Region, TextBlock


@dataclass
class ViewportState:
    """视口状态快照：窗口原点、滚动偏移与当前可见页。

    三者共同决定屏幕坐标与文档坐标的换算关系。打包成数据类而不是散着传三个值，
    是为了让正向换算、反向换算、命中测试、区域查询共用同一份状态，避免调用方写反顺序。
    """

    window_origin: tuple[int, int] = (0, 0)  # 窗口左上在屏幕逻辑坐标中的位置
    scroll_offset: tuple[int, int] = (0, 0)  # 视口滚动偏移（逻辑像素），屏幕当前滚动到了哪里
    current_page: int = 0  # 当前可见页；分页文档只在本页内做命中与聚合


class TextMapper:
    """屏幕↔文档坐标的换算器，兼做命中测试与段落聚合。

    生命周期是寄存器式的：由渲染器在每次排版完成后调 register_blocks 整体替换块集合，
    由视图在窗口移动、滚动、翻页时调 update_viewport 更新换算关系。本类自身不排版、
    不渲染，也不持有 TextBlock 之外的任何版面信息，因此每次重排都整体重注册即可。
    """

    def __init__(self) -> None:
        """建立空的块列表与默认视口状态。

        此刻尚无任何命中与聚合能力，调用方必须先注册至少一个文本块。
        """
        self._blocks: list[TextBlock] = []
        self._viewport = ViewportState()
        self._target_hwnd:int | None = None # 目标阅读窗口句柄（Windows）
        self._occlusion_provider = None # 自定义遮挡检测回调：非 Windows 平台或测试时兜底
        # 段落聚合缓存：(para_key, page) → ParagraphGroup，翻页/重排/换文档时清空
        self._paragraph_cache: dict[tuple[str, int], ParagraphGroup] = {}

    def set_target_window(self,hwnd:int | None = None,provider = None)->None:
        """接入遮挡检测来源。

        Args:
            hwnd: 阅读窗口句柄，供 win32gui 判定最小化、失焦与点级遮挡；
                传 None 表示不走 Win32 判定。
            provider: 无参返回 bool 的自定义遮挡回调，在 Win32 判定不可用时兜底。
        """
        self._target_hwnd = hwnd
        self._occlusion_provider = provider

    # ---- 注册 / 清理 -----------------------------------------------------
    def register_blocks(self, blocks: Iterable[TextBlock]) -> None:
        """整体替换文本块集合，供命中与聚合使用。

        Args:
            blocks: 任意可迭代的文本块序列（列表、元组、生成器均可）。
                必须是已完成 bbox 回填的同一版版面，混合两版坐标会让并集框横跨全文。

        Notes:
            这里是整体替换而非追加：块集合变了聚合结果全部失效，缓存一并清空。
        """
        self._blocks = list(blocks)
        self._paragraph_cache.clear()

    def clear(self) -> None:
        """清空文本块与聚合缓存，用于关闭文档或切换到眼动之外的输入模式。"""
        self._blocks.clear()
        self._paragraph_cache.clear()

    def update_viewport(
        self,
        window_origin: tuple[int, int] | None = None,
        scroll_offset: tuple[int, int] | None = None,
        current_page: int | None = None
    ) -> None:
        """局部更新视口状态。

        Args:
            window_origin: 窗口左上角在屏幕逻辑坐标中的位置，None 表示保持原值。
            scroll_offset: 视口滚动偏移（逻辑像素），None 表示保持原值。
            current_page: 当前可见页，None 表示保持原值。

        Notes:
            三个参数都设计成可选，是因为它们由不同事件驱动（窗口移动 / 滚动 / 翻页），
            调用方通常一次只知道其中一个。只有页码真正变化时才清缓存——
            命中范围与段落聚合都按页隔离，同页内的滚动不需要重算聚合。
        """
        if window_origin is not None:
            self._viewport.window_origin = window_origin
        if scroll_offset is not None:
            self._viewport.scroll_offset = scroll_offset
        #翻页：命中范围与段落聚合都按页隔离，缓存必须失效
        if current_page is not None and current_page != self._viewport.current_page:
            self._viewport.current_page = current_page
            self._paragraph_cache.clear()

    def screen_to_document(self, screen_point: tuple[float, float]) -> tuple[float, float]:
        """屏幕坐标 → 文档坐标。

        Args:
            screen_point: (x, y) 屏幕逻辑像素坐标，通常是鼠标位置或凝视点。

        Returns:
            文档坐标 (dx, dy)，单位与 TextBlock 上的几何字段一致（渲染回填时的像素）。
        """
        sx,sy = screen_point #鼠标在屏幕上的物理逻辑坐标
        vp = self._viewport

        #减窗口原点得窗口内坐标，再加滚动偏移得文档坐标
        wx = sx - vp.window_origin[0]
        wy = sy - vp.window_origin[1]

        dx = wx + vp.scroll_offset[0]
        dy = wy + vp.scroll_offset[1]
        return (dx,dy)

    def document_to_screen(self, doc_point: tuple[float, float]) -> tuple[float, float]:
        """文档坐标 → 屏幕逻辑坐标，供段落高亮矩形回到屏幕上定位。

        Args:
            doc_point: (x, y) 文档坐标，通常来自 TextBlock 或段落并集 bbox。

        Returns:
            屏幕逻辑像素坐标 (sx, sy)。这是 screen_to_document 的逆运算，
            两者共用同一份视口状态，窗口移动或滚动后必须重新求值。
        """
        dx, dy = doc_point
        vp = self._viewport
        sx = dx - vp.scroll_offset[0] + vp.window_origin[0]
        sy = dy - vp.scroll_offset[1] + vp.window_origin[1]
        return (sx, sy)

    @staticmethod
    def _effective_tol(block: TextBlock, tol: float) -> float:
        """求该块实际生效的容差：块自己的容差上限优先。

        Args:
            block: 待判定的文本块，读取它的 hit_pad_max。
            tol: 调用方给的全局容差（像素）。

        Returns:
            块自身上限不为 None 时取二者较小值，否则返回全局容差。

        Notes:
            逐句模式下由版面按"到相邻块的半个空隙"算出上限，相邻命中域正好在中线相接，
            于是全局容差可以开到很大而不会串到相邻句。
        """
        if block.hit_pad_max is None:
            return tol
        return min(tol, block.hit_pad_max)

    def hit_test(
        self,
        screen_point: tuple[float, float],
        tolerance_px: float = 0.0,
    ) -> TextBlock | None:
        """按屏幕点找命中的文本块，先精确包含判定，再按距离回退。

        Args:
            screen_point: 屏幕逻辑像素坐标（凝视点或鼠标位置）。
            tolerance_px: 向外的容错距离（像素）。眼视/眼动输入的抖动远大于一个字符尺寸，
                所以做大了很多；鼠标模式通常传 0 走精确判定。

        Returns:
            命中的 TextBlock；没有任何候选且不满足回退条件时返回 None。

        Notes:
            判定逻辑集中在 TextBlock.contains 里，这里只负责编排候选与取舍。
            多候选时取"离凝视点最近"的块，而不是列表里最后一个：重排版面的段间距
            只有 0.5 行高，容差 100px 会让上下相邻几段的命中域全部重叠，
            按逆序取第一个会稳定选中下方 1~3 段。距离并列时取靠上的段。
            回退阶段只有当最近块的距离不超过它自己的有效容差才承认命中，
            否则等于把整页都判成命中。
        """
        dx, dy = self.screen_to_document(screen_point)
        tol = max(tolerance_px, 0.0)

        # 1. 命中候选（含眼动容差的外膨胀，判定逻辑集中在 TextBlock.contains）
        candidates = [b for b in self._blocks if b.contains(dx, dy, self._effective_tol(b, tol))]
        if candidates:
            return min(candidates, key=lambda b: (b.distance_to(dx, dy), b.y, b.x))

        # 2. 回退"最近块"：距离最小且 ≤ 该块自己的容差才承认命中
        if tol > 0 and self._blocks:
            nearest = min(self._blocks, key=lambda b: b.distance_to(dx, dy))
            if nearest.distance_to(dx, dy) <= self._effective_tol(nearest, tol):
                return nearest
        return None

    # ---- 段落（重排后凝视选中的唯一单位）--------------------------------
    def paragraph_blocks(self, para_key: str) -> list[TextBlock]:
        """取同一段落的全部文本块。

        Args:
            para_key: 重排后写入的段落标识（统一为 seg{n} 或 sentence-{n}）。

        Returns:
            该段的 TextBlock 列表，沿用注册时的顺序；段落不存在时为空列表。
        """
        return [b for b in self._blocks if b.para_key == para_key]

    def segment_text(self, para_key: str) -> str:
        """段落文本的唯一出处：预翻译与即时翻译都从这里取，缓存键才会一致。

        Args:
            para_key: 段落标识。

        Returns:
            按行拼接后的段落文本；段落为空时返回空字符串。
        """
        return paragraph_text(self.paragraph_blocks(para_key))

    def segment_key_parts(self, para_key: str) -> tuple[str, str, int, str] | None:
        """段落的 (para_key, 文本, page, 首块 block_id)；无文本段返回 None。

        Args:
            para_key: 段落标识。

        Returns:
            四元组，与 segments() 的元素同形状，可直接交给 make_segment_key 建缓存键；
            段落不存在或文本为纯空白时返回 None。
        """
        blocks = self.paragraph_blocks(para_key)
        text = paragraph_text(blocks)
        if not blocks or not text.strip():
            return None
        return para_key, text, blocks[0].page, blocks[0].block_id

    def region_key_parts(self, region: Region) -> tuple[str, str, int, str] | None:
        """区域模式的单位取值：区域内 blocks 按阅读顺序拼成文本。

        Args:
            region: 屏幕坐标下的矩形区域。

        Returns:
            (para_key, 文本, page, 首块 block_id)；区域内无块或拼出的文本为纯空白时返回 None。

        Notes:
            取区域内首块的 para_key 作为键，因此跨越多个段落的区域会按首块归属成一段。
        """
        blocks = self.blocks_in_region(region)
        if not blocks:
            return None
        text = paragraph_text(blocks)
        if not text.strip():
            return None
        return blocks[0].para_key, text, blocks[0].page, blocks[0].block_id

    def segments(self) -> list[tuple[str, str, int, str]]:
        """按阅读顺序返回各段的 (para_key, 文本, page, 首块 block_id)。

        Returns:
            顺序即重排版面从上到下的顺序，也正是用户的阅读顺序——预翻译照此排队，
            先读到的一段更早拿到译文。
        """
        out: list[tuple[str, str, int, str]] = []
        for para_key, blocks in group_paragraphs(self._blocks):
            text = paragraph_text(blocks)
            if text.strip():
                out.append((para_key, text, blocks[0].page, blocks[0].block_id))
        return out

    # ---- 段落聚合 -------------------------------------------------------
    @staticmethod
    def paragraph_key(block: TextBlock) -> str:
        """反解 block 属于哪个段落。

        Args:
            block: 单个文本块。

        Returns:
            段落标识；block_id 缺失时返回空字符串。

        Notes:
            优先用解析期或重排期写入的 para_key，拿不到时才按来源格式反解 block_id
            （PDF 取 p{页}-b{块}，TXT 取 l{行}-para{段} 的后半截，Word 用整个 id）。
        """
        # 解析期已写入 para_key（TXT/Word/PDF 解析器）→ 直接用
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
        """把 block 所在段落的所有块聚合成一个并集框。

        Args:
            block: 段落内任意一个块，用来定位段落与页码。

        Returns:
            带并集 bbox 与成员 block_id 列表的 ParagraphGroup；算不出段落标识或
            当前没有该段成员时返回 None。

        Notes:
            结果按 (para_key, page) 缓存。高亮框每帧都可能被调用，单次算 union 要遍历
            全部块；缓存的清空时机见 register_blocks / update_viewport / clear。
        """
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
        """取与给定屏幕矩形相交的全部文本块。

        Args:
            region: 屏幕坐标下的矩形，需带 x / y / right / bottom 四个属性。

        Returns:
            与区域相交的块，已按阅读顺序（先 y 后 x）排序，可直接拼成段落文本。

        Notes:
            只考虑当前可见页，避免跨页命中。用 AABB 相交而非包含：区域常切在某个视觉行
            中间，用包含会漏掉刚好被边界切到的块。
        """
        dx0,dy0 = self.screen_to_document( ( float(region.x),float(region.y) ) ) #左上角
        dx1,dy1 = self.screen_to_document( ( float(region.right), float(region.bottom) ) ) #右下角

        #边界框规范化：拖拽方向任意，先取 min/max 再比较
        left,right = min(dx0,dx1),max(dx0,dx1)
        top,bottom = min(dy0,dy1),max(dy0,dy1)

        #只考虑当前可见页，避免跨页命中
        page = self._viewport.current_page
        candidates = [b for b in self._blocks if b.page == page]

        #AABB 相交测试
        hit = [
            b for b in candidates
            if b.x < right and b.x + b.w > left
            and b.y < bottom and b.y + b.h > top
        ]

        #按阅读顺序排：先上后下（b.y），同行再从左到右（b.x）
        hit.sort(key=lambda b : (b.y,b.x) )

        return hit

    # ---- 遮挡 ------------------------------------------------------------
    def is_occluded(self, screen_point: tuple[float, float]) -> bool:
        """判定该屏幕点所属的窗口是否被最小化、失焦或被别的窗口盖住。

        Args:
            screen_point: 屏幕逻辑像素坐标。

        Returns:
            True 表示不可用于注视判定；检测手段不可用时一律返回 False（按未遮挡处理）。

        Notes:
            四级判定依次是：最小化 → 失焦 → 点落在窗口矩形之外 → 该点最顶层窗口不是本窗口。
            任一步拿不到句柄信息都按"被遮挡"处理，只有一开始就没接入 Win32 判定时才走
            provider 或返回 False。
        """
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
            top_hwnd = win32gui.WindowFromPoint(
                ( int(sx),int(sy) )
            )
            #追溯到根窗口再比对：顶层可能是本窗口的某个子控件
            root = win32gui.GetAncestor( top_hwnd,win32gui.GA_ROOT )
            if root != hwnd:
                return True
        except win32gui.error:
            return True

        return False
