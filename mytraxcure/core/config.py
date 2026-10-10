"""模块名称：config.py

功能描述：全应用配置的唯一定义处——各配置段 dataclass、数据目录路径常量，以及
    config.local.json 的读取与回退写入。被 ui / gaze / file / translation 共用。

关键约束：
    - 只依赖标准库。eyetrax 一类的重依赖一律函数内延迟导入，否则无摄像头、
      未装依赖的环境下连"读个配置"都会失败。
    - 这里的数值大多是实测经验值，注释里记着放宽/收紧的原因与原始取值；
      调参前先看注释，改完请把新的依据补回注释。
    - AppConfig.from_dict() 逐段显式还原 dataclass：新增配置段必须同步加一行，
      否则该段会退化成 dict，访问字段即 AttributeError。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---- 项目路径常量 ----------------------------------------------------------
# 以本文件位置反推仓库根（core/config.py 上三级），不用 CWD：
# 从别的目录启动时 CWD 会变，data/ 会指到别处
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CALIBRATION_DIR = DATA_DIR / "calibration"
UPLOADS_DIR = DATA_DIR / "uploads"
CACHE_DB_PATH = DATA_DIR / "cache.db"
CONFIG_PATH = PROJECT_ROOT / "config.local.json"

# model_name="auto" 时运行时构造估算器用的占位模型名：实际模型由校准 pkl 决定
_RUNTIME_PLACEHOLDER_MODEL = "ridge"

@dataclass
class GazeConfig:
    """眼动链路配置：回归模型选择、EMA 平滑强度、凝视稳定判定与头部姿态门控阈值。"""

    # auto：校准后按交叉验证误差自动选（ridge/elastic_net/tiny_mlp）
    # 也可指定具体模型：ridge/elastic_net/linear_svr/svr/tiny_mlp
    # 注意 eyetrax 0.4.0 里 svr 是 linear_svr 的别名（都是 LinearSVR，线性模型）
    model_name: str = "auto"
    # OpenCV 设备下标；取不到画面会在启动校准阶段降级为鼠标模式
    camera_index: int = 0
    # EMA 强度：eyetrax 公式 out = a*prev + (1-a)*new → a 越大越平滑但越滞后
    ema_alpha: float = 0.25
    # None → 交给 eyetrax 自动下载或读环境变量
    face_landmarker_model: str | None = None

    # ---- 凝视稳定判定 --------------------------
    # 阈值整体放宽：眼动输入精度有限，过严的判定会让段落选中概率过低
    dwell_seconds: float = 0.6   # 驻留时长（原 0.8，缩短后触发更快）
    # True → 段落模式；无块或超大块时自动回退点模式
    paragraph_mode: bool = True

    # 段落模式阈值
    # 窗口内 para_key 众数占比下限（原 0.6，放宽以容忍更多落空帧）
    dominant_ratio: float = 0.5
    # 磁滞：切到新段所需的连续命中帧数（原 4），防止视线扫过时反复跳段
    min_dominant_frames: int = 3

    # 命中容差本意是 min(校准中位误差, block_h * tolerance_ratio_of_block_h)，
    # 但校准误差目前取不到，实际恒用 hit_tolerance_px，不随本次校准收敛
    hit_tolerance_px: float = 100.0
    tolerance_ratio_of_block_h: float = 0.7  # 最近块回退容差的块高占比
    # 逐句模式的请求容差：比连续滚动大得多（大字号 + 一句话就是目标）。
    # 实际取值还会被"到相邻句空隙的一半"夹住（TextBlock.hit_pad_max），
    # 所以给多大都行——相邻句的命中域不会相交，不会点错句
    step_hit_tolerance_px: float = 200.0

    # 点模式阈值（像素）：窗口内凝视点的离散程度上限
    max_variance_px: float = 60.0
    # 质心最大偏移上限；段落模式下阈值改为 max(此值, 块高)，块越高容忍越大
    max_offset_px: float = 40.0

    # 头部姿态阈值（度）：主流程每帧据此丢弃偏转帧，与眨眼帧同处理。
    # 只比 yaw/pitch 不比 roll——eyetrax 正脸 roll≈±π 且会环绕跳变，
    # 取绝对值会把正脸也判成超限
    max_head_angle_deg: float = 15.0

    def runtime_model_name(self) -> str:
        """把配置里的模型名解析成 eyetrax 认得的具体模型名。

        Returns:
            str：可直接传给 eyetrax 的模型名。配置写 "auto"，或写了一个
            eyetrax 不认识的非法名字时，返回占位模型 ridge。

        Notes:
            "auto" 只是校准期的选择策略，create_model 不认这个名字，所以运行期
            先用占位模型构造 estimator，随后 load_model 用 pkl 里校准好的模型
            整体替换。配置文件可手改，故每次都真调一次 create_model 做实测校验，
            把非法名字统一兜在这里，避免构造阶段才抛异常。
            降级原因会打到 stderr：静默换模型会让"调了半天没效果"无从排查。
        """
        if self.model_name == "auto":
            return _RUNTIME_PLACEHOLDER_MODEL
        try:
            from eyetrax.models import create_model  # 延迟导入：config 保持无重依赖

            create_model(self.model_name)
            return self.model_name
        except Exception as exc:
            print(
                f"[config] 回归模型 {self.model_name!r} 不可用（{exc}），"
                f"已改用 {_RUNTIME_PLACEHOLDER_MODEL}",
                file=sys.stderr,
            )
            return _RUNTIME_PLACEHOLDER_MODEL

@dataclass
class RegionConfig:
    """点模式框选区域（Region）的默认尺寸与上限，同时充当段落并集 bbox 的合法性判据。"""

    default_width: int = 400
    default_height: int = 120
    # 上限须 ≥ TXT 内容宽度 _TXT_WRAP_WIDTH(960)，否则长标题/段落必超限，
    # 会被 lock_block 判为"整页一大块"而回退点模式（蓝框只能框住局部）
    max_width: int = 1000  # 同时作为段落并集 bbox 的宽度上限
    max_height: int = 400  # 同时作为段落并集 bbox 的高度上限（折行后多行段落可达 300+）

@dataclass
class TranslationConfig:
    """翻译相关配置：后端与模型、连接超时与重试、分段长度、术语表与后台预翻译。"""

    backend: str = "ollama"  # 后端；目前只实现 ollama
    # 本地 Ollama 里的模型标签，换机器/换模型时改这里，须与已拉取的标签一致
    model: str = "hy-mt1.5-1.8b-fixed:latest"
    ollama_url: str = "http://127.0.0.1:11434"  # 默认端口 11434，改过要同步
    source_lang: str = "auto"  # 源语言；auto 交给模型判断
    target_lang: str = "zh"  # 目标语言

    timeout_s: float = 60.0  # 单次请求超时（秒）
    max_retries: int = 2  # 重试次数（总尝试次数 = 该值 + 1）
    retry_interval_s: float = 1.0  # 两次重试之间的间隔（秒）
    max_chars_per_segment: int = 800  # 超长自动分段

    term_table_path: str | None = None  # CSV 术语表：原文,译文

    # 加载文档后按阅读顺序在后台预翻译（只写缓存；用户按空格时命中缓存即时出译文）
    pre_translate: bool = True
    # 单次最多预翻译多少段，避免整本书跑满本地模型
    pre_translate_max_segments: int = 30

@dataclass
class ReadingConfig:
    """逐句步进阅读的排版参数：一屏放几句、字号行高与边距。"""

    # 逐句步进阅读：一屏只放能完整显示的 N 句，滚轮一次走一句（N 按窗口高度算）。
    # 默认值取自原型 tools/step_reader_prototype.html 在 1280x787 窗口实测的一组参数
    step_mode: bool = True       # 启动即进逐句步进；菜单里可切回连续滚动
    font_px: int = 44            # 字号（像素）
    line_height_em: float = 2.5  # 行高（倍字号）
    gap_em: float = 2.9          # 段间距目标值：先按它求 N，剩余空间再均分进去
    max_gap_em: float = 6.0      # 段间距上限，超出的空间转成上下居中留白
    min_font_px: int = 16        # 超长句缩字号的保留下限
    margin_x_min: int = 40       # 左右边距下限（像素）
    margin_x_vw: float = 0.06    # 左右边距（视口宽的占比）
    margin_y: int = 24           # 上下边距（像素）


@dataclass
class CalibrationConfig:
    """校准采集配置：点位模式、每点采集时长与每点样本数下限。"""

    # 5_point / 9_point / 14_point（默认）/ dense（5x5=25 点）/ lissajous / adaptive
    mode: str = "14_point"
    # 每个点的采集时长（秒），网格模式生效；脉冲提示固定 0.8s 不计入
    sample_seconds_per_point: float = 1.5
    # 每点样本数下限，低于只告警不失败（眨眼/丢脸会导致帧数偏少）
    min_frames_per_point: int = 30

@dataclass
class AppConfig:
    """顶层配置容器：五个配置段 + 用户标识，是配置文件与运行时之间唯一的形态。"""

    gaze: GazeConfig = field(default_factory=GazeConfig)
    region: RegionConfig = field(default_factory=RegionConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    reading: ReadingConfig = field(default_factory=ReadingConfig)

    user_id: str = "default"

    def to_dict(self) -> dict[str, Any]:
        """展开为嵌套 dict；嵌套 dataclass 会被 asdict 递归拍平，不再保有字段访问。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        """从配置文件的原始 dict 还原 AppConfig，缺失的字段一律用默认值兜底。

        Args:
            data: json.loads 的结果。允许缺字段、缺整段、字段值为 None；
                默认值里没有的多余键会被忽略。

        Returns:
            AppConfig：五个配置段一定是 dataclass 实例，不会是 plain dict。

        Raises:
            TypeError：某配置段存在但类型不对（例如 gaze 是字符串）时，
            由 GazeConfig(**...) 等构造调用抛出。

        Notes:
            合并规则是"payload 为 None 才回退默认值"，所以显式写的 false / 0 / ""
            都会被保留，不会被默认值覆盖。
        """
        # 逐段用默认值兜底，保证旧配置文件缺字段也能加载
        def merge(default: Any, payload: Any) -> Any:
            """递归合并：payload 为 None 的字段用默认值，其余原样保留。"""
            if isinstance(default, dict) and isinstance(payload, dict):
                return {
                    k: merge(v, payload.get(k)) for k, v in default.items()
                }
            return payload if payload is not None else default

        merged = merge(asdict(cls()), data)
        # asdict() 把嵌套 dataclass 拍平成 dict，这里必须显式还原，
        # 否则 cfg.gaze 等是 plain dict，访问字段即 AttributeError。
        # 注意：AppConfig 新增配置段时此处需同步添加。
        return cls(
            gaze=GazeConfig(**merged["gaze"]),
            region=RegionConfig(**merged["region"]),
            translation=TranslationConfig(**merged["translation"]),
            calibration=CalibrationConfig(**merged["calibration"]),
            reading=ReadingConfig(**merged["reading"]),
            user_id=merged["user_id"],
        )

class ConfigManager:
    """config.local.json 的读写入口，并记录最近一次 load 到底用的是文件还是默认值。

    使用约束：last_source 由 load() 独占维护，save() 不改动它；要判断"配置是否
    真的生效过"，必须在 load() 之后立刻读，晚读会被后续 load 覆盖。
    """

    def __init__(self, path: Path | None = None) -> None:
        """绑定配置文件路径。

        Args:
            path: 配置文件路径；None 表示用仓库根的 config.local.json。
        """
        self.path = Path(path) if path else CONFIG_PATH
        # 本次 load 的配置来源："file" = 自定义配置 / "default" = 默认值（缺失或损坏回退）
        self.last_source: str = "default"

    def load(self) -> AppConfig:
        """读取并解析配置文件；任何失败都退化为一份全新的默认配置，不抛异常。

        Returns:
            AppConfig：文件存在且合法时按文件内容构造（缺字段补默认值）；
            文件不存在、JSON 解析失败或读取出错时返回 AppConfig()。

        Notes:
            会先把 self.last_source 置为 "default"，仅在成功解析后才改成 "file"。
            损坏时只往 stderr 打一行原因：配置失效不应阻断启动，但静默降级会
            让人以为参数改过了，故至少留一条可见日志。
        """
        self.last_source = "default"
        if not self.path.exists():
            return AppConfig()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # 静默降级会掩盖配置失效：至少把原因打到 stderr，调参时可见
            print(
                f"[config] 配置文件损坏或不可读，已回退默认配置: {self.path}（{type(exc).__name__}: {exc}）",
                file=sys.stderr,
            )
            return AppConfig()
        self.last_source = "file"
        return AppConfig.from_dict(data)

    def save(self, config: AppConfig) -> None:
        """把配置写回文件（UTF-8、缩进 2、中文不转义），父目录不存在时自动创建。

        Args:
            config: 待持久化的配置，写入前会经 to_dict() 展开为嵌套 dict。

        Notes:
            写盘不改变 last_source——它只描述最近一次 load 的来源。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

