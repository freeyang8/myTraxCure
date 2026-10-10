"""模块名称：calibration_store.py

功能描述：校准 pkl 的落盘与检索。按"用户 + 摄像头 + 屏幕分辨率"三元组给校准文件命名，
据此判断当前环境有没有可用校准，并提供保存/加载/清理。

关键约束：文件名即环境指纹，三元组任一变化都会指向另一个 pkl；因此"环境是否变了"
在本模块里等价于"当前三元组有没有对应文件"。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mytraxcure.core.config import CALIBRATION_DIR
from mytraxcure.core.gaze.gaze_wrapper import GazeWrapper


@dataclass(frozen=True)
class CalibrationEnvironment:
    """校准环境指纹：三个字段共同决定 pkl 文件名，是 store 唯一的存取键。"""

    screen_resolution: tuple[int, int]  # 主屏物理分辨率 (w, h)：分辨率变了标定坐标系就失效
    camera_id: int                      # 摄像头序号：换摄像头等于换一套特征分布
    user_id: str                        # 同一台机器多人使用时靠它隔开各自的校准

class CalibrationStore:
    """校准 pkl 目录的读写封装：目录默认取 config.CALIBRATION_DIR，构造时即创建。"""

    def __init__(self, directory: Path | None = None) -> None:
        """绑定校准目录。

        Args:
            directory: pkl 存放目录；None 表示用 config.CALIBRATION_DIR。

        Notes:
            构造时就 mkdir(parents=True, exist_ok=True)：后续 save 不再处理建目录失败。
        """
        self.directory = Path(directory) if directory else CALIBRATION_DIR
        self.directory.mkdir(parents=True, exist_ok=True)

    # ---- 路径命名 --------------------------------------------------------
    def path_for(self, env: CalibrationEnvironment) -> Path:
        """按环境三元组拼出 pkl 路径（只拼名，不检查文件是否存在）。"""
        w, h = env.screen_resolution
        name = f"{env.user_id}_{env.camera_id}_{w}x{h}.pkl"
        return self.directory / name

    # ---- 保存 / 加载 -----------------------------------------------------
    def save(self, wrapper: GazeWrapper, env: CalibrationEnvironment) -> Path:
        """把 wrapper 当前模型写到该环境的 pkl（已存在则覆盖），返回写入路径。"""
        path = self.path_for(env)
        wrapper.save_model(path)
        return path

    def load(self, wrapper: GazeWrapper, env: CalibrationEnvironment) -> bool:
        """把该环境的 pkl 载入 wrapper，整体替换构造时的占位模型。

        Returns:
            True 已加载；False 该文件不存在。

        Notes:
            文件存在但损坏或与当前 eyetrax 版本不兼容时，异常由底层抛出且此处不捕获，
            调用方需自己兜（见 ui/main_window 的启用眼动流程）。
        """
        path = self.path_for(env)
        if not path.exists():
            return False
        wrapper.load_model(path)
        return True

    def exists(self, env: CalibrationEnvironment) -> bool:
        """该环境是否已有校准 pkl（启动期三态决策与"是否重新校准"都依据它）。"""
        return self.path_for(env).exists()

    # ---- 环境变化 --------------------------------------------------------
    def environment_changed(self, env: CalibrationEnvironment) -> bool:
        """判断校准环境是否"变过"，实现为当前三元组没有对应 pkl。

        Notes:
            只回答"当前环境有没有可用校准"，不追溯是否曾为其它分辨率/摄像头/用户校准过，
            因此换屏、换摄像头、换用户 id 都会判为变化并要求重新校准。
        """
        return not self.exists(env)

    def list_environments(self) -> list[Path]:
        """列出目录下所有 pkl（按路径排序），供界面展示或核对已有哪些校准。"""
        return sorted(self.directory.glob("*.pkl"))

    def clear(self) -> None:
        """删除目录下所有 pkl——不分用户与环境，设置界面的"清除校准"即走这里。"""
        for p in self.directory.glob("*.pkl"):
            p.unlink(missing_ok=True)
