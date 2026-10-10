"""模块名称：calibration_runner.py

功能描述：眼动校准的执行层。按配置的网格模式采集标定点的特征与目标坐标，
用按注视点分组的交叉验证比较候选回归模型，挑误差最小的那个全量训练后写回 estimator。

关键约束：
- eyetrax 自带的校准函数在内部训练并丢弃 (X, y)，换模型就得让用户重做一遍校准，
  也无法比较模型误差；因此网格模式自己采集、自己评估。
- 采集画面是 cv2 全屏窗口，尺寸必须取自 primary_screen_size()（主屏物理分辨率），
  否则标定点会画到屏幕外，坐标系整体错位。
- lissajous / adaptive 两种模式由 eyetrax 自己生成轨迹并在内部训练，拿不到逐点样本，
  只能委托出去，因此这两种模式没有模型优选与误差评估。
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
from mytraxcure.core.gaze.screen_utils import primary_screen_size
from eyetrax.utils.video import open_camera

from mytraxcure.core.config import CalibrationConfig, GazeConfig

# model_name="auto" 的候选模型。eyetrax 0.4.0 里 "svr" 是 "linear_svr" 的别名
# （同一个 LinearSVR，仍是线性），真正的非线性只有 tiny_mlp，故候选里只留一个非线性模型。
AUTO_MODEL_CANDIDATES: tuple[str, ...] = ("ridge", "elastic_net", "tiny_mlp")

# 自建采集 + 模型优选覆盖的模式（能拿到逐点样本）
GRID_MODES: tuple[str, ...] = ("5_point", "9_point", "14_point", "dense")
# 委托 eyetrax 内部训练的模式（拿不到逐点样本，无法评估模型）
DELEGATED_MODES: tuple[str, ...] = ("lissajous", "adaptive")

_DEFAULT_MODE = "14_point"  # 未知模式名的回退值：14 点在误差与采集耗时之间最平衡
# 采集顺序：先中心再四角，避免一上来就要用户把视线甩到极限视角
_ORDER_5 = [(1, 1), (0, 0), (2, 0), (0, 2), (2, 2)]
_ORDER_9 = [
    (1, 1), (0, 0), (2, 0), (0, 2), (2, 2), (1, 0), (0, 1), (2, 1), (1, 2),
]

# 14 点布局（默认）：边距与其它模式一致（10%），所以最外圈坐标与 9 点/dense 完全相同。
# 上/下两行各 4 点（含四角 + 内两列），中间两行各 3 点并向内收（0.2/0.5/0.8）——
# 收进来是因为正文显示在窗口内（默认 1280x720 在屏幕左上角），把中间行的点放到
# 屏幕最左/最右边缘等于采在不看的地方。8 种子实测（合成特征，中位误差）：
# 覆盖区 9 点 4.90 / 14 点 2.63 / dense 2.11 px；窗口区 6.81 / 3.85 / 3.05 px。
_GRID_MARGIN = 0.10
_GRID_14 = (
    (0.0, (0.0, 1 / 3, 2 / 3, 1.0)),
    (1 / 3, (0.2, 0.5, 0.8)),
    (2 / 3, (0.2, 0.5, 0.8)),
    (1.0, (0.0, 1 / 3, 2 / 3, 1.0)),
)
_DENSE_ROWS, _DENSE_COLS = 5, 5
_WINDOW_NAME = "Calibration"  # 与 wait_for_face_and_countdown 建的全屏窗口同名
_PULSE_SECONDS = 0.8  # 点闪动提示（不开采），比 eyetrax 默认 1.0 略快
_COUNTDOWN_SECONDS = 2
_CV_FOLDS = 5
_CV_SEED = 0  # 固定随机种子：同一份样本的评估结果可复现，调参时才好比较

_MODE_LABELS = {
    "5_point": "5 点",
    "9_point": "9 点",
    "14_point": "14 点",
    "dense": f"{_DENSE_ROWS * _DENSE_COLS} 点密集网格",
    "lissajous": "李萨如曲线",
    "adaptive": "自适应（9 点 + 60 随机点）",
}


def describe_mode(mode: str) -> str:
    """把模式名转成给界面看的中文标签；未知模式原样标注而不抛异常。"""
    return _MODE_LABELS.get(mode, f"{mode}（未知模式）")


def normalize_mode(mode: str) -> str:
    """校验校准模式名，非法值打 stderr 告警后回退默认模式。

    Args:
        mode: 配置里读到的模式名，可能是用户手改坏的。

    Returns:
        合法模式名；非法输入返回 _DEFAULT_MODE。
    """
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
    """一次网格采集的产物：特征矩阵、目标坐标，以及把样本映射回注视点的分组编号。"""

    X: np.ndarray
    y: np.ndarray
    groups: np.ndarray  # 每个样本所属注视点编号；交叉验证靠它整点切分
    n_points: int
    # 每点实际入库帧数（与点序一致），只用于样本量告警
    frames_per_point: list[int] = field(default_factory=list)


@dataclass
class ModelScore:
    """单个候选模型的交叉验证结果。"""

    name: str
    error_px: float | None  # None = 该模型交叉验证失败（note 里记异常）
    note: str = ""


@dataclass
class CalibrationReport:
    """一次校准的结果摘要，供日志与界面提示使用。"""

    mode: str
    model_name: str
    cv_error_px: float | None = None
    n_samples: int = 0
    n_points: int = 0
    scores: list[ModelScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """压成一行中文摘要（模式 → 模型 → 验证误差 → 备注），供状态栏与 stderr 日志使用。"""
        text = f"{describe_mode(self.mode)} → {self.model_name}"
        if self.cv_error_px is not None:
            text += f"，验证误差 {self.cv_error_px:.0f}px"
        if self.notes:
            text += f"（{'；'.join(self.notes)}）"
        return text


# ---- 采集 ------------------------------------------------------------------
def _points_14(sw: int, sh: int) -> list[tuple[int, int]]:
    """14 点布局：把 _GRID_14 的比例映射到 10% 边距内的区域，行序即采集顺序。

    Args:
        sw: 主屏物理宽；sh: 主屏物理高。

    Returns:
        14 个点的像素坐标，顺序按 _GRID_14 从上到下逐行展开。
    """
    mx, my = int(sw * _GRID_MARGIN), int(sh * _GRID_MARGIN)
    gw, gh = sw - 2 * mx, sh - 2 * my
    return [
        (mx + int(fx * gw), my + int(fy * gh))
        for fy, xs in _GRID_14
        for fx in xs
    ]


def _grid_points(mode: str, sw: int, sh: int) -> list[tuple[int, int]]:
    """按模式生成网格点像素坐标；5/9/14 点各有固定布局，其余（含 dense）走 5x5 蛇形网格。

    Args:
        mode: 已归一化的模式名；本函数只在 GRID_MODES 分支下被调用。
        sw: 主屏物理宽；sh: 主屏物理高。

    Returns:
        网格点像素坐标，列表顺序即采集顺序。
    """
    if mode == "5_point":
        return compute_grid_points(_ORDER_5, sw, sh)
    if mode == "14_point":
        return _points_14(sw, sh)
    if mode == "9_point":
        return compute_grid_points(_ORDER_9, sw, sh)
    return compute_grid_points_from_shape(
        _DENSE_ROWS, _DENSE_COLS, sw, sh, order="serpentine"
    )


def _head_ok(features: np.ndarray, max_head_angle_deg: float) -> bool:
    """头部姿态入库门控：只判 yaw/pitch（features[-3] / features[-2]），绝对角度不超阈值才收。

    Args:
        features: eyetrax 提的单帧特征向量，末三位是 yaw/pitch/roll（弧度）。
        max_head_angle_deg: yaw/pitch 的绝对角度上限（度）。

    Returns:
        True 表示该帧可以入库。

    Notes:
        roll（features[-1]）不参与判定：正脸时它≈±π 且会在 ±π 之间环绕跳变，
        用绝对阈值会把正脸也拒掉；单纯歪头又已被 eyetrax 的人脸坐标系归一化抵消掉大部分。
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
    """逐个网格点做"脉冲提示 + 采集"，只把通过质量门控的帧入库。

    结构照 eyetrax 的 _pulse_and_capture（先闪动提示、再画进度环采集），
    多一条头部姿态入库条件：转头或眨眼期间的帧不进样本。

    Args:
        estimator: eyetrax GazeEstimator，逐帧调 extract_features。
        cap: 已打开的 cv2.VideoCapture。
        points: 网格点像素坐标（主屏物理坐标系）。
        sw: 画布宽（主屏物理宽）；sh: 画布高。
        sample_seconds: 每点采集时长（秒），不含前面的脉冲提示时间。
        max_head_angle_deg: yaw/pitch 绝对角度上限（度），超限帧丢弃。

    Returns:
        (feats, targs, frames_per_point)；用户按 ESC 取消时返回 None。

    Notes:
        采集期间会在同一个全屏 cv2 窗口里画进度环并按 ESC 退出，
        因此调用前不要另开/销毁该窗口（wait_for_face_and_countdown 建的窗口同名复用）。
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
                # cv2 的 putText 画不了中文：用英文短提示让用户知道转头这段没被采集
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
    """开摄像头、等人脸到位后采集一次网格样本，并给每个样本标上注视点编号。

    Args:
        estimator: eyetrax GazeEstimator。
        points: 网格点像素坐标。
        sample_seconds: 每点采集时长（秒）。
        camera_index: 摄像头序号。
        max_head_angle_deg: yaw/pitch 绝对角度上限（度）。

    Returns:
        SampleSet；ESC 取消、等不到人脸或全部帧都被门控掉时返回 None。

    Notes:
        摄像头在 finally 里 release，cv2 窗口随即销毁，之后才把样本交出去。
    """
    sw, sh = primary_screen_size()
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
    """训练一个候选模型；收敛类告警已静音，避免刷屏校准输出。"""
    # elastic_net 在 486 维小样本下几乎必然不收敛，告警会刷满校准输出；
    # 模型好坏由实测验证误差决定，这里静音收敛告警不影响结论
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        model.train(X, y)


def _fold_of(n_groups: int, k: int) -> np.ndarray:
    """把注视点编号打散后交错分到 k 折，保证各折点数尽量均衡。

    Args:
        n_groups: 注视点个数；k: 折数。

    Returns:
        长度 n_groups 的数组，值为该注视点所属的折号（0 ~ k-1）。
    """
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
    """按注视点分组的 K 折交叉验证，给出每个候选模型的平均误差。

    Args:
        X: 特征矩阵；y: 目标坐标（主屏物理像素）。
        groups: 每个样本所属注视点编号，用于整点切分。
        names: 候选模型名列表。
        folds: 折数上限，实际折数为 min(folds, 注视点数)。

    Returns:
        每个模型一个 ModelScore，error_px 为各点误差的等权平均（像素）；
        训练或预测抛异常时该模型 error_px 为 None，异常记在 note 里。

    Notes:
        按点分组而非按帧随机切分：同一注视点的相邻帧几乎重复，随机切分会把训练集
        信息泄漏进验证集，误差被系统性低估。
        单个候选失败只作废该模型（清空它的误差列表），不影响其余候选继续评估。
    """
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
    """跑交叉验证在候选里选模型，再用全量样本训练它并返回模型与报告。

    Args:
        samples: 本次采集到的样本。
        requested: 配置指定的模型名；"auto" 表示按验证误差自动挑。
        mode: 校准模式，只写进报告。
        min_frames_per_point: 每点样本数下限，低于只记告警不判失败。

    Returns:
        (已全量训练的模型, 校准报告)。

    Notes:
        指定了具体模型名时它也会被交叉验证评估（额外追加进候选），但若它自身验证失败
        就回退 AUTO_MODEL_CANDIDATES[0]；"auto" 且所有候选都失败时同样回退首个候选，
        保证一定有可用模型返回。
        选定后用全量数据（含验证折）重新训练，不复用折内训练出的模型。
    """
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
    """按配置跑一次完整校准，把训练好的模型挂到 estimator 上。

    Args:
        estimator: eyetrax GazeEstimator，校准成功后就地替换它的 model。
        calibration: 校准模式与采集参数。
        gaze: 回归模型名与头部姿态阈值。
        camera_index: 摄像头序号。

    Returns:
        CalibrationReport；用户取消或没有有效样本时返回 None。

    Notes:
        lissajous / adaptive 由 eyetrax 自己生成轨迹并在内部训练，拿不到逐点样本，
        故这两条分支不做模型优选与误差评估，"auto" 在此退化为配置的占位模型名。
    """
    mode = normalize_mode(calibration.mode)
    requested = gaze.model_name
    if requested != "auto" and requested != gaze.runtime_model_name():
        # 模型名非法：runtime_model_name() 已告警并降级为占位模型，这里改为自动优选
        requested = "auto"

    if mode in GRID_MODES:
        sw, sh = primary_screen_size()
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
