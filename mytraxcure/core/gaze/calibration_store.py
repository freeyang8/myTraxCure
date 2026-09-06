from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mytraxcure.core.config import CALIBRATION_DIR
from mytraxcure.core.gaze.gaze_wrapper import GazeWrapper


@dataclass(frozen=True)
class CalibrationEnvironment:

    screen_resolution: tuple[int, int]
    camera_id: int
    user_id: str

class CalibrationStore:

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else CALIBRATION_DIR
        self.directory.mkdir(parents=True, exist_ok=True)

    # ---- 路径命名 --------------------------------------------------------
    def path_for(self, env: CalibrationEnvironment) -> Path:
        w, h = env.screen_resolution
        name = f"{env.user_id}_{env.camera_id}_{w}x{h}.pkl"
        return self.directory / name

    # ---- 保存 / 加载 -----------------------------------------------------
    def save(self, wrapper: GazeWrapper, env: CalibrationEnvironment) -> Path:
        path = self.path_for(env)
        wrapper.save_model(path)
        return path

    def load(self, wrapper: GazeWrapper, env: CalibrationEnvironment) -> bool:
        path = self.path_for(env)
        if not path.exists():
            return False
        wrapper.load_model(path)
        return True

    def exists(self, env: CalibrationEnvironment) -> bool:
        return self.path_for(env).exists()

    # ---- 环境变化 --------------------------------------------------------
    def environment_changed(self, env: CalibrationEnvironment) -> bool:
        return not self.exists(env)

    def list_environments(self) -> list[Path]:
        return sorted(self.directory.glob("*.pkl"))

    def clear(self) -> None:
        for p in self.directory.glob("*.pkl"):
            p.unlink(missing_ok=True)
