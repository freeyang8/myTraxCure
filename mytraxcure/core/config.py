from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---- 项目路径常量 ----------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CALIBRATION_DIR = DATA_DIR / "calibration"
UPLOADS_DIR = DATA_DIR / "uploads"
CACHE_DB_PATH = DATA_DIR / "cache.db"
CONFIG_PATH = PROJECT_ROOT / "config.local.json"

@dataclass
class GazeConfig:

    model_name: str = "ridge"  # ridge/elastic_net/linear_svr/svr/tiny_mlp
    camera_index: int = 0
    ema_alpha: float = 0.25        # EMA 强度（平滑算法固定 kalman_ema，此值待实测调优）
    face_landmarker_model: str | None = None  # None → eyetrax 自动下载/环境变量

    # ---- 凝视稳定判定 --------------------------
    # 阈值整体放宽：眼动输入精度有限，过严的判定会让段落选中概率过低
    dwell_seconds: float = 0.6   # 驻留时长（原 0.8，缩短后触发更快）
    paragraph_mode: bool = True  # True → 段落模式；无块/超大块时自动回退点模式

    # 段落模式阈值
    dominant_ratio: float = 0.5  # 窗口内 para_key 众数占比下限（原 0.6，容忍更多落空帧）
    min_dominant_frames: int = 3  # 磁滞：切到新段所需连续命中帧数（原 4）

    # 命中容差：tolerance = min(校准中位误差, block_h * tolerance_ratio_of_block_h)
    hit_tolerance_px: float = 100.0  # 首选命中容差（v6：校准误差不可获取，恒用默认值）
    tolerance_ratio_of_block_h: float = 0.7  # 最近块回退容差的块高占比

    # 点模式阈值
    max_variance_px: float = 60.0
    max_offset_px: float = 40.0

    # 头部姿态阈值：yaw/pitch/roll 任一角超过则暂停选区
    max_head_angle_deg: float = 15.0

@dataclass
class RegionConfig:

    default_width: int = 400
    default_height: int = 120
    # 上限须 ≥ TXT 内容宽度 _TXT_WRAP_WIDTH(960)，否则长标题/段落必超限，
    # 会被 lock_block 判为"整页一大块"而回退点模式（蓝框只能框住局部）
    max_width: int = 1000  # 同时作为段落并集 bbox 的宽度上限
    max_height: int = 400  # 同时作为段落并集 bbox 的高度上限（折行后多行段落可达 300+）

@dataclass
class TranslationConfig:

    backend: str = "ollama"  # 翻译后端（当前仅 ollama）
    model: str = "hy-mt1.5-1.8b-fixed:latest" #这里修改自己的模型
    ollama_url: str = "http://127.0.0.1:11434" #确认ollama运行端口
    source_lang: str = "auto"
    target_lang: str = "zh"

    timeout_s: float = 60.0
    max_retries: int = 2  #超时重连次数
    retry_interval_s: float = 1.0
    max_chars_per_segment: int = 800  # 超长自动分段

    term_table_path: str | None = None  # CSV 术语表：原文,译文

@dataclass
class CalibrationConfig:

    mode: str = "9_point"  # 5_point / 9_point / dense / lissajous / adaptive
    sample_seconds_per_point: float = 1.5  # eyetrax 默认 1.0，需调长
    min_frames_per_point: int = 30

@dataclass
class AppConfig:

    gaze: GazeConfig = field(default_factory=GazeConfig)
    region: RegionConfig = field(default_factory=RegionConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)

    user_id: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        # 逐段用默认值兜底，保证旧配置文件缺字段也能加载
        def merge(default: Any, payload: Any) -> Any:
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
            user_id=merged["user_id"],
        )

class ConfigManager:

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else CONFIG_PATH
        # 本次 load 的配置来源："file" = 自定义配置 / "default" = 默认值（缺失或损坏回退）
        self.last_source: str = "default"

    def load(self) -> AppConfig:
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        
