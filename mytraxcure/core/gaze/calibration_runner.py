"""校准采集 + 回归模型优选。

eyetrax 自带的校准函数在内部训练并丢弃 (X, y)，换个模型就得让用户重做一遍校准，
也无法比较模型误差。这里自己采集网格点的特征/目标（复用 eyetrax 的采集循环），
同一份数据跑按注视点分组的交叉验证，挑误差最小的模型再全量训练。
"""

from __future__ import annotations

import math
import sys
import time
import warnings
from dataclasses import dataclass, field

import cv2
import numpy as np
from sklearn.exceptions import ConvergenceWarning
from eyetrax.calibration import run_lissajous_calibration
from eyetrax.calibration.adaptive import run_adaptive_calibration
from eyetrax.calibration.common import (
    compute_grid_points,
    compute_grid_points_from_shape,
    wait_for_face_and_countdown,
)
from eyetrax.models import BaseModel, create_model
from eyetrax.utils.screen import get_screen_size
from eyetrax.utils.video import open_camera

from mytraxcure.core.config import CalibrationConfig, GazeConfig

# model_name="auto" 的候选模型。eyetrax 0.4.0 里 "svr" 是 "linear_svr" 的别名
# （同一个 LinearSVR，仍是线性），真正的非线性只有 tiny_mlp。
AUTO_MODEL_CANDIDATES: tuple[str, ...] = ("ridge", "elastic_net", "tiny_mlp")

GRID_MODES: tuple[str, ...] = ("5_point", "9_point", "dense")
DELEGATED_MODES: tuple[str, ...] = ("lissajous", "adaptive")

_DEFAULT_MODE = "9_point"
_ORDER_5 = [(1, 1), (0, 0), (2, 0), (0, 2), (2, 2)]
_ORDER_9 = [
    (1, 1), (0, 0), (2, 0), (0, 2), (2, 2), (1, 0), (0, 1), (2, 1), (1, 2),
]
_DENSE_ROWS, _DENSE_COLS = 5, 5
_WINDOW_NAME = "Calibration"  # 与 wait_for_face_and_countdown 建的全屏窗口同名
_PULSE_SECONDS = 0.8  # 点闪动提示（不开采），比 eyetrax 默认 1.0 略快
_COUNTDOWN_SECONDS = 2
_CV_FOLDS = 5
_CV_SEED = 0

_MODE_LABELS = {
    "5_point": "5 点",
    "9_point": "9 点",
    "dense": f"{_DENSE_ROWS * _DENSE_COLS} 点密集网格",
    "lissajous": "李萨如曲线",
    "adaptive": "自适应（9 点 + 60 随机点）",
}


def describe_mode(mode: str) -> str:
    return _MODE_LABELS.get(mode, f"{mode}（未知模式）")


def normalize_mode(mode: str) -> str:
    if mode in GRID_MODES or mode in DELEGATED_MODES:
        return mode
    print(
        f"[calibration] 未知校准模式 {mode!r}，回退 {_DEFAULT_MODE}",
        file=sys.stderr,
    )
    return _DEFAULT_MODE


# ---- 数据容器 --------------------------------------------------------------
@dataclass
class SampleSet:

    X: np.ndarray
    y: np.ndarray
    groups: np.ndarray  # 每个样本所属注视点编号
    n_points: int
    frames_per_point: list[int] = field(default_factory=list)


@dataclass
class ModelScore:

    name: str
    error_px: float | None  # None = 该模型交叉验证失败
    note: str = ""


@dataclass
class CalibrationReport:

    mode: str
    model_name: str
    cv_error_px: float | None = None
    n_samples: int = 0
    n_points: int = 0
    scores: list[ModelScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"{describe_mode(self.mode)} → {self.model_name}"
        if self.cv_error_px is not None:
            text += f"，验证误差 {self.cv_error_px:.0f}px"
        if self.notes:
            text += f"（{'；'.join(self.notes)}）"
        return text


# ---- 采集 ------------------------------------------------------------------
def _grid_points(mode: str, sw: int, sh: int) -> list[tuple[int, int]]:
    if mode == "5_point":
        return compute_grid_points(_ORDER_5, sw, sh)
    if mode == "9_point":
        return compute_grid_points(_ORDER_9, sw, sh)
    return compute_grid_points_from_shape(
        _DENSE_ROWS, _DENSE_COLS, sw, sh, order="serpentine"
    )


def _head_ok(features: np.ndarray, max_head_angle_deg: float) -> bool:
    """只判 yaw/pitch（features[-3] / features[-2]）。

    roll（features[-1]）正脸≈±π 且在 ±π 间环绕跳变，绝对阈值会把正脸也拒掉；
    单歪头又被 eyetrax 的人脸坐标系归一化抵消大部分，故不参与判定。
    """
    yaw_deg = abs(math.degrees(float(features[-3])))
    pitch_deg = abs(math.degrees(float(features[-2])))
    return max(yaw_deg, pitch_deg) <= max_head_angle_deg


def capture_grid_samples(
    estimator,
    cap,
    points: list[tuple[int, int]],
    sw: int,
    sh: int,
    sample_seconds: float,
    max_head_angle_deg: float,
) -> tuple[list, list, list[int]] | None:
    """网格点采集。结构照 eyetrax 的 _pulse_and_capture，多一条头部姿态入库条件。

    返回 (feats, targs, frames_per_point)；None = ESC 取消。
    """
    feats: list = []
    targs: list = []
    frames_per_point: list[int] = []

    for x, y in points:
        # ---- 脉冲提示（不采集，与 eyetrax 一致）----
        pulse_start = time.time()
        final_radius = 20
        while True:
            elapsed = time.time() - pulse_start
            if elapsed > _PULSE_SECONDS:
                break
            ok, frame = cap.read()
            if not ok:
                continue
            canvas = np.zeros((sh, sw, 3), dtype=np.uint8)
            radius = 15 + int(15 * abs(np.sin(2 * np.pi * elapsed)))
            final_radius = radius
            cv2.circle(canvas, (x, y), radius, (0, 255, 0), -1)
            cv2.imshow(_WINDOW_NAME, canvas)
            if cv2.waitKey(1) == 27:
                return None

        # ---- 采集 ----
        captured = 0
        cap_start = time.time()
        while True:
            elapsed = time.time() - cap_start
            if elapsed > sample_seconds:
                break
            ok, frame = cap.read()
            if not ok:
                continue
            canvas = np.zeros((sh, sw, 3), dtype=np.uint8)
            cv2.circle(canvas, (x, y), final_radius, (0, 255, 0), -1)
            ease = (elapsed / sample_seconds) ** 2 * (3 - 2 * (elapsed / sample_seconds))
            cv2.ellipse(
                canvas, (x, y), (40, 40), 0, -90, -90 + 360 * (1 - ease),
                (255, 255, 255), 4,
            )
            # 每帧只提一次特征：extract_features 内部有 EAR 眨眼历史，重复调用会污染状态
            ft, blink = estimator.extract_features(frame)
            head_ok = ft is not None and not blink and _head_ok(ft, max_head_angle_deg)
            if ft is not None and not blink and not head_ok:
                # cv2 画不了中文，用英文短提示让用户知道"转头这段没被采集"
                cv2.putText(
                    canvas, "Head off-center - not recorded", (max(x - 260, 10), max(y - 60, 30)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2,
                )
            cv2.imshow(_WINDOW_NAME, canvas)
            if cv2.waitKey(1) == 27:
                return None
            if head_ok:
                feats.append(ft)
                targs.append([x, y])
                captured += 1
        frames_per_point.append(captured)

    return feats, targs, frames_per_point


def collect_grid_samples(
    estimator,
    points: list[tuple[int, int]],
    sample_seconds: float,
    camera_index: int,
    max_head_angle_deg: float,
) -> SampleSet | None:
    """采集一次网格校准样本。返回 None = ESC 取消 / 无有效帧。"""
    sw, sh = get_screen_size()
    cap = open_camera(camera_index)
    try:
        if not wait_for_face_and_countdown(cap, estimator, sw, sh, _COUNTDOWN_SECONDS):
            return None
        res = capture_grid_samples(
            estimator, cap, points, sw, sh, sample_seconds, max_head_angle_deg
        )
    finally:
        cap.release()
        cv2.destroyAllWindows()

    if res is None:
        return None
    feats, targs, frames_per_point = res
    if not feats:
        return None

    # 目标坐标唯一 → 用它把样本分回各自注视点（交叉验证必须按点分组：
    # 同一点相邻帧几乎重复，随机切分会把训练集信息泄漏进验证集）
    point_index: dict[tuple[int, int], int] = {}
    groups = np.empty(len(targs), dtype=int)
    for i, (gx, gy) in enumerate(targs):
        groups[i] = point_index.setdefault((int(gx), int(gy)), len(point_index))

    return SampleSet(
        X=np.asarray(feats),
        y=np.asarray(targs, dtype=float),
        groups=groups,
        n_points=len(point_index),
        frames_per_point=frames_per_point,
    )


# ---- 模型评估 / 训练 -------------------------------------------------------
def _fit(model: BaseModel, X: np.ndarray, y: np.ndarray) -> None:
    # elastic_net 在 486 维小样本下几乎必然不收敛，告警会刷满校准输出；
    # 模型好坏由实测验证误差决定，这里静音收敛告警不影响结论
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        model.train(X, y)


def _fold_of(n_groups: int, k: int) -> np.ndarray:
    """把注视点编号打散后交错分到 k 折，保证各折点数尽量均衡。"""
    rng = np.random.default_rng(_CV_SEED)
    order = rng.permutation(n_groups)
    fold = np.empty(n_groups, dtype=int)
    fold[order] = np.arange(n_groups) % k
    return fold


def cross_validate(
    X: np.ndarray,
    y: np.ndarray,
    groups: np.ndarray,
    names: list[str],
    folds: int = _CV_FOLDS,
) -> list[ModelScore]:
    """按注视点分组的 K 折交叉验证，返回每个模型的平均误差（像素，各点等权）。"""
    n_points = int(groups.max()) + 1 if len(groups) else 0
    k = max(1, min(folds, n_points))
    group_fold = _fold_of(n_points, k)

    scores: list[ModelScore] = []
    for name in names:
        per_point_errors: list[float] = []
        note = ""
        for f in range(k):
            val_groups = np.flatnonzero(group_fold == f)
            val_mask = np.isin(groups, val_groups)
            try:
                model = create_model(name)
                _fit(model, X[~val_mask], y[~val_mask])
                pred = model.predict(X[val_mask])
            except Exception as exc:  # 单个候选失败不该拖垮整次校准
                note = f"{type(exc).__name__}: {exc}"
                per_point_errors = []
                break
            err = np.hypot(pred[:, 0] - y[val_mask, 0], pred[:, 1] - y[val_mask, 1])
            val_group_ids = groups[val_mask]
            for g in val_groups:
                per_point_errors.append(float(err[val_group_ids == g].mean()))

        scores.append(
            ModelScore(
                name=name,
                error_px=float(np.mean(per_point_errors)) if per_point_errors else None,
                note=note,
            )
        )
    return scores


def _train_best(
    samples: SampleSet, requested: str, mode: str, min_frames_per_point: int
) -> tuple[BaseModel, CalibrationReport]:
    names = list(AUTO_MODEL_CANDIDATES)
    if requested != "auto" and requested not in names:
        names.append(requested)
    scores = cross_validate(samples.X, samples.y, samples.groups, names)
    by_name = {s.name: s for s in scores}

    report = CalibrationReport(
        mode=mode,
        model_name=requested,
        n_samples=int(len(samples.y)),
        n_points=samples.n_points,
        scores=scores,
    )
    ok = [s for s in scores if s.error_px is not None]
    if requested == "auto":
        chosen = min(ok, key=lambda s: s.error_px).name if ok else AUTO_MODEL_CANDIDATES[0]
        if not ok:
            report.notes.append(f"候选模型全部训练失败，回退 {chosen}")
    else:
        chosen = requested
        if by_name.get(requested) is None or by_name[requested].error_px is None:
            report.notes.append(f"{requested} 交叉验证失败，回退 {AUTO_MODEL_CANDIDATES[0]}")
            chosen = AUTO_MODEL_CANDIDATES[0]

    model = create_model(chosen)
    _fit(model, samples.X, samples.y)  # 选定后全量训练（含验证折数据）
    report.model_name = chosen
    report.cv_error_px = by_name[chosen].error_px if chosen in by_name else None

    shortage = [
        f"第{i + 1}点 {n} 帧"
        for i, n in enumerate(samples.frames_per_point)
        if n < min_frames_per_point
    ]
    if shortage:
        note = f"样本数不足 {min_frames_per_point} 帧（{'、'.join(shortage)}）"
        report.notes.append(note)
        print(f"[calibration] {note}，精度可能下降", file=sys.stderr)

    print(
        "[calibration] 模型交叉验证误差(px)："
        + "，".join(
            f"{s.name}={s.error_px:.1f}" if s.error_px is not None else f"{s.name}=失败"
            for s in scores
        ),
        file=sys.stderr,
    )
    return model, report


# ---- 对外入口 --------------------------------------------------------------
def run_calibration(
    estimator,
    calibration: CalibrationConfig,
    gaze: GazeConfig,
    camera_index: int,
) -> CalibrationReport | None:
    """就地完成校准训练（None = 用户取消 / 无有效样本）。"""
    mode = normalize_mode(calibration.mode)
    requested = gaze.model_name
    if requested != "auto" and requested != gaze.runtime_model_name():
        # 模型名非法：runtime_model_name() 已告警并降级为占位模型，这里改为自动优选
        requested = "auto"

    if mode in GRID_MODES:
        sw, sh = get_screen_size()
        samples = collect_grid_samples(
            estimator,
            _grid_points(mode, sw, sh),
            calibration.sample_seconds_per_point,
            camera_index,
            gaze.max_head_angle_deg,
        )
        if samples is None:
            return None
        model, report = _train_best(
            samples, requested, mode, calibration.min_frames_per_point
        )
        estimator.model = model
        return report

    # lissajous / adaptive：eyetrax 自己生成轨迹并在内部训练，拿不到逐点样本，
    # 因此不做模型优选，直接用配置的模型名（auto 退化为占位模型）
    if mode == "lissajous":
        run_lissajous_calibration(estimator, camera_index=camera_index)
    else:
        run_adaptive_calibration(estimator, camera_index=camera_index)
    note = "该模式由 eyetrax 直接训练，未做模型优选与误差评估"
    if requested == "auto":
        note += f"（auto 在此模式不可用，已用 {gaze.runtime_model_name()}）"
    return CalibrationReport(
        mode=mode, model_name=gaze.runtime_model_name(), notes=[note]
    )


__all__ = [
    "AUTO_MODEL_CANDIDATES",
    "CalibrationReport",
    "GRID_MODES",
    "ModelScore",
    "SampleSet",
    "capture_grid_samples",
    "collect_grid_samples",
    "cross_validate",
    "describe_mode",
    "normalize_mode",
    "run_calibration",
]
