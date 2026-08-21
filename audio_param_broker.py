"""Mixer4TrackのUI操作を音声スレッドへ安全に渡すパラメータBroker。

Phase 24Aでは、フェーダー、PAN、MUTE/SOLO、MASTER音量を対象にする。
UIは最新の意図だけをBrokerへ登録し、音声スレッドはチャンク境界で
不変スナップショットを一度だけ取得する。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, Literal, Optional, Tuple

from track_model import TrackModel


@dataclass(frozen=True)
class EQSnapshot:
    """EQParamsをBrokerに安全に格納する不変値オブジェクト。"""

    low_gain_db: float = 0.0
    mid_gain_db: float = 0.0
    mid_freq_hz: float = 1000.0
    mid_q: float = 1.0
    high_gain_db: float = 0.0

    @classmethod
    def from_params(cls, params) -> "EQSnapshot":
        return cls(
            low_gain_db=float(params.low_gain_db),
            mid_gain_db=float(params.mid_gain_db),
            mid_freq_hz=float(params.mid_freq_hz),
            mid_q=float(params.mid_q),
            high_gain_db=float(params.high_gain_db),
        )

    def to_params(self):
        from eq_engine import EQParams
        params = EQParams(
            low_gain_db=self.low_gain_db,
            mid_gain_db=self.mid_gain_db,
            mid_freq_hz=self.mid_freq_hz,
            mid_q=self.mid_q,
            high_gain_db=self.high_gain_db,
        )
        params.clamp()
        return params


@dataclass(frozen=True)
class GEQSnapshot:
    """GEQParamsの周波数ゲインを順序固定の不変Tupleへ変換した値。"""

    gains: Tuple[Tuple[float, float], ...] = ()

    @classmethod
    def from_params(cls, params) -> "GEQSnapshot":
        return cls(tuple(sorted(
            (float(freq), float(gain)) for freq, gain in params.get_all_gains().items()
        )))

    def to_params(self):
        from geq_engine import GEQParams
        params = GEQParams()
        for freq, gain in self.gains:
            params.set_gain(freq, gain)
        return params


@dataclass(frozen=True)
class TrackDSPParams:
    """トラックDSPの切替に必要な、チャンク中は不変の設定集合。"""

    gain_db: float = 0.0
    eq: EQSnapshot = field(default_factory=EQSnapshot)
    effect_preset: str = "None"
    effect_enabled: bool = False
    aux_enabled: bool = False


@dataclass(frozen=True)
class MasterDSPParams:
    """MASTER DSPの切替に必要な、チャンク中は不変の設定集合。"""

    geq: GEQSnapshot = field(default_factory=GEQSnapshot)


ParamSource = Literal["default", "automation", "manual", "system", "transport"]
ParamKey = Tuple[str, Optional[int], str]

SOURCE_PRIORITY: Dict[str, int] = {
    "default": 0,
    "automation": 100,
    "manual": 200,
    "system": 300,
    "transport": 400,
}


@dataclass(frozen=True)
class TrackMixParams:
    """トラックのミックス段だけに必要な不変パラメータ。"""

    track_id: int
    volume: float = 0.80
    pan: float = 0.0
    muted: bool = False
    solo: bool = False
    dsp: TrackDSPParams = field(default_factory=TrackDSPParams)

    def is_audible(self, any_solo: bool) -> bool:
        return not self.muted and (not any_solo or self.solo)


@dataclass(frozen=True)
class MasterMixParams:
    """Phase 24AでBroker管理するMASTERミックスパラメータ。"""

    volume: float = 1.0
    dsp: MasterDSPParams = field(default_factory=MasterDSPParams)


@dataclass(frozen=True)
class AudioParamSnapshot:
    """1チャンク中は変更されない、音声処理用のパラメータ集合。"""

    generation: int
    transport_epoch: int
    tracks: Tuple[TrackMixParams, ...]
    master: MasterMixParams

    def track_for(self, track_id: int) -> TrackMixParams:
        for params in self.tracks:
            if params.track_id == track_id:
                return params
        return TrackMixParams(track_id=track_id)

    @property
    def any_solo(self) -> bool:
        return any(params.solo for params in self.tracks)


@dataclass(frozen=True)
class ParamPatch:
    """Brokerへ渡す1項目の更新要求。"""

    key: ParamKey
    value: object
    source: ParamSource = "manual"
    transport_epoch: Optional[int] = None


@dataclass(frozen=True)
class ParamBatch:
    """同一generationで適用する複数Patch。"""

    patches: Tuple[ParamPatch, ...]


class AudioParamBroker:
    """最新値圧縮、世代管理、Transport Epochを担うスレッドセーフなBroker。"""

    def __init__(self, num_tracks: int):
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._generation = 0
        self._transport_epoch = 0
        self._sequence = 0
        self._tracks: Dict[int, TrackMixParams] = {
            track_id: TrackMixParams(track_id=track_id)
            for track_id in range(max(0, int(num_tracks)))
        }
        self._master = MasterMixParams()
        # key -> (priority, sequence)。同世代の低優先更新を退ける。
        self._last_writer: Dict[ParamKey, Tuple[int, int]] = {}
        self._last_snapshot = self._build_snapshot_locked()

    # ------------------------------------------------------------------
    # 初期化・観測
    # ------------------------------------------------------------------

    def snapshot(self) -> AudioParamSnapshot:
        with self._lock:
            return self._last_snapshot

    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    def current_transport_epoch(self) -> int:
        with self._lock:
            return self._transport_epoch

    def reset_from_tracks(self, tracks: Iterable[TrackModel], master_volume: float,
                          gain_by_track: Optional[Dict[int, float]] = None,
                          eq_by_track: Optional[Dict[int, object]] = None,
                          effect_preset_by_track: Optional[Dict[int, str]] = None,
                          effect_enabled_by_track: Optional[Dict[int, bool]] = None,
                          aux_enabled_by_track: Optional[Dict[int, bool]] = None,
                          master_geq: Optional[object] = None) -> int:
        """再生開始時にUIモデルの現在値を一つのSnapshotとして登録する。"""
        with self._condition:
            gain_by_track = gain_by_track or {}
            eq_by_track = eq_by_track or {}
            effect_preset_by_track = effect_preset_by_track or {}
            effect_enabled_by_track = effect_enabled_by_track or {}
            aux_enabled_by_track = aux_enabled_by_track or {}
            self._tracks = {}
            for track in tracks:
                eq_params = eq_by_track.get(track.track_id)
                dsp = TrackDSPParams(
                    gain_db=self._clamp_gain(float(gain_by_track.get(track.track_id, 0.0))),
                    eq=EQSnapshot.from_params(eq_params) if eq_params is not None else EQSnapshot(),
                    effect_preset=str(effect_preset_by_track.get(track.track_id, "None")),
                    effect_enabled=bool(effect_enabled_by_track.get(track.track_id, False)),
                    aux_enabled=bool(aux_enabled_by_track.get(track.track_id, False)),
                )
                self._tracks[track.track_id] = TrackMixParams(
                    track_id=track.track_id,
                    volume=self._clamp_volume(track.volume),
                    pan=self._clamp_pan(track.pan),
                    muted=bool(track.muted), solo=bool(track.solo), dsp=dsp,
                )
            master_dsp = MasterDSPParams(
                geq=GEQSnapshot.from_params(master_geq) if master_geq is not None else GEQSnapshot()
            )
            self._master = MasterMixParams(
                volume=self._clamp_master_volume(master_volume), dsp=master_dsp
            )
            self._last_writer.clear()
            return self._commit_locked()

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------

    def submit_track_mix(self, track_id: int, *, volume: Optional[float] = None,
                         pan: Optional[float] = None, muted: Optional[bool] = None,
                         solo: Optional[bool] = None,
                         source: ParamSource = "manual",
                         transport_epoch: Optional[int] = None) -> int:
        """トラックのミックス更新を1 generationで登録する。"""
        patches = []
        if volume is not None:
            patches.append(ParamPatch(("track", track_id, "volume"), volume, source, transport_epoch))
        if pan is not None:
            patches.append(ParamPatch(("track", track_id, "pan"), pan, source, transport_epoch))
        if muted is not None:
            patches.append(ParamPatch(("track", track_id, "muted"), muted, source, transport_epoch))
        if solo is not None:
            patches.append(ParamPatch(("track", track_id, "solo"), solo, source, transport_epoch))
        return self.submit_batch(ParamBatch(tuple(patches)))

    def submit_master_volume(self, volume: float, *, source: ParamSource = "manual",
                             transport_epoch: Optional[int] = None) -> int:
        return self.submit_batch(ParamBatch((
            ParamPatch(("master", None, "volume"), volume, source, transport_epoch),
        )))

    def submit_track_dsp(self, track_id: int, *, gain_db: Optional[float] = None,
                         eq_params: Optional[object] = None,
                         effect_preset: Optional[str] = None,
                         effect_enabled: Optional[bool] = None,
                         aux_enabled: Optional[bool] = None,
                         source: ParamSource = "manual",
                         transport_epoch: Optional[int] = None) -> int:
        """トラックDSPの複数値を一つのgenerationとして登録する。"""
        patches = []
        if gain_db is not None:
            patches.append(ParamPatch(("track", track_id, "gain_db"), gain_db, source, transport_epoch))
        if eq_params is not None:
            patches.append(ParamPatch(("track", track_id, "eq"), EQSnapshot.from_params(eq_params), source, transport_epoch))
        if effect_preset is not None:
            patches.append(ParamPatch(("track", track_id, "effect_preset"), effect_preset, source, transport_epoch))
        if effect_enabled is not None:
            patches.append(ParamPatch(("track", track_id, "effect_enabled"), effect_enabled, source, transport_epoch))
        if aux_enabled is not None:
            patches.append(ParamPatch(("track", track_id, "aux_enabled"), aux_enabled, source, transport_epoch))
        return self.submit_batch(ParamBatch(tuple(patches)))

    def submit_master_geq(self, params: object, *, source: ParamSource = "manual",
                          transport_epoch: Optional[int] = None) -> int:
        return self.submit_batch(ParamBatch((
            ParamPatch(("master", None, "geq"), GEQSnapshot.from_params(params), source, transport_epoch),
        )))

    def submit_batch(self, batch: ParamBatch) -> int:
        """同じ時点に成立すべきPatch群を原子的に登録する。"""
        if not batch.patches:
            return self.current_generation()

        with self._condition:
            accepted = False
            for patch in batch.patches:
                if patch.transport_epoch is not None and patch.transport_epoch != self._transport_epoch:
                    continue
                if patch.source not in SOURCE_PRIORITY:
                    raise ValueError(f"Unsupported parameter source: {patch.source}")
                self._sequence += 1
                priority = SOURCE_PRIORITY[patch.source]
                previous = self._last_writer.get(patch.key)
                if previous is not None and previous[0] > priority:
                    continue
                if self._apply_patch_locked(patch):
                    self._last_writer[patch.key] = (priority, self._sequence)
                    accepted = True
            if accepted:
                return self._commit_locked()
            return self._generation

    def begin_transport_epoch(self) -> int:
        """STOP/LOAD等で旧操作を無効化するための世代境界を開始する。"""
        with self._condition:
            self._transport_epoch += 1
            self._last_writer.clear()
            self._commit_locked()
            return self._transport_epoch

    # ------------------------------------------------------------------
    # 音声スレッド向け同期
    # ------------------------------------------------------------------

    def take_snapshot(self, last_generation: int) -> Optional[AudioParamSnapshot]:
        """generationが変わった場合だけ最新Snapshotを返す。"""
        with self._lock:
            if self._generation == last_generation:
                return None
            return self._last_snapshot

    def wait_for_generation(self, last_generation: int, timeout_sec: float) -> bool:
        """新generationを待つ。通知を逃しても世代比較で必ず検出できる。"""
        with self._condition:
            if self._generation != last_generation:
                return True
            self._condition.wait(timeout=max(0.0, float(timeout_sec)))
            return self._generation != last_generation

    # ------------------------------------------------------------------
    # 内部処理
    # ------------------------------------------------------------------

    def _apply_patch_locked(self, patch: ParamPatch) -> bool:
        scope, track_id, field = patch.key
        if scope == "master":
            if field == "volume":
                value = self._clamp_master_volume(float(patch.value))
                updated = MasterMixParams(volume=value, dsp=self._master.dsp)
            elif field == "geq":
                updated = MasterMixParams(
                    volume=self._master.volume,
                    dsp=MasterDSPParams(geq=patch.value),
                )
            else:
                raise ValueError(f"Unsupported parameter key: {patch.key}")
            if updated == self._master:
                return False
            self._master = updated
            return True

        if scope != "track" or track_id is None:
            raise ValueError(f"Unsupported parameter key: {patch.key}")

        current = self._tracks.get(track_id, TrackMixParams(track_id=track_id))
        dsp = current.dsp
        if field == "volume":
            updated = TrackMixParams(track_id, self._clamp_volume(float(patch.value)),
                                     current.pan, current.muted, current.solo, dsp)
        elif field == "pan":
            updated = TrackMixParams(track_id, current.volume, self._clamp_pan(float(patch.value)),
                                     current.muted, current.solo, dsp)
        elif field == "muted":
            updated = TrackMixParams(track_id, current.volume, current.pan,
                                     bool(patch.value), current.solo, dsp)
        elif field == "solo":
            updated = TrackMixParams(track_id, current.volume, current.pan,
                                     current.muted, bool(patch.value), dsp)
        elif field == "gain_db":
            updated_dsp = TrackDSPParams(self._clamp_gain(float(patch.value)), dsp.eq,
                                         dsp.effect_preset, dsp.effect_enabled, dsp.aux_enabled)
            updated = TrackMixParams(track_id, current.volume, current.pan, current.muted, current.solo, updated_dsp)
        elif field == "eq":
            updated_dsp = TrackDSPParams(dsp.gain_db, patch.value,
                                         dsp.effect_preset, dsp.effect_enabled, dsp.aux_enabled)
            updated = TrackMixParams(track_id, current.volume, current.pan, current.muted, current.solo, updated_dsp)
        elif field == "effect_preset":
            updated_dsp = TrackDSPParams(dsp.gain_db, dsp.eq, str(patch.value),
                                         dsp.effect_enabled, dsp.aux_enabled)
            updated = TrackMixParams(track_id, current.volume, current.pan, current.muted, current.solo, updated_dsp)
        elif field == "effect_enabled":
            updated_dsp = TrackDSPParams(dsp.gain_db, dsp.eq, dsp.effect_preset,
                                         bool(patch.value), dsp.aux_enabled)
            updated = TrackMixParams(track_id, current.volume, current.pan, current.muted, current.solo, updated_dsp)
        elif field == "aux_enabled":
            updated_dsp = TrackDSPParams(dsp.gain_db, dsp.eq, dsp.effect_preset,
                                         dsp.effect_enabled, bool(patch.value))
            updated = TrackMixParams(track_id, current.volume, current.pan, current.muted, current.solo, updated_dsp)
        else:
            raise ValueError(f"Unsupported parameter key: {patch.key}")
        if updated == current:
            return False
        self._tracks[track_id] = updated
        return True

    def _commit_locked(self) -> int:
        self._generation += 1
        self._last_snapshot = self._build_snapshot_locked()
        self._condition.notify_all()
        return self._generation

    def _build_snapshot_locked(self) -> AudioParamSnapshot:
        return AudioParamSnapshot(
            generation=self._generation,
            transport_epoch=self._transport_epoch,
            tracks=tuple(self._tracks[track_id] for track_id in sorted(self._tracks)),
            master=self._master,
        )

    @staticmethod
    def _clamp_volume(value: float) -> float:
        return max(0.0, min(1.5, value))

    @staticmethod
    def _clamp_master_volume(value: float) -> float:
        return max(0.0, min(1.5, value))

    @staticmethod
    def _clamp_gain(value: float) -> float:
        return max(-24.0, min(24.0, value))

    @staticmethod
    def _clamp_pan(value: float) -> float:
        return max(-1.0, min(1.0, value))
