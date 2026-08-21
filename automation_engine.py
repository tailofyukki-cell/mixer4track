"""Mixer4TrackのPhase 27オートメーション管理。

UIスレッドは録音ポイントの追加とレーン状態の保存だけを担い、音声スレッドは
レンダー対象チャンクの時刻で補間値を取得してBrokerへ適用する。
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class AutomationPoint:
    time_sec: float
    value: float

    def to_dict(self) -> dict:
        return {"time_sec": round(float(self.time_sec), 5), "value": float(self.value)}

    @classmethod
    def from_dict(cls, data: dict) -> "AutomationPoint":
        return cls(max(0.0, float(data.get("time_sec", 0.0))), float(data.get("value", 0.0)))


class AutomationLane:
    """単一パラメータの時系列レーン。近接点は上書きしてイベント量を抑える。"""

    MERGE_WINDOW_SEC = 0.025

    def __init__(self, points: Optional[Iterable[AutomationPoint]] = None):
        self._points: List[AutomationPoint] = sorted(list(points or []), key=lambda p: p.time_sec)

    def add_point(self, time_sec: float, value: float):
        point = AutomationPoint(max(0.0, float(time_sec)), float(value))
        for index, existing in enumerate(self._points):
            if abs(existing.time_sec - point.time_sec) <= self.MERGE_WINDOW_SEC:
                self._points[index] = point
                return
            if existing.time_sec > point.time_sec:
                self._points.insert(index, point)
                return
        self._points.append(point)

    def value_at(self, time_sec: float) -> Optional[float]:
        if not self._points:
            return None
        time_sec = max(0.0, float(time_sec))
        if time_sec <= self._points[0].time_sec:
            return self._points[0].value
        if time_sec >= self._points[-1].time_sec:
            return self._points[-1].value
        for left, right in zip(self._points, self._points[1:]):
            if left.time_sec <= time_sec <= right.time_sec:
                span = right.time_sec - left.time_sec
                if span <= 1e-9:
                    return right.value
                ratio = (time_sec - left.time_sec) / span
                return left.value + (right.value - left.value) * ratio
        return self._points[-1].value

    def to_list(self) -> List[dict]:
        return [point.to_dict() for point in self._points]

    @classmethod
    def from_list(cls, data: object) -> "AutomationLane":
        if not isinstance(data, list):
            return cls()
        return cls(AutomationPoint.from_dict(item) for item in data if isinstance(item, dict))


class AutomationManager:
    """トラック（volume/pan）とMASTER X-FADERのオートメーションを管理する。"""

    TRACK_TARGETS = ("volume", "pan")
    MASTER_TARGETS = ("xfade_position",)

    def __init__(self):
        self._track_lanes: Dict[int, Dict[str, AutomationLane]] = {}
        self._master_lanes: Dict[str, AutomationLane] = {}
        self._lock = threading.RLock()
        self.enabled = False
        self.recording = False

    def set_track_data(self, track_id: int, data: object):
        lanes = {}
        if isinstance(data, dict):
            for target in self.TRACK_TARGETS:
                lanes[target] = AutomationLane.from_list(data.get(target, []))
        with self._lock:
            self._track_lanes[int(track_id)] = lanes

    def get_track_data(self, track_id: int) -> Dict[str, List[dict]]:
        with self._lock:
            lanes = self._track_lanes.get(int(track_id), {})
            return {target: lane.to_list() for target, lane in lanes.items() if lane.to_list()}

    def set_master_data(self, data: object):
        lanes = {}
        if isinstance(data, dict):
            for target in self.MASTER_TARGETS:
                lanes[target] = AutomationLane.from_list(data.get(target, []))
        with self._lock:
            self._master_lanes = lanes

    def get_master_data(self) -> Dict[str, List[dict]]:
        with self._lock:
            return {target: lane.to_list() for target, lane in self._master_lanes.items() if lane.to_list()}

    def record_track(self, track_id: int, target: str, time_sec: float, value: float):
        if target not in self.TRACK_TARGETS:
            raise ValueError(f"Unsupported track automation target: {target}")
        with self._lock:
            lane = self._track_lanes.setdefault(int(track_id), {}).setdefault(target, AutomationLane())
            lane.add_point(time_sec, value)

    def record_master(self, target: str, time_sec: float, value: float):
        if target not in self.MASTER_TARGETS:
            raise ValueError(f"Unsupported master automation target: {target}")
        with self._lock:
            lane = self._master_lanes.setdefault(target, AutomationLane())
            lane.add_point(time_sec, value)

    def values_at(self, time_sec: float) -> Tuple[Dict[int, Dict[str, float]], Dict[str, float]]:
        with self._lock:
            if not self.enabled:
                return {}, {}
            tracks: Dict[int, Dict[str, float]] = {}
            for track_id, lanes in self._track_lanes.items():
                values = {target: value for target, lane in lanes.items()
                          if (value := lane.value_at(time_sec)) is not None}
                if values:
                    tracks[track_id] = values
            master = {target: value for target, lane in self._master_lanes.items()
                      if (value := lane.value_at(time_sec)) is not None}
            return tracks, master
