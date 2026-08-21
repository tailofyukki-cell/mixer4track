"""
audio_engine.py
チャンクストリーミング方式の音声再生エンジン。

Phase 8: リアルタイムエフェクト対応。
  - 音声ファイルをnumpy配列として保持
  - 再生中はバックグラウンドスレッドが CHUNK_SEC 秒ごとにチャンクを生成
  - チャンク生成時にその瞬間のゲイン/EQ/エフェクトパラメータを適用
  - パラメータ変更は次チャンク（最大 CHUNK_SEC 秒後）から即反映
  - 再起動なし・ノイズなしでリアルタイム切り替えを実現
"""

import os
import math
import time
import wave
import struct
import threading
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

import numpy as np

from track_model import TrackModel
from audio_param_broker import AudioParamBroker, TrackMixParams, TrackDSPParams, GEQSnapshot
from eq_engine import EQEngine, EQParams
from effect_engine import EffectEngine, EFFECT_PRESETS, MasterLimiter
from geq_engine import GEQEngine, GEQParams
from automation_engine import AutomationManager
import mic_engine
from spectrum_engine import SpectrumManager


# ------------------------------------------------------------------
# 定数
# ------------------------------------------------------------------
SAMPLE_RATE  = 44100
CHANNELS     = 2
SAMPLE_WIDTH = 2          # 16bit = 2 bytes
MAX_AMPLITUDE = 32767
CHUNK_SEC    = 0.08       # チャンク長（秒）。短いほどレイテンシが低い
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_SEC)
QUEUE_AHEAD  = 2          # 先読みキュー数（チャンク数）


# ------------------------------------------------------------------
# 書き出し結果データクラス
# ------------------------------------------------------------------
@dataclass
class ExportResult:
    """ミックス書き出しの結果を格納するデータクラス。"""
    success: bool
    output_path: str = ""
    duration_sec: float = 0.0
    clipping_detected: bool = False
    clipping_count: int = 0
    clipping_ratio: float = 0.0
    peak_level: float = 0.0
    error_message: str = ""


# ------------------------------------------------------------------
# トラックストリーマー（1トラック分のチャンク生成）
# ------------------------------------------------------------------
class _TrackStreamer:
    """
    1トラック分のPCMデータをチャンク単位で供給するクラス。
    EQEngine・EffectEngineを内包し、パラメータ変更を次チャンクから反映する。
    """

    def __init__(self, track_id: int, pcm: np.ndarray, sample_rate: int):
        """
        Parameters
        ----------
        pcm : np.ndarray
            shape (n_samples, 2), dtype float32, 値域 -1.0〜1.0
        """
        self._track_id = track_id
        self._pcm = pcm                    # (n_samples, 2) float32
        self._n_samples = len(pcm)
        self._pos = 0                      # 現在の再生位置（サンプル数）
        self._lock = threading.Lock()

        # Phase 22: ループ再生状態（共通タイムライン上のサンプル位置）
        self._loop_enabled: bool = False
        self._loop_start_sample: int = 0
        self._loop_end_sample: int = 0  # 終端はexclusive（この位置の直前まで再生）

        # パラメータ（スレッドセーフに更新）
        self._gain_db: float = 0.0
        self._applied_gain_linear: float = 1.0
        self._eq_params: EQParams = EQParams()
        self._effect_preset: str = "None"
        self._effect_enabled: bool = False
        self._aux_enabled: bool = False  # AUX ON: TrueのトラックのみにFXを適用

        # DSPエンジン（チャンク生成スレッドのみが使用）
        self._eq_engine = EQEngine(sample_rate)
        self._effect_engine = EffectEngine(sample_rate)

    # --- パラメータ更新（音声スレッドのみが呼ぶ） ---

    def set_gain(self, gain_db: float):
        with self._lock:
            self._gain_db = max(-24.0, min(24.0, gain_db))

    def set_eq(self, params: EQParams):
        with self._lock:
            self._eq_params = params
            # EQエンジンにパラメータ変更を通知（クロスフェード開始）
            self._eq_engine.set_params(params)

    def set_effect(self, preset_name: str, enabled: bool):
        with self._lock:
            self._effect_preset = preset_name
            self._effect_enabled = enabled

    def set_aux(self, enabled: bool):
        """AUX ON/OFFを設定する。TrueのときのみFXを適用する。"""
        with self._lock:
            self._aux_enabled = enabled

    def apply_dsp_params(self, params: TrackDSPParams):
        """Broker SnapshotからDSP設定を適用する（音声スレッド専用）。"""
        with self._lock:
            if abs(self._gain_db - params.gain_db) > 0.000001:
                self._gain_db = params.gain_db
            # Phase 26: Morph中はA/B Snapshotを補間し、既存EQEngineの
            # 二重フィルタ・クロスフェード経路で連続的に切り替える。
            eq_snapshot = params.eq
            if params.eq_morph_enabled:
                eq_snapshot = params.eq_snap_a.interpolate(
                    params.eq_snap_b, params.eq_morph_position
                )
            eq_params = eq_snapshot.to_params()
            if self._eq_params.to_dict() != eq_params.to_dict():
                self._eq_params = eq_params
                self._eq_engine.set_params(eq_params)
            if self._effect_preset != params.effect_preset or self._effect_enabled != params.effect_enabled:
                self._effect_preset = params.effect_preset
                self._effect_enabled = params.effect_enabled
            self._aux_enabled = params.aux_enabled

    # --- 再生位置 ---

    def reset(self):
        """再生位置を先頭に戻す。"""
        with self._lock:
            self._pos = 0

    def get_pos_sec(self) -> float:
        """現在の再生位置（秒）を返す。"""
        return self._pos / SAMPLE_RATE

    def is_finished(self) -> bool:
        """再生終了かを返す。ループ有効時は終端に達しても終了しない。"""
        with self._lock:
            return (not self._loop_enabled) and self._pos >= self._n_samples

    def set_loop(self, enabled: bool, start_sample: int = 0, end_sample: int = 0):
        """ループ範囲を設定する。終端はexclusive。"""
        with self._lock:
            if enabled and end_sample > start_sample:
                self._loop_enabled = True
                self._loop_start_sample = max(0, start_sample)
                self._loop_end_sample = max(self._loop_start_sample + 1, end_sample)
                # 新しい範囲外にいる場合は、範囲先頭から再生する
                if self._pos < self._loop_start_sample or self._pos >= self._loop_end_sample:
                    self._pos = self._loop_start_sample
            else:
                self._loop_enabled = False
                self._loop_start_sample = 0
                self._loop_end_sample = 0

    def seek(self, sample_pos: int):
        """再生位置を設定する。ループ有効時は範囲内に丸める。"""
        with self._lock:
            target = max(0, sample_pos)
            if self._loop_enabled and self._loop_end_sample > self._loop_start_sample:
                target = max(self._loop_start_sample,
                             min(target, self._loop_end_sample - 1))
            else:
                target = min(target, self._n_samples)
            self._pos = target

    def _read_loop_chunk_locked(self) -> np.ndarray:
        """ループ範囲をまたいでも常に1チャンク返す（lock取得済み）。"""
        parts = []
        remaining = CHUNK_SAMPLES
        # ループが極端に短い場合も安全に複数周回できるようにする
        while remaining > 0:
            if self._pos >= self._loop_end_sample:
                self._pos = self._loop_start_sample
                # ループ境界でフィルター・ディレイ等の状態を持ち越さない
                self._eq_engine.reset_state()
                self._effect_engine.reset_state()

            segment_end = min(self._pos + remaining, self._loop_end_sample)
            segment_len = max(0, segment_end - self._pos)
            if segment_len == 0:
                self._pos = self._loop_start_sample
                continue

            if self._pos < self._n_samples:
                audio_end = min(segment_end, self._n_samples)
                audio = self._pcm[self._pos:audio_end].copy()
                if len(audio) < segment_len:
                    silence = np.zeros((segment_len - len(audio), 2), dtype=np.float32)
                    audio = np.concatenate([audio, silence], axis=0)
            else:
                # 短いトラックは共通ループ終端まで無音を出力して同期を維持する
                audio = np.zeros((segment_len, 2), dtype=np.float32)

            parts.append(audio)
            self._pos = segment_end
            remaining -= segment_len

        return np.concatenate(parts, axis=0)

    # --- チャンク生成（チャンク生成スレッドから呼ばれる） ---

    def next_chunk(self) -> Optional[np.ndarray]:
        """
        次のチャンク（CHUNK_SAMPLES サンプル）を生成して返す。
        通常再生では終了時にNone、ループ再生では範囲をまたいで連続チャンクを返す。
        返り値は shape (CHUNK_SAMPLES, 2) dtype float32。
        """
        with self._lock:
            if self._loop_enabled and self._loop_end_sample > self._loop_start_sample:
                chunk = self._read_loop_chunk_locked()
            else:
                if self._pos >= self._n_samples:
                    return None

                # PCMスライス
                end = min(self._pos + CHUNK_SAMPLES, self._n_samples)
                chunk = self._pcm[self._pos:end].copy()
                self._pos = end

                # 末尾パディング（最終チャンクが短い場合）
                if len(chunk) < CHUNK_SAMPLES:
                    pad = np.zeros((CHUNK_SAMPLES - len(chunk), 2), dtype=np.float32)
                    chunk = np.concatenate([chunk, pad], axis=0)

            # GAINは前チャンクの実効値からランプし、設定変更時の段差を抑える。
            target_gain = 10.0 ** (self._gain_db / 20.0)
            if abs(target_gain - self._applied_gain_linear) > 0.000001:
                gain_ramp = np.linspace(self._applied_gain_linear, target_gain,
                                        len(chunk), dtype=np.float32)
                chunk = chunk * gain_ramp[:, np.newaxis]
                self._applied_gain_linear = target_gain
            elif abs(target_gain - 1.0) > 0.000001:
                chunk = chunk * target_gain

            # EQ適用（常に apply_eq を呼び、内部で Flat 時はバイパスしつつ _prev_last_sample を更新する）
            chunk = self._eq_engine.apply_eq(chunk)

            # AUX OFF/FX OFFもNoneプリセットとして通し、既存FXからのクロスフェードを維持する。
            effective_preset = (
                self._effect_preset if self._effect_enabled and self._aux_enabled else "None"
            )
            chunk = self._effect_engine.apply(chunk, effective_preset)

            # クリップ
            chunk = np.clip(chunk, -1.0, 1.0)
            return chunk.astype(np.float32)


# ------------------------------------------------------------------
# AudioEngine
# ------------------------------------------------------------------
class AudioEngine:
    """
    チャンクストリーミング方式の音声再生エンジン。
    pygame.mixer.Channel.queue() を使い、バックグラウンドスレッドが
    チャンクを継続的に供給することでリアルタイムエフェクトを実現する。
    """

    MAX_CHANNELS = 32
    SAMPLE_RATE  = SAMPLE_RATE
    CHANNELS     = CHANNELS
    SAMPLE_WIDTH = SAMPLE_WIDTH
    MAX_AMPLITUDE = MAX_AMPLITUDE

    def __init__(self, num_tracks: int = 4):
        self._num_tracks = num_tracks
        self._initialized = False

        # トラックデータ（numpy PCM）
        self._pcm_data: Dict[int, Optional[np.ndarray]] = {}   # float32 (n,2)
        self._file_paths: Dict[int, str] = {}

        # ストリーマー（再生中のみ存在）
        self._streamers: Dict[int, Optional[_TrackStreamer]] = {}

        # パラメータ（ストリーマー未生成時も保持）
        self._gain_db: Dict[int, float] = {}
        self._eq_params: Dict[int, EQParams] = {}
        self._effect_presets: Dict[int, str] = {}
        self._effect_enabled: Dict[int, bool] = {}
        self._aux_enabled: Dict[int, bool] = {}  # AUX ON/OFFフラグ

        # pygame チャンネル（ファイル再生はマスター合成チャンネル1本を共有する）
        self._channels: Dict[int, Optional[object]] = {}
        self._master_channel: Optional[object] = None

        # 再生状態
        self._playing = False
        self._paused = False   # 一時停止中フラグ
        self._tracks_snapshot: List[TrackModel] = []
        self._master_volume: float = 1.0
        self._master_xfade = {
            "position": 0.5,
            "curve": "equal_power",
            "cut_a": False,
            "cut_b": False,
        }
        # Phase 24A: UIスレッドと音声スレッドの間で最新操作だけを渡す。
        self._param_broker = AudioParamBroker(num_tracks)
        # Phase 27: レーンの評価結果は音声スレッドからBrokerへ反映する。
        self._automation = AutomationManager()

        # Phase 23: マスター・リミッター（最終ステレオ出力へ適用）
        self._master_limiter_enabled: bool = True
        self._master_limiter_ceiling_db: float = -1.0
        self._master_limiter_release_ms: float = 120.0
        self._master_limiter = MasterLimiter(SAMPLE_RATE, self._master_limiter_release_ms)
        self._master_limiter_reduction_db: float = 0.0
        self._master_limiter_lock = threading.Lock()

        # Phase 22: 共通タイムラインのループ範囲（秒）
        # end_sec はexclusive。ループ設定はSTOP後も維持する。
        self._loop_enabled: bool = False
        self._loop_start_sec: float = 0.0
        self._loop_end_sec: float = 0.0

        # マスターGEQ
        self._master_geq_params: GEQParams = GEQParams()
        self._master_geq_engine: GEQEngine = GEQEngine(SAMPLE_RATE)
        self._master_geq_active_snapshot: GEQSnapshot = GEQSnapshot()
        self._master_geq_previous_engine: Optional[GEQEngine] = None
        self._master_geq_crossfade_remaining: int = 0
        self._master_geq_crossfade_samples: int = max(1, int(SAMPLE_RATE * 0.020))
        self._master_geq_lock = threading.Lock()

        # ストリーミングスレッド
        self._stream_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # EQ/エフェクトパラメータ変更通知イベント
        # set_eq呼び出し時にストリーミングスレッドを即座に起動し、キュー空白を防ぐ
        self._param_changed_event = threading.Event()

        # MICリアルタイム入力用チャンネル（トラックごと）
        self._mic_channels: Dict[int, Optional[object]] = {}
        self._mic_thread: Optional[threading.Thread] = None

        # スペクトラムアナライザー用マネージャー
        self._spectrum_manager = SpectrumManager(num_tracks=num_tracks)

        # 録音バッファ（REC START〜REC STOP間のミックス済みPCMを蓄積）
        self._rec_buffer: List[np.ndarray] = []
        self._rec_active: bool = False
        self._rec_lock = threading.Lock()
        self._rec_start_time: float = 0.0

        # リアルタイムVU/ピークメーター用（MASTERステレオ）
        self._vu_rms_l: float = 0.0   # 現在チャンクのRMS（Lch）
        self._vu_rms_r: float = 0.0   # 現在チャンクのRMS（Rch）
        self._vu_peak_l: float = 0.0  # ピークホールド値（Lch）
        self._vu_peak_r: float = 0.0  # ピークホールド値（Rch）
        self._vu_clip_l: bool = False  # クリップフラグ（Lch）
        self._vu_clip_r: bool = False  # クリップフラグ（Rch）
        self._vu_lock = threading.Lock()

        self._init_pygame()

    # ------------------------------------------------------------------
    # 初期化
    # ------------------------------------------------------------------

    def _init_pygame(self):
        try:
            import pygame
            pygame.mixer.pre_init(SAMPLE_RATE, -16, CHANNELS, 512)
            pygame.mixer.init()
            pygame.mixer.set_num_channels(self.MAX_CHANNELS)
            self._pygame = pygame
            self._initialized = True
        except Exception as e:
            print(f"[AudioEngine] pygame.mixer 初期化失敗: {e}")
            self._initialized = False

    # ------------------------------------------------------------------
    # ファイル読み込み
    # ------------------------------------------------------------------

    def load_file(self, track_id: int, file_path: str) -> bool:
        """指定トラックに音声ファイルを読み込む（WAV / MP3 対応）。"""
        if not self._initialized:
            return False
        if not os.path.isfile(file_path):
            print(f"[AudioEngine] ファイルが見つかりません: {file_path}")
            return False

        abs_path = os.path.abspath(file_path)
        try:
            # pygameでデコードしてnumpy配列に変換
            sound = self._pygame.mixer.Sound(abs_path)
            buf = self._pygame.sndarray.array(sound)  # int16

            # float32 ステレオに正規化
            if buf.ndim == 2 and buf.shape[1] >= 2:
                f32 = buf[:, :2].astype(np.float32) / 32768.0
            else:
                mono = buf.flatten().astype(np.float32) / 32768.0
                f32 = np.stack([mono, mono], axis=1)

            with self._lock:
                self._pcm_data[track_id] = f32
                self._file_paths[track_id] = abs_path
                self._channels[track_id] = None

            print(f"[AudioEngine] Track {track_id} 読み込み成功: {abs_path} ({len(f32)/SAMPLE_RATE:.2f}s)")
            return True
        except Exception as e:
            print(f"[AudioEngine] Track {track_id} 読み込み失敗: {e}")
            return False

    def unload_file(self, track_id: int):
        """指定トラックの音声を解放する。"""
        with self._lock:
            self._pcm_data.pop(track_id, None)
            self._file_paths.pop(track_id, None)
            self._channels[track_id] = None
            self._streamers.pop(track_id, None)

    # ------------------------------------------------------------------
    # パラメータ更新（リアルタイム反映）
    # ------------------------------------------------------------------

    def update_gain(self, track_id: int, gain_db: float):
        """GAIN更新をBrokerへ登録する。DSPへの適用は次のチャンク境界で行う。"""
        gain_db = max(-24.0, min(24.0, gain_db))
        self._gain_db[track_id] = gain_db
        self._param_broker.submit_track_dsp(track_id, gain_db=gain_db)
        self._param_changed_event.set()

    def update_eq(self, track_id: int, params: EQParams):
        """EQ更新をBrokerへ登録する。クロスフェード開始は音声スレッドが担う。"""
        self._eq_params[track_id] = params
        self._param_broker.submit_track_dsp(track_id, eq_params=params)
        self._param_changed_event.set()

    def update_eq_morph(self, track_id: int, current_params: EQParams,
                        snap_a: EQParams, snap_b: EQParams,
                        position: float, enabled: bool):
        """Phase 26: EQ A/BとMorph状態を一つのBroker generationとして更新する。"""
        position = max(0.0, min(1.0, float(position)))
        self._eq_params[track_id] = current_params
        self._param_broker.submit_track_dsp(
            track_id,
            eq_params=current_params,
            eq_snap_a=snap_a,
            eq_snap_b=snap_b,
            eq_morph_position=position,
            eq_morph_enabled=bool(enabled),
        )
        self._param_changed_event.set()

    def update_effect(self, track_id: int, preset_name: str, enabled: bool):
        """FX更新をBrokerへ登録する。クロスフェード開始は音声スレッドが担う。"""
        self._effect_presets[track_id] = preset_name
        self._effect_enabled[track_id] = enabled
        self._param_broker.submit_track_dsp(
            track_id, effect_preset=preset_name, effect_enabled=enabled
        )
        self._param_changed_event.set()

    def set_aux_track(self, track_id: int, enabled: bool):
        """AUX ON/OFFをBrokerへ登録する。"""
        self._aux_enabled[track_id] = enabled
        self._param_broker.submit_track_dsp(track_id, aux_enabled=enabled)
        self._param_changed_event.set()

    # ------------------------------------------------------------------
    # Phase 27: オートメーション
    # ------------------------------------------------------------------

    def configure_automation(self, tracks: List[TrackModel], master_automation: Optional[Dict] = None,
                             enabled: bool = False, recording: bool = False):
        """保存済みレーンを設定する。再生中にも安全に差し替え可能。"""
        for track in tracks:
            self._automation.set_track_data(track.track_id, getattr(track, "automation", {}))
        self._automation.set_master_data(master_automation or {})
        self._automation.enabled = bool(enabled)
        self._automation.recording = bool(recording)

    def set_automation_enabled(self, enabled: bool):
        self._automation.enabled = bool(enabled)
        self._param_changed_event.set()

    def set_automation_recording(self, recording: bool):
        self._automation.recording = bool(recording)

    def get_automation_master_data(self) -> Dict:
        return self._automation.get_master_data()

    def get_automation_track_data(self, track_id: int) -> Dict:
        return self._automation.get_track_data(track_id)

    def clear_automation(self):
        """全トラックおよびMASTERのレーンを消去する。"""
        self._automation = AutomationManager()
        self._param_changed_event.set()

    def get_timeline_position_sec(self) -> float:
        """共通タイムラインの現在位置。短いトラックを含む場合も最大位置を採用する。"""
        with self._lock:
            streamers = [s for s in self._streamers.values() if s is not None]
        positions = [s.get_pos_sec() for s in streamers]
        return max(positions) if positions else 0.0

    def record_track_automation(self, track_id: int, target: str, value: float):
        if not self._automation.recording or not self._playing:
            return
        self._automation.record_track(track_id, target, self.get_timeline_position_sec(), value)

    def record_master_automation(self, target: str, value: float):
        if not self._automation.recording or not self._playing:
            return
        self._automation.record_master(target, self.get_timeline_position_sec(), value)

    def _apply_automation_at(self, time_sec: float):
        """補間結果をautomation優先度でBrokerへ登録する（音声スレッド専用）。"""
        track_values, master_values = self._automation.values_at(time_sec)
        for track_id, values in track_values.items():
            self._param_broker.submit_track_mix(track_id, source="automation", **values)
        if "xfade_position" in master_values:
            self._param_broker.submit_master_xfade(
                position=master_values["xfade_position"], source="automation"
            )

    # ------------------------------------------------------------------
    # 再生制御
    # ------------------------------------------------------------------

    def play_all(self, tracks: List[TrackModel]):
        """全トラックをストリーミング再生開始する。"""
        if not self._initialized:
            return
        self.stop_all()

        with self._lock:
            self._tracks_snapshot = list(tracks)
            # ストリーマーを生成
            for track in tracks:
                pcm = self._pcm_data.get(track.track_id)
                if pcm is None:
                    self._streamers[track.track_id] = None
                    continue
                s = _TrackStreamer(track.track_id, pcm, SAMPLE_RATE)
                s.set_gain(self._gain_db.get(track.track_id, 0.0))
                s.set_eq(self._eq_params.get(track.track_id, EQParams()))
                s.set_effect(
                    self._effect_presets.get(track.track_id, "None"),
                    self._effect_enabled.get(track.track_id, False)
                )
                s.set_aux(self._aux_enabled.get(track.track_id, False))
                # 既に設定済みのループ範囲があれば、ループ先頭から再生する
                if self._loop_enabled and self._loop_end_sec > self._loop_start_sec:
                    s.set_loop(True,
                               int(self._loop_start_sec * SAMPLE_RATE),
                               int(self._loop_end_sec * SAMPLE_RATE))
                self._streamers[track.track_id] = s
                self._channels[track.track_id] = None

        # Phase 24B: ミックス段とDSP段を同一の不変Snapshotとして確定する。
        self._reset_broker_snapshot(tracks)
        self._playing = True
        self._stop_event.clear()
        self._stream_thread = threading.Thread(
            target=self._master_mix_loop,
            daemon=True,
            name="MasterMixStreamLoop"
        )
        self._stream_thread.start()

    def stop_all(self):
        """全トラックを停止する。"""
        self._stop_event.set()
        # STOP前の自動操作・保留Patchを無効化する。
        self._param_broker.begin_transport_epoch()
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=2.0)
            self._stream_thread = None

        if self._initialized:
            try:
                self._pygame.mixer.stop()
            except Exception:
                pass

        with self._lock:
            self._channels = {k: None for k in self._channels}
            self._streamers = {}
            self._master_channel = None
        self._playing = False
        self._paused = False
        with self._master_limiter_lock:
            self._master_limiter.reset_state()
            self._master_limiter_reduction_db = 0.0

    def pause(self):
        """再生を一時停止する。再生中のみ有効。"""
        if not self._initialized or not self._playing or self._paused:
            return
        try:
            self._pygame.mixer.pause()  # 全チャンネルを一時停止
            self._paused = True
            # ストリームループにポーズを通知（チャンク供給を一時停止）
            self._param_changed_event.set()
        except Exception as e:
            print(f"[AudioEngine] pause失敗: {e}")

    def resume(self):
        """一時停止から再開する。ポーズ中のみ有効。"""
        if not self._initialized or not self._paused:
            return
        try:
            self._pygame.mixer.unpause()  # 全チャンネルを再開
            self._paused = False
            self._param_changed_event.set()
        except Exception as e:
            print(f"[AudioEngine] resume失敗: {e}")

    def is_paused(self) -> bool:
        """一時停止中かどうかを返す。"""
        return self._paused

    # ------------------------------------------------------------------
    # Phase 22: ループ再生制御
    # ------------------------------------------------------------------

    def get_timeline_duration_sec(self) -> float:
        """読み込み済みトラックのうち、最長の再生時間を返す。"""
        with self._lock:
            durations = [len(pcm) / SAMPLE_RATE for pcm in self._pcm_data.values()
                         if pcm is not None]
        return max(durations) if durations else 0.0

    def set_loop_range(self, start_sec: float, end_sec: float, enabled: bool = True) -> bool:
        """
        共通タイムラインのループ範囲を設定する。

        再生中は古いキューを破棄して範囲先頭から即座に再スタートする。
        範囲が不正な場合はFalse、設定に成功した場合はTrueを返す。
        """
        duration = self.get_timeline_duration_sec()
        start = max(0.0, min(float(start_sec), duration))
        end = max(0.0, min(float(end_sec), duration))
        # 最低1サンプル以上の範囲が必要
        if not enabled or duration <= 0.0 or end <= start + (1.0 / SAMPLE_RATE):
            return False

        start_sample = int(start * SAMPLE_RATE)
        end_sample = max(start_sample + 1, int(end * SAMPLE_RATE))
        with self._lock:
            self._loop_enabled = True
            self._loop_start_sec = start_sample / SAMPLE_RATE
            self._loop_end_sec = min(duration, end_sample / SAMPLE_RATE)
            streamers = dict(self._streamers)
            channels = dict(self._channels)
            is_playing = self._playing

        for s in streamers.values():
            if s is not None:
                s.set_loop(True, start_sample, end_sample)
                # ループを有効にした瞬間はIN地点から明示的に開始する
                s.seek(start_sample)

        # 動的な範囲変更時は、既にqueueされている古い音声を破棄する
        if is_playing:
            for ch in channels.values():
                if ch is not None:
                    try:
                        ch.stop()
                    except Exception:
                        pass
        self._param_changed_event.set()
        return True

    def clear_loop_range(self):
        """ループ設定を解除して通常再生に戻す。"""
        with self._lock:
            self._loop_enabled = False
            self._loop_start_sec = 0.0
            self._loop_end_sec = 0.0
            streamers = dict(self._streamers)

        for s in streamers.values():
            if s is not None:
                s.set_loop(False)
        self._param_changed_event.set()

    def is_loop_enabled(self) -> bool:
        with self._lock:
            return self._loop_enabled

    def get_loop_range(self) -> Tuple[bool, float, float]:
        """(有効か, 開始秒, 終了秒) を返す。"""
        with self._lock:
            return self._loop_enabled, self._loop_start_sec, self._loop_end_sec

    def seek_all_tracks(self, pos_sec: float):
        """
        読み込み済み全トラックを共通位置にシークする。
        再生中はキューをクリアし、指定位置の新しい音声を供給する。
        """
        with self._lock:
            streamers = dict(self._streamers)
            channels = dict(self._channels)
        target = int(max(0.0, pos_sec) * SAMPLE_RATE)
        for track_id, s in streamers.items():
            if s is None:
                continue
            s.seek(target)
            ch = channels.get(track_id)
            if ch is not None:
                try:
                    ch.stop()
                except Exception:
                    pass
        self._param_changed_event.set()

    def is_playing(self) -> bool:
        """再生中（ポーズ中を含む）かどうかを返す。"""
        if not self._initialized or not self._playing:
            return False
        if self._stop_event.is_set():
            return False
        # ストリーマーがまだ終わっていないか確認
        with self._lock:
            for s in self._streamers.values():
                if s is not None and not s.is_finished():
                    return True
        return False

    # ------------------------------------------------------------------
    # ストリーミングループ（バックグラウンドスレッド）
    # ------------------------------------------------------------------

    def _stream_loop(self):
        """
        バックグラウンドスレッドのメインループ。
        各トラックのチャンクを生成してpygame.mixer.Channelにqueueする。
        """
        # 各トラックのチャンネルを確保
        with self._lock:
            tracks = list(self._tracks_snapshot)
            streamers = dict(self._streamers)

        any_solo = any(t.solo for t in tracks)

        # チャンネルを割り当て（インデックス直接指定で重複を防ぐ）
        channel_map: Dict[int, object] = {}
        ch_index = 0
        for track in tracks:
            if streamers.get(track.track_id) is None:
                continue
            if ch_index >= self.MAX_CHANNELS:
                print(f"[AudioEngine] チャンネル上限({self.MAX_CHANNELS})に達しました")
                break
            ch = self._pygame.mixer.Channel(ch_index)
            ch_index += 1
            channel_map[track.track_id] = ch
            with self._lock:
                self._channels[track.track_id] = ch

        # 最初のチャンクを先読みしてキューに積む
        for track in tracks:
            s = streamers.get(track.track_id)
            ch = channel_map.get(track.track_id)
            if s is None or ch is None:
                continue
            for _ in range(QUEUE_AHEAD):
                chunk = s.next_chunk()
                if chunk is None:
                    break
                sound = self._chunk_to_sound(chunk, track, any_solo)
                if not ch.get_busy():
                    ch.play(sound)
                else:
                    ch.queue(sound)

        # メインループ：チャンクを継続的に供給
        while not self._stop_event.is_set():
            # ポーズ中はチャンク供給を停止して待機
            if self._paused:
                self._param_changed_event.wait(timeout=0.05)
                self._param_changed_event.clear()
                continue

            all_done = True
            any_solo = any(t.solo for t in tracks)

            for track in tracks:
                s = streamers.get(track.track_id)
                ch = channel_map.get(track.track_id)
                if s is None or ch is None:
                    continue
                if s.is_finished():
                    continue
                all_done = False

                # キューに空きがあれば次チャンクを追加
                # pygame.mixer.Channel.get_queue() が None = キューが空
                if ch.get_queue() is None:
                    chunk = s.next_chunk()
                    if chunk is not None:
                        sound = self._chunk_to_sound(chunk, track, any_solo)
                        if not ch.get_busy():
                            ch.play(sound)
                        else:
                            ch.queue(sound)

            if all_done:
                # 全トラック終了：最後のチャンクの再生が終わるまで待つ
                time.sleep(CHUNK_SEC * QUEUE_AHEAD + 0.1)
                break

            # param_changed_eventがセットされたら即座にループを続行（EQ切り替え時にキューを即補充）
            # イベントがセットされていない場合は最大 CHUNK_SEC*0.05（4ms）待機
            self._param_changed_event.wait(timeout=CHUNK_SEC * 0.05)
            self._param_changed_event.clear()

        self._playing = False

    # ------------------------------------------------------------------
    # Phase 23: 実マスター合成ストリーム
    # ------------------------------------------------------------------

    def _apply_track_mix_params(self, chunk: np.ndarray, track: TrackMixParams,
                                any_solo: bool, xfade_start, xfade_target) -> np.ndarray:
        """トラック単位の音量・PAN・MUTE/SOLOを適用し、マスター合成用PCMを返す。"""
        out = chunk.copy()
        if track.is_audible(any_solo):
            left_gain, right_gain = self._calc_pan_volumes(track.volume, track.pan)
            start_gain = self._get_xfade_gain(track.xfade_assign, xfade_start)
            target_gain = self._get_xfade_gain(track.xfade_assign, xfade_target)
            # Phase 25: position/CUT/curveの変更はチャンク内でランプし、クリックを抑える。
            if abs(start_gain - target_gain) > 1e-7:
                xfade_gain = np.linspace(start_gain, target_gain, len(out), dtype=np.float32)
                out[:, 0] *= left_gain * xfade_gain
                out[:, 1] *= right_gain * xfade_gain
            else:
                out[:, 0] *= left_gain * target_gain
                out[:, 1] *= right_gain * target_gain
        else:
            out.fill(0.0)
        return out.astype(np.float32)

    @staticmethod
    def _get_xfade_gain(assign: str, xfade) -> float:
        """A/B/THRU割当とX-FADER設定からトラックへ乗算するゲインを返す。"""
        assign = str(assign).upper()
        if assign == "THRU":
            return 1.0
        if assign == "A":
            if xfade.cut_a:
                return 0.0
            return (math.cos(xfade.position * math.pi / 2.0)
                    if xfade.curve == "equal_power" else 1.0 - xfade.position)
        if assign == "B":
            if xfade.cut_b:
                return 0.0
            return (math.sin(xfade.position * math.pi / 2.0)
                    if xfade.curve == "equal_power" else xfade.position)
        return 1.0

    def _finalize_master_chunk(self, mix: np.ndarray,
                               master_volume_start: Optional[float] = None,
                               master_volume_target: Optional[float] = None) -> object:
        """合成済みステレオPCMへMASTER VOL、GEQ、リミッター、計測・録音を適用する。"""
        target = self._master_volume if master_volume_target is None else master_volume_target
        start = target if master_volume_start is None else master_volume_start
        out = mix.astype(np.float32, copy=True)
        # Broker更新は音声スレッド内で前回実効値からチャンク内ランプを適用する。
        if len(out) and abs(start - target) > 0.000001:
            ramp = np.linspace(start, target, len(out), dtype=np.float32)
            out *= ramp[:, np.newaxis]
        else:
            out *= target

        out = self._apply_master_geq_chunk(out)

        pre_limit_peak = float(np.max(np.abs(out))) if out.size else 0.0
        with self._master_limiter_lock:
            limiter_enabled = self._master_limiter_enabled
            ceiling_db = self._master_limiter_ceiling_db
            if limiter_enabled:
                out, reduction_db = self._master_limiter.process(out, ceiling_db)
            else:
                reduction_db = 0.0
                self._master_limiter.reset_state()
            self._master_limiter_reduction_db = reduction_db

        # リミッターOFF時も、16bit PCM変換を壊さないための最終安全クリップを行う。
        out = np.clip(out, -1.0, 1.0).astype(np.float32)

        with self._rec_lock:
            if self._rec_active:
                self._rec_buffer.append(out.copy())

        try:
            rms_l = float(np.sqrt(np.mean(out[:, 0] ** 2)))
            rms_r = float(np.sqrt(np.mean(out[:, 1] ** 2)))
            peak_l = float(np.max(np.abs(out[:, 0])))
            peak_r = float(np.max(np.abs(out[:, 1])))
            with self._vu_lock:
                self._vu_rms_l = rms_l
                self._vu_rms_r = rms_r
                self._vu_peak_l = max(self._vu_peak_l, peak_l)
                self._vu_peak_r = max(self._vu_peak_r, peak_r)
                # リミッターが保護している場合はCLIPではなくGR表示で知らせる。
                if not limiter_enabled:
                    if pre_limit_peak >= 0.999:
                        self._vu_clip_l = True
                        self._vu_clip_r = True
        except Exception:
            pass

        i16 = (out * MAX_AMPLITUDE).astype(np.int16)
        return self._pygame.sndarray.make_sound(i16)

    def _apply_master_dsp_snapshot(self, snapshot):
        """Broker SnapshotのMASTER DSP設定を音声スレッドで適用する。"""
        target = snapshot.master.dsp.geq
        with self._master_geq_lock:
            if target == self._master_geq_active_snapshot:
                return
            previous_engine = self._master_geq_engine
            next_engine = GEQEngine(SAMPLE_RATE)
            next_params = target.to_params()
            next_engine.set_params(next_params)
            self._master_geq_previous_engine = previous_engine
            self._master_geq_engine = next_engine
            self._master_geq_active_snapshot = target
            self._master_geq_crossfade_remaining = self._master_geq_crossfade_samples

    def _apply_master_geq_chunk(self, pcm: np.ndarray) -> np.ndarray:
        """現在のMASTER GEQを適用し、切替中は新旧出力をクロスフェードする。"""
        with self._master_geq_lock:
            current_engine = self._master_geq_engine
            previous_engine = self._master_geq_previous_engine
            remaining = self._master_geq_crossfade_remaining
            total = self._master_geq_crossfade_samples

            current_out = current_engine.apply_vectorized(pcm)
            if previous_engine is None or remaining <= 0:
                return current_out

            previous_out = previous_engine.apply_vectorized(pcm)
            fade_len = min(len(pcm), remaining)
            progressed = total - remaining
            fade_in = np.linspace(
                progressed / total, (progressed + fade_len) / total,
                fade_len, dtype=np.float32,
            )
            out = current_out.copy()
            out[:fade_len] = (
                previous_out[:fade_len] * (1.0 - fade_in[:, np.newaxis])
                + current_out[:fade_len] * fade_in[:, np.newaxis]
            )
            self._master_geq_crossfade_remaining = max(0, remaining - len(pcm))
            if self._master_geq_crossfade_remaining == 0:
                self._master_geq_previous_engine = None
            return out

    def _render_master_mix_chunk(self, tracks: List[TrackModel],
                                 streamers: Dict[int, Optional[_TrackStreamer]],
                                 snapshot, previous_xfade=None) -> Tuple[Optional[np.ndarray], bool]:
        """各トラックの次チャンクを1つずつ合成し、(PCM, データ有無)を返す。"""
        mix = np.zeros((CHUNK_SAMPLES, 2), dtype=np.float32)
        has_data = False
        previous_xfade = previous_xfade or snapshot.master.xfade
        for track in tracks:
            streamer = streamers.get(track.track_id)
            if streamer is None:
                continue
            chunk = streamer.next_chunk()
            if chunk is None:
                continue
            has_data = True
            mix_params = snapshot.track_for(track.track_id)
            processed = self._apply_track_mix_params(
                chunk, mix_params, snapshot.any_solo, previous_xfade, snapshot.master.xfade
            )
            mix += processed
            try:
                self._spectrum_manager.push_chunk(track.track_id, processed)
            except Exception:
                pass
        return (mix if has_data else None), has_data

    def _master_mix_loop(self):
        """全ファイルトラックを先に合成してから、単一のマスターチャンネルへ供給する。"""
        with self._lock:
            tracks = list(self._tracks_snapshot)
            streamers = dict(self._streamers)

        # この変数は音声スレッドだけが更新する。UIはBroker経由で次の値を登録する。
        render_snapshot = self._param_broker.snapshot()

        master_channel = self._pygame.mixer.Channel(self.MAX_CHANNELS - 1)
        with self._lock:
            self._master_channel = master_channel
            # 既存のトラック別シークAPIとの互換性のため、すべて同一チャンネルを参照する。
            for track in tracks:
                if streamers.get(track.track_id) is not None:
                    self._channels[track.track_id] = master_channel

        def queue_one() -> bool:
            nonlocal render_snapshot
            previous_snapshot = render_snapshot
            self._apply_automation_at(self.get_timeline_position_sec())
            latest_snapshot = self._param_broker.take_snapshot(render_snapshot.generation)
            if latest_snapshot is not None:
                render_snapshot = latest_snapshot

            # DSP状態の切替はUIスレッドではなく、ここ（チャンク境界）でのみ実行する。
            for track in tracks:
                streamer = streamers.get(track.track_id)
                if streamer is not None:
                    streamer.apply_dsp_params(render_snapshot.track_for(track.track_id).dsp)
            self._apply_master_dsp_snapshot(render_snapshot)

            mix, has_data = self._render_master_mix_chunk(
                tracks, streamers, render_snapshot, previous_snapshot.master.xfade
            )
            if not has_data or mix is None:
                return False
            sound = self._finalize_master_chunk(
                mix,
                master_volume_start=previous_snapshot.master.volume,
                master_volume_target=render_snapshot.master.volume,
            )
            if not master_channel.get_busy():
                master_channel.play(sound)
            else:
                master_channel.queue(sound)
            return True

        # 初期キューを確保し、EQ・UI操作中も音切れしにくい余裕を持たせる。
        queued = False
        for _ in range(QUEUE_AHEAD):
            queued = queue_one() or queued
            if not queued:
                break

        while not self._stop_event.is_set() and queued:
            if self._paused:
                self._param_changed_event.wait(timeout=0.05)
                self._param_changed_event.clear()
                continue

            if master_channel.get_queue() is None:
                queued = queue_one()
                if not queued:
                    # 最終チャンクの再生が完了するまで短く待機してから終了する。
                    time.sleep(CHUNK_SEC * QUEUE_AHEAD + 0.1)
                    break

            # Brokerはgenerationを使うためEvent.clear()由来の通知取りこぼしがない。
            self._param_broker.wait_for_generation(
                render_snapshot.generation, CHUNK_SEC * 0.05
            )

        self._playing = False

    # ------------------------------------------------------------------
    # MICリアルタイム入力ループ（独立スレッド）
    # ------------------------------------------------------------------

    def start_mic_loop(self, tracks: List[TrackModel]):
        """
        MIC入力が割り当てられたトラックのリアルタイム再生ループを開始する。
        ファイル再生ループとは独立したスレッドで動作する。
        """
        if self._mic_thread is not None and self._mic_thread.is_alive():
            return  # 既に起動済み
        self._mic_thread = threading.Thread(
            target=self._mic_loop,
            args=(list(tracks),),
            daemon=True,
            name="MicLoop"
        )
        self._mic_thread.start()

    def stop_mic_loop(self):
        """マイクループを停止する（_stop_eventを共有）。"""
        # _stop_eventは_stream_loopと共有。stop_all()で一括停止される。
        if self._mic_thread is not None:
            self._mic_thread.join(timeout=1.0)
            self._mic_thread = None

    def _mic_loop(self, tracks: List[TrackModel]):
        """
        MIC入力トラックのリアルタイム再生ループ。
        mic_engineからCHUNK_SAMPLESフレームずつ読み出し、
        pygame.mixer.Channelにキューする。
        """
        if not self._initialized:
            return

        any_solo = any(t.solo for t in tracks)

        # MICが割り当てられたトラックのチャンネルを確保
        mic_tracks = [t for t in tracks if mic_engine.has_mic(t.track_id)]
        for track in mic_tracks:
            ch = self._pygame.mixer.find_channel(True)
            if ch is not None:
                with self._lock:
                    self._mic_channels[track.track_id] = ch

        while not self._stop_event.is_set():
            any_solo = any(t.solo for t in tracks)

            for track in tracks:
                if not mic_engine.has_mic(track.track_id):
                    continue
                stream = mic_engine.get_mic_stream(track.track_id)
                if stream is None:
                    continue
                ch = self._mic_channels.get(track.track_id)
                if ch is None:
                    ch = self._pygame.mixer.find_channel(True)
                    if ch is None:
                        continue
                    with self._lock:
                        self._mic_channels[track.track_id] = ch

                # MICチャンクを読み出してステレオに変換
                mono = stream.read_chunk(CHUNK_SAMPLES)  # (CHUNK_SAMPLES,) float32
                stereo = np.stack([mono, mono], axis=1)  # (CHUNK_SAMPLES, 2)

                sound = self._chunk_to_sound(stereo, track, any_solo)
                if not ch.get_busy():
                    ch.play(sound)
                else:
                    if ch.get_queue() is None:
                        ch.queue(sound)

            time.sleep(CHUNK_SEC * 0.5)

        # ループ終了時にチャンネルを解放
        with self._lock:
            for track_id, ch in self._mic_channels.items():
                if ch is not None:
                    try:
                        ch.stop()
                    except Exception:
                        pass
            self._mic_channels.clear()

    def _chunk_to_sound(self, chunk: np.ndarray, track: TrackModel,
                        any_solo: bool) -> object:
        """
        float32 チャンク (CHUNK_SAMPLES, 2) を pygame.mixer.Sound に変換する。
        音量・パン・ミュート・ソロを適用する。
        """
        out = self._apply_track_mix_params(
            chunk, track, any_solo, snapshot.master.xfade, snapshot.master.xfade
        )
        try:
            self._spectrum_manager.push_chunk(track.track_id, out)
        except Exception:
            pass
        return self._finalize_master_chunk(out)

    # ------------------------------------------------------------------
    # リアルタイム音量・パン更新
    # ------------------------------------------------------------------

    def update_track(self, track: TrackModel, any_solo_active: bool):
        """
        再生中のトラックの音量・パン・ミュート・ソロを反映する。
        ストリーミング方式では次チャンクから反映される。
        """
        # tracks_snapshotを更新することで次チャンク生成時に反映される
        with self._lock:
            for i, t in enumerate(self._tracks_snapshot):
                if t.track_id == track.track_id:
                    self._tracks_snapshot[i] = track
                    break
        self._param_broker.submit_track_mix(
            track.track_id,
            volume=track.volume,
            pan=track.pan,
            muted=track.muted,
            solo=track.solo,
        )
        self._param_changed_event.set()

    def update_all_tracks(self, tracks: List[TrackModel]):
        """全トラックの状態をまとめて更新する。"""
        with self._lock:
            self._tracks_snapshot = list(tracks)
        self._reset_broker_snapshot(tracks)
        self._param_changed_event.set()

    def _reset_broker_snapshot(self, tracks: List[TrackModel]):
        """保持済みDSP設定を含むBroker Snapshotを生成する。"""
        with self._master_geq_lock:
            master_geq = self._master_geq_params
        self._param_broker.reset_from_tracks(
            tracks,
            self._master_volume,
            gain_by_track=self._gain_db,
            eq_by_track=self._eq_params,
            effect_preset_by_track=self._effect_presets,
            effect_enabled_by_track=self._effect_enabled,
            aux_enabled_by_track=self._aux_enabled,
            master_geq=master_geq,
            master_xfade=self._master_xfade,
        )

    # ------------------------------------------------------------------
    # マスター音量
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # スペクトラムアナライザー
    # ------------------------------------------------------------------

    def get_spectrum_bands(self, track_id: int):
        """指定トラックのスペクトルバンドデータ（0.0〜1.0 の ndarray）を返す。"""
        return self._spectrum_manager.get_bands(track_id)

    def reset_spectrum(self, track_id: int = None):
        """スペクトルデータをリセットする。"""
        self._spectrum_manager.reset(track_id)

    def set_master_volume(self, volume: float):
        """マスター音量を設定する（0.0〜1.5）。"""
        self._master_volume = max(0.0, min(1.5, volume))
        self._param_broker.submit_master_volume(self._master_volume)
        self._param_changed_event.set()

    def get_master_volume(self) -> float:
        return self._master_volume

    # ------------------------------------------------------------------
    # Phase 25: X-FADER
    # ------------------------------------------------------------------

    def set_track_xfade_assign(self, track_id: int, assign: str):
        """トラックのX-FADER割当をBrokerへ登録する。"""
        assign = str(assign).upper()
        if assign not in ("A", "B", "THRU"):
            assign = "THRU"
        with self._lock:
            for track in self._tracks_snapshot:
                if track.track_id == track_id:
                    track.xfade_assign = assign
                    break
        self._param_broker.submit_track_xfade_assign(track_id, assign)
        self._param_changed_event.set()

    def set_master_xfade(self, position: float = None, curve: str = None,
                         cut_a: bool = None, cut_b: bool = None):
        """MASTER X-FADER状態をBrokerへ登録する。"""
        if position is not None:
            self._master_xfade["position"] = max(0.0, min(1.0, float(position)))
        if curve is not None:
            self._master_xfade["curve"] = curve if curve in ("equal_power", "linear") else "equal_power"
        if cut_a is not None:
            self._master_xfade["cut_a"] = bool(cut_a)
        if cut_b is not None:
            self._master_xfade["cut_b"] = bool(cut_b)
        self._param_broker.submit_master_xfade(
            position=position, curve=curve, cut_a=cut_a, cut_b=cut_b
        )
        self._param_changed_event.set()

    def get_master_xfade_state(self) -> Dict[str, object]:
        """現在のMASTER X-FADER状態をコピーして返す。"""
        return dict(self._master_xfade)

    # ------------------------------------------------------------------
    # Phase 23: マスター・リミッター
    # ------------------------------------------------------------------

    def set_master_limiter(self, enabled: bool, ceiling_db: float = None,
                           release_ms: float = None):
        """マスター・リミッターを更新する。設定は次のマスターチャンクから反映される。"""
        with self._master_limiter_lock:
            self._master_limiter_enabled = bool(enabled)
            if ceiling_db is not None:
                self._master_limiter_ceiling_db = max(-12.0, min(-0.1, float(ceiling_db)))
            if release_ms is not None:
                self._master_limiter_release_ms = max(10.0, min(1000.0, float(release_ms)))
                self._master_limiter.set_release_ms(self._master_limiter_release_ms)
            if not self._master_limiter_enabled:
                self._master_limiter.reset_state()
                self._master_limiter_reduction_db = 0.0
        self._param_changed_event.set()

    def get_master_limiter_state(self) -> Tuple[bool, float, float]:
        """(有効か、ceiling dB、release ms)を返す。"""
        with self._master_limiter_lock:
            return (self._master_limiter_enabled, self._master_limiter_ceiling_db,
                    self._master_limiter_release_ms)

    def get_master_limiter_reduction_db(self) -> float:
        """直近マスターチャンクの最大ゲインリダクション量（dB）を返す。"""
        with self._master_limiter_lock:
            return self._master_limiter_reduction_db

    def _apply_master_limiter_offline(self, pcm: np.ndarray) -> np.ndarray:
        """EXPORT WAV用に、現在のマスター・リミッター設定を独立した状態で適用する。"""
        with self._master_limiter_lock:
            enabled = self._master_limiter_enabled
            ceiling_db = self._master_limiter_ceiling_db
            release_ms = self._master_limiter_release_ms
        if not enabled:
            return pcm.astype(np.float32)
        limiter = MasterLimiter(SAMPLE_RATE, release_ms)
        out, _ = limiter.process(pcm.astype(np.float32), ceiling_db)
        return out

    def get_vu_levels(self) -> Tuple[float, float, float, float, bool, bool]:
        """
        リアルタイムVU/ピークメーター値を返す。
        戻り値: (rms_l, rms_r, peak_l, peak_r, clip_l, clip_r)
        rms/peakは 0.0～1.0 の線形振幅値。
        """
        with self._vu_lock:
            rms_l  = self._vu_rms_l
            rms_r  = self._vu_rms_r
            peak_l = self._vu_peak_l
            peak_r = self._vu_peak_r
            clip_l = self._vu_clip_l
            clip_r = self._vu_clip_r
            # ピーク値は読み取り後に減衰させる（次チャンクまで保持しない）
            self._vu_peak_l = max(0.0, self._vu_peak_l - 0.005)
            self._vu_peak_r = max(0.0, self._vu_peak_r - 0.005)
        return rms_l, rms_r, peak_l, peak_r, clip_l, clip_r

    def reset_vu_clip(self):
        """VUクリップフラグをリセットする（メータークリック時に呼び出す）。"""
        with self._vu_lock:
            self._vu_clip_l = False
            self._vu_clip_r = False

    # ------------------------------------------------------------------
    # マスターGEQ
    # ------------------------------------------------------------------

    def update_master_geq(self, params: GEQParams):
        """MASTER GEQ更新をBrokerへ登録する。DSP切替は音声スレッドで行う。"""
        with self._master_geq_lock:
            self._master_geq_params = params
        self._param_broker.submit_master_geq(params)
        self._param_changed_event.set()

    def get_master_geq_params(self) -> GEQParams:
        """現在のマスターGEQパラメータを返す。"""
        with self._master_geq_lock:
            return self._master_geq_params

    # ------------------------------------------------------------------
    # レベルメーター
    # ------------------------------------------------------------------

    def get_level(self, track_id: int, track: TrackModel, any_solo_active: bool) -> float:
        """トラックの現在レベル（0.0〜1.0）を返す（疑似レベル）。"""
        if not self._initialized or not self._playing:
            return 0.0
        with self._lock:
            s = self._streamers.get(track_id)
            ch = self._channels.get(track_id)
        if s is None or ch is None:
            return 0.0
        if not ch.get_busy():
            return 0.0
        if not track.is_audible(any_solo_active):
            return 0.0
        return track.volume * self._master_volume

    # ------------------------------------------------------------------
    # 再生位置
    # ------------------------------------------------------------------

    def get_playback_position_ms(self, track_id: int) -> int:
        """指定トラックの現在再生位置（ミリ秒）を返す。"""
        with self._lock:
            s = self._streamers.get(track_id)
        if s is None:
            return -1
        return int(s.get_pos_sec() * 1000)

    # ------------------------------------------------------------------
    # 波形ピーク
    # ------------------------------------------------------------------

    def get_waveform_peaks(self, track_id: int, num_points: int = 200):
        """指定トラックの波形ピーク値リストを返す（波形表示用）。"""
        with self._lock:
            pcm = self._pcm_data.get(track_id)
        if pcm is None:
            return []

        mono = pcm.mean(axis=1)
        total = len(mono)
        if total == 0:
            return []

        chunk_size = max(1, total // num_points)
        peaks = []
        for i in range(num_points):
            start = i * chunk_size
            end = min(start + chunk_size, total)
            if start >= total:
                peaks.append(0.0)
            else:
                peaks.append(float(np.max(np.abs(mono[start:end]))))

        max_peak = max(peaks) if peaks else 1.0
        if max_peak > 0:
            peaks = [p / max_peak for p in peaks]
        return peaks

    def get_sound_duration(self, track_id: int) -> float:
        """指定トラックの音声長（秒）を返す。"""
        with self._lock:
            pcm = self._pcm_data.get(track_id)
        if pcm is None:
            return 0.0
        return len(pcm) / SAMPLE_RATE

    def get_track_duration_sec(self, track_id: int) -> float:
        """指定トラックの音声長（秒）を返す互換API。"""
        return self.get_sound_duration(track_id)

    def get_track_position_sec(self, track_id: int) -> float:
        """指定トラックの現在再生位置（秒）を返す。再生中以外は 0.0。"""
        with self._lock:
            s = self._streamers.get(track_id)
        if s is None:
            return 0.0
        return s.get_pos_sec()

    def seek_track(self, track_id: int, pos_sec: float):
        """指定トラックの再生位置を秒単位でシークする。再生中のみ有効。"""
        with self._lock:
            s = self._streamers.get(track_id)
            pcm = self._pcm_data.get(track_id)
        if s is None or pcm is None:
            return
        total_samples = len(pcm)
        target = int(max(0.0, min(pos_sec, total_samples / SAMPLE_RATE)) * SAMPLE_RATE)
        s.seek(target)
        # 既にキュー済みの音声を破棄し、指定位置から再生し直す
        with self._lock:
            ch = self._channels.get(track_id)
        if ch is not None:
            try:
                ch.stop()
            except Exception:
                pass
        # パラメータ変更イベントを発火してストリームループに即座に反映
        self._param_changed_event.set()

    # ------------------------------------------------------------------
    # 録音制御（REC START / REC STOP）
    # ------------------------------------------------------------------

    def start_rec(self):
        """録音を開始する。既存バッファをクリアして新規録音を開始。"""
        with self._rec_lock:
            self._rec_buffer = []
            self._rec_active = True
            self._rec_start_time = time.time()

    def stop_rec(self) -> float:
        """録音を停止して録音時間（秒）を返す。"""
        with self._rec_lock:
            self._rec_active = False
            if self._rec_buffer:
                total_samples = sum(len(c) for c in self._rec_buffer)
                return total_samples / SAMPLE_RATE
            return 0.0

    def get_rec_duration_sec(self) -> float:
        """現在の録音バッファの長さ（秒）を返す。録音中はリアルタイムに増加。"""
        with self._rec_lock:
            if not self._rec_buffer:
                return 0.0
            return sum(len(c) for c in self._rec_buffer) / SAMPLE_RATE

    def has_rec_data(self) -> bool:
        """録音済みデータが存在するかどうかを返す。"""
        with self._rec_lock:
            return len(self._rec_buffer) > 0

    def export_rec_buffer(self, output_path: str) -> ExportResult:
        """録音バッファをWAVファイルに書き出す。"""
        with self._rec_lock:
            if not self._rec_buffer:
                return ExportResult(
                    success=False,
                    error_message="録音データがありません。REC START → REC STOP を実行してから書き出してください。"
                )
            # バッファを結合（各チャンクは float32 (CHUNK_SAMPLES, 2)）
            pcm = np.concatenate(self._rec_buffer, axis=0)  # (total_samples, 2)

        # RECバッファは録音時点でマスターGEQ・リミッターを通過済みの最終出力。
        # 書き出し後に設定を変更しても、録音済みの内容は変えない。
        max_val = float(MAX_AMPLITUDE)
        mix_left  = pcm[:, 0].astype(np.float64) * max_val
        mix_right = pcm[:, 1].astype(np.float64) * max_val
        total_len = len(pcm)

        peak_left  = float(np.max(np.abs(mix_left)))
        peak_right = float(np.max(np.abs(mix_right)))
        peak_level = max(peak_left, peak_right) / max_val

        clip_left  = int(np.sum(np.abs(mix_left)  > max_val))
        clip_right = int(np.sum(np.abs(mix_right) > max_val))
        clipping_count = clip_left + clip_right
        total_samples  = total_len * 2
        clipping_ratio = clipping_count / total_samples if total_samples > 0 else 0.0
        clipping_detected = clipping_count > 0

        if clipping_detected:
            scale = max_val / max(peak_left, peak_right)
            mix_left  *= scale
            mix_right *= scale

        mix_left  = np.clip(mix_left,  -max_val, max_val).astype(np.int16)
        mix_right = np.clip(mix_right, -max_val, max_val).astype(np.int16)

        interleaved = np.empty(total_len * 2, dtype=np.int16)
        interleaved[0::2] = mix_left
        interleaved[1::2] = mix_right

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        try:
            with wave.open(output_path, 'w') as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(SAMPLE_WIDTH)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(interleaved.tobytes())
        except Exception as e:
            return ExportResult(success=False, error_message=f"WAVファイルの書き込みに失敗しました: {e}")

        return ExportResult(
            success=True,
            output_path=output_path,
            duration_sec=total_len / SAMPLE_RATE,
            clipping_detected=clipping_detected,
            clipping_count=clipping_count,
            clipping_ratio=clipping_ratio,
            peak_level=peak_level,
        )

    # ------------------------------------------------------------------
    # ミックス書き出し
    # ------------------------------------------------------------------

    def export_mix(self, tracks: List[TrackModel], output_path: str) -> ExportResult:
        """
        現在のゲイン/EQ/エフェクト/音量/パン/ミュート/ソロ/マスター音量を
        反映してWAVファイルに書き出す。
        """
        if not self._initialized:
            return ExportResult(success=False, error_message="AudioEngine が初期化されていません。")

        export_snapshot = self._param_broker.snapshot()
        track_arrays: List[Tuple[np.ndarray, TrackModel, TrackMixParams]] = []
        max_len = 0

        with self._lock:
            pcm_snapshot = dict(self._pcm_data)

        for track in tracks:
            pcm = pcm_snapshot.get(track.track_id)
            if pcm is None:
                continue
            mix_params = export_snapshot.track_for(track.track_id)
            if not mix_params.is_audible(export_snapshot.any_solo):
                continue

            # ゲイン適用
            gain_db = mix_params.dsp.gain_db
            processed = pcm.copy()
            if abs(gain_db) > 0.01:
                processed = processed * (10.0 ** (gain_db / 20.0))

            # EQ適用
            eq_params = mix_params.dsp.eq.to_params()
            if not eq_params.is_flat():
                eq_eng = EQEngine(SAMPLE_RATE)
                eq_eng.set_params(eq_params)
                processed = eq_eng.apply_eq(processed)

            # エフェクト適用
            preset = mix_params.dsp.effect_preset
            enabled = mix_params.dsp.effect_enabled and mix_params.dsp.aux_enabled
            if enabled and preset != "None" and preset in EFFECT_PRESETS:
                fx_eng = EffectEngine(SAMPLE_RATE)
                processed = fx_eng.apply(processed, preset)

            processed = np.clip(processed, -1.0, 1.0)
            track_arrays.append((processed, track, mix_params))
            max_len = max(max_len, len(processed))

        if max_len == 0 or not track_arrays:
            return ExportResult(
                success=False,
                error_message="書き出せるトラックがありません。少なくとも1つのトラックに音声を読み込んでください。"
            )

        mix_left  = np.zeros(max_len, dtype=np.float64)
        mix_right = np.zeros(max_len, dtype=np.float64)

        for processed, track, mix_params in track_arrays:
            n = len(processed)
            left_gain, right_gain = self._calc_pan_volumes(
                mix_params.volume * export_snapshot.master.volume, mix_params.pan
            )
            mix_left[:n]  += processed[:, 0].astype(np.float64) * left_gain * MAX_AMPLITUDE
            mix_right[:n] += processed[:, 1].astype(np.float64) * right_gain * MAX_AMPLITUDE

        max_val = float(MAX_AMPLITUDE)
        peak_left  = float(np.max(np.abs(mix_left)))
        peak_right = float(np.max(np.abs(mix_right)))
        peak_level = max(peak_left, peak_right) / max_val

        clip_left  = int(np.sum(np.abs(mix_left)  > max_val))
        clip_right = int(np.sum(np.abs(mix_right) > max_val))
        clipping_count = clip_left + clip_right
        total_samples  = max_len * 2
        clipping_ratio = clipping_count / total_samples if total_samples > 0 else 0.0
        clipping_detected = clipping_count > 0

        # マスターGEQ適用（ミックス後）
        geq_params = export_snapshot.master.dsp.geq.to_params()
        if not geq_params.is_flat():
            mix_stereo = np.stack(
                [mix_left / MAX_AMPLITUDE, mix_right / MAX_AMPLITUDE], axis=1
            ).astype(np.float32)
            geq_eng = GEQEngine(SAMPLE_RATE)
            geq_eng.set_params(geq_params)
            mix_stereo = geq_eng.apply_vectorized(mix_stereo)
            mix_left  = mix_stereo[:, 0].astype(np.float64) * MAX_AMPLITUDE
            mix_right = mix_stereo[:, 1].astype(np.float64) * MAX_AMPLITUDE

        # マスターGEQ後が最終ミックスバス。ここでマスター・リミッターを通す。
        mix_stereo = np.stack(
            [mix_left / MAX_AMPLITUDE, mix_right / MAX_AMPLITUDE], axis=1
        ).astype(np.float32)
        mix_stereo = self._apply_master_limiter_offline(mix_stereo)
        mix_left = mix_stereo[:, 0].astype(np.float64) * MAX_AMPLITUDE
        mix_right = mix_stereo[:, 1].astype(np.float64) * MAX_AMPLITUDE

        # 結果は最終出力ベースで返す。リミッターOFF時だけ安全正規化にフォールバックする。
        peak_left = float(np.max(np.abs(mix_left)))
        peak_right = float(np.max(np.abs(mix_right)))
        peak_level = max(peak_left, peak_right) / max_val
        clip_left = int(np.sum(np.abs(mix_left) > max_val))
        clip_right = int(np.sum(np.abs(mix_right) > max_val))
        clipping_count = clip_left + clip_right
        clipping_ratio = clipping_count / total_samples if total_samples > 0 else 0.0
        clipping_detected = clipping_count > 0
        if clipping_detected:
            scale = max_val / max(peak_left, peak_right)
            mix_left *= scale
            mix_right *= scale

        mix_left  = np.clip(mix_left,  -max_val, max_val).astype(np.int16)
        mix_right = np.clip(mix_right, -max_val, max_val).astype(np.int16)

        interleaved = np.empty(max_len * 2, dtype=np.int16)
        interleaved[0::2] = mix_left
        interleaved[1::2] = mix_right

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        try:
            with wave.open(output_path, 'w') as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(SAMPLE_WIDTH)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(interleaved.tobytes())
        except Exception as e:
            return ExportResult(success=False, error_message=f"WAVファイルの書き込みに失敗しました: {e}")

        return ExportResult(
            success=True,
            output_path=output_path,
            duration_sec=max_len / SAMPLE_RATE,
            clipping_detected=clipping_detected,
            clipping_count=clipping_count,
            clipping_ratio=clipping_ratio,
            peak_level=peak_level,
        )

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_pan_volumes(volume: float, pan: float) -> Tuple[float, float]:
        """等パワーパンニング（sin/cos カーブ）で左右チャンネル音量を計算する。"""
        angle = (pan + 1.0) / 2.0 * (math.pi / 2.0)
        left  = volume * math.cos(angle)
        right = volume * math.sin(angle)
        return left, right

    def cleanup(self):
        """エンジンを終了する。アプリ終了時に呼ぶ。"""
        self.stop_all()
        self.stop_mic_loop()
        mic_engine.stop_all()  # 全マイクストリームを停止
        if self._initialized:
            try:
                self._pygame.mixer.quit()
            except Exception:
                pass
        self._initialized = False
