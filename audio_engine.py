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
from eq_engine import EQEngine, EQParams
from effect_engine import EffectEngine, EFFECT_PRESETS
from geq_engine import GEQEngine, GEQParams


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

        # パラメータ（スレッドセーフに更新）
        self._gain_db: float = 0.0
        self._eq_params: EQParams = EQParams()
        self._effect_preset: str = "None"
        self._effect_enabled: bool = False

        # DSPエンジン（チャンク生成スレッドのみが使用）
        self._eq_engine = EQEngine(sample_rate)
        self._effect_engine = EffectEngine(sample_rate)

    # --- パラメータ更新（UIスレッドから呼ばれる） ---

    def set_gain(self, gain_db: float):
        with self._lock:
            self._gain_db = max(-24.0, min(24.0, gain_db))

    def set_eq(self, params: EQParams):
        with self._lock:
            self._eq_params = params

    def set_effect(self, preset_name: str, enabled: bool):
        with self._lock:
            self._effect_preset = preset_name
            self._effect_enabled = enabled

    # --- 再生位置 ---

    def reset(self):
        """再生位置を先頭に戻す。"""
        with self._lock:
            self._pos = 0

    def get_pos_sec(self) -> float:
        """現在の再生位置（秒）を返す。"""
        return self._pos / SAMPLE_RATE

    def is_finished(self) -> bool:
        return self._pos >= self._n_samples

    # --- チャンク生成（チャンク生成スレッドから呼ばれる） ---

    def next_chunk(self) -> Optional[np.ndarray]:
        """
        次のチャンク（CHUNK_SAMPLES サンプル）を生成して返す。
        再生終了時は None を返す。
        返り値は shape (CHUNK_SAMPLES, 2) dtype float32。
        """
        with self._lock:
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

            # ゲイン適用
            if abs(self._gain_db) > 0.01:
                linear = 10.0 ** (self._gain_db / 20.0)
                chunk = chunk * linear

            # EQ適用
            if not self._eq_params.is_flat():
                self._eq_engine.set_params(self._eq_params)
                chunk = self._eq_engine.apply_eq(chunk)

            # エフェクト適用
            if self._effect_enabled and self._effect_preset != "None":
                chunk = self._effect_engine.apply(chunk, self._effect_preset)

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

        # pygame チャンネル
        self._channels: Dict[int, Optional[object]] = {}

        # 再生状態
        self._playing = False
        self._tracks_snapshot: List[TrackModel] = []
        self._master_volume: float = 1.0

        # マスターGEQ
        self._master_geq_params: GEQParams = GEQParams()
        self._master_geq_engine: GEQEngine = GEQEngine(SAMPLE_RATE)
        self._master_geq_lock = threading.Lock()

        # ストリーミングスレッド
        self._stream_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

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
        """ゲインを更新する。再生中は次チャンクから即反映。"""
        gain_db = max(-24.0, min(24.0, gain_db))
        self._gain_db[track_id] = gain_db
        with self._lock:
            s = self._streamers.get(track_id)
        if s is not None:
            s.set_gain(gain_db)

    def update_eq(self, track_id: int, params: EQParams):
        """EQパラメータを更新する。再生中は次チャンクから即反映。"""
        self._eq_params[track_id] = params
        with self._lock:
            s = self._streamers.get(track_id)
        if s is not None:
            s.set_eq(params)

    def update_effect(self, track_id: int, preset_name: str, enabled: bool):
        """エフェクトを更新する。再生中は次チャンクから即反映。"""
        self._effect_presets[track_id] = preset_name
        self._effect_enabled[track_id] = enabled
        with self._lock:
            s = self._streamers.get(track_id)
        if s is not None:
            s.set_effect(preset_name, enabled)

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
                self._streamers[track.track_id] = s
                self._channels[track.track_id] = None

        self._playing = True
        self._stop_event.clear()
        self._stream_thread = threading.Thread(
            target=self._stream_loop,
            daemon=True,
            name="AudioStreamLoop"
        )
        self._stream_thread.start()

    def stop_all(self):
        """全トラックを停止する。"""
        self._stop_event.set()
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
        self._playing = False

    def is_playing(self) -> bool:
        """いずれかのトラックが再生中かどうかを返す。"""
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

        # チャンネルを割り当て
        channel_map: Dict[int, object] = {}
        for track in tracks:
            if streamers.get(track.track_id) is None:
                continue
            ch = self._pygame.mixer.find_channel(True)
            if ch is None:
                continue
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

            time.sleep(CHUNK_SEC * 0.25)  # ポーリング間隔

        self._playing = False

    def _chunk_to_sound(self, chunk: np.ndarray, track: TrackModel,
                        any_solo: bool) -> object:
        """
        float32 チャンク (CHUNK_SAMPLES, 2) を pygame.mixer.Sound に変換する。
        音量・パン・ミュート・ソロを適用する。
        """
        if track.is_audible(any_solo):
            left_gain, right_gain = self._calc_pan_volumes(
                track.volume * self._master_volume, track.pan
            )
        else:
            left_gain, right_gain = 0.0, 0.0

        out = chunk.copy()
        out[:, 0] *= left_gain
        out[:, 1] *= right_gain

        # マスターGEQ適用
        with self._master_geq_lock:
            geq_params = self._master_geq_params
            geq_engine = self._master_geq_engine
        if not geq_params.is_flat():
            out = geq_engine.apply_vectorized(out)

        out = np.clip(out, -1.0, 1.0)
        i16 = (out * 32767).astype(np.int16)
        return self._pygame.sndarray.make_sound(i16)

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

    def update_all_tracks(self, tracks: List[TrackModel]):
        """全トラックの状態をまとめて更新する。"""
        with self._lock:
            self._tracks_snapshot = list(tracks)

    # ------------------------------------------------------------------
    # マスター音量
    # ------------------------------------------------------------------

    def set_master_volume(self, volume: float):
        """マスター音量を設定する（0.0〜1.5）。"""
        self._master_volume = max(0.0, min(1.5, volume))

    def get_master_volume(self) -> float:
        return self._master_volume

    # ------------------------------------------------------------------
    # マスターGEQ
    # ------------------------------------------------------------------

    def update_master_geq(self, params: GEQParams):
        """マスターGEQパラメータを更新する。再生中は次チャンクから即反映。"""
        with self._master_geq_lock:
            self._master_geq_params = params
            self._master_geq_engine.set_params(params)

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

        any_solo = any(t.solo for t in tracks)
        track_arrays: List[Tuple[np.ndarray, TrackModel]] = []
        max_len = 0

        with self._lock:
            pcm_snapshot = dict(self._pcm_data)

        for track in tracks:
            pcm = pcm_snapshot.get(track.track_id)
            if pcm is None:
                continue
            if not track.is_audible(any_solo):
                continue

            # ゲイン適用
            gain_db = self._gain_db.get(track.track_id, 0.0)
            processed = pcm.copy()
            if abs(gain_db) > 0.01:
                processed = processed * (10.0 ** (gain_db / 20.0))

            # EQ適用
            eq_params = self._eq_params.get(track.track_id, EQParams())
            if not eq_params.is_flat():
                eq_eng = EQEngine(SAMPLE_RATE)
                eq_eng.set_params(eq_params)
                processed = eq_eng.apply_eq(processed)

            # エフェクト適用
            preset = self._effect_presets.get(track.track_id, "None")
            enabled = self._effect_enabled.get(track.track_id, False)
            if enabled and preset != "None" and preset in EFFECT_PRESETS:
                fx_eng = EffectEngine(SAMPLE_RATE)
                processed = fx_eng.apply(processed, preset)

            processed = np.clip(processed, -1.0, 1.0)
            track_arrays.append((processed, track))
            max_len = max(max_len, len(processed))

        if max_len == 0 or not track_arrays:
            return ExportResult(
                success=False,
                error_message="書き出せるトラックがありません。少なくとも1つのトラックに音声を読み込んでください。"
            )

        mix_left  = np.zeros(max_len, dtype=np.float64)
        mix_right = np.zeros(max_len, dtype=np.float64)

        for processed, track in track_arrays:
            n = len(processed)
            left_gain, right_gain = self._calc_pan_volumes(
                track.volume * self._master_volume, track.pan
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
        with self._master_geq_lock:
            geq_params = self._master_geq_params
        if not geq_params.is_flat():
            mix_stereo = np.stack(
                [mix_left / MAX_AMPLITUDE, mix_right / MAX_AMPLITUDE], axis=1
            ).astype(np.float32)
            geq_eng = GEQEngine(SAMPLE_RATE)
            geq_eng.set_params(geq_params)
            mix_stereo = geq_eng.apply_vectorized(mix_stereo)
            mix_left  = mix_stereo[:, 0].astype(np.float64) * MAX_AMPLITUDE
            mix_right = mix_stereo[:, 1].astype(np.float64) * MAX_AMPLITUDE

        if clipping_detected:
            scale = max_val / max(peak_left, peak_right)
            mix_left  *= scale
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
        if self._initialized:
            try:
                self._pygame.mixer.quit()
            except Exception:
                pass
        self._initialized = False
