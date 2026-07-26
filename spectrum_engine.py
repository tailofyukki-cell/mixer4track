"""
spectrum_engine.py
スペクトラムアナライザー用FFT計算エンジン。

- チャンク（numpy float32 stereo）からFFTを計算し、
  対数スケールの周波数バンドに集約する。
- フォールオフ（減衰）処理により滑らかなアニメーションを実現する。
- スレッドセーフ（audio_engineスレッドから書き込み、UIスレッドから読み取り）。
"""

import threading
import math
import numpy as np
from typing import List, Optional

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
SAMPLE_RATE   = 44100
NUM_BANDS     = 48          # 表示バンド数（対数スケールで20Hz〜20kHz）
FREQ_MIN      = 20.0        # 最低周波数 (Hz)
FREQ_MAX      = 20000.0     # 最高周波数 (Hz)
DB_FLOOR      = -60.0       # 最低dB（これ以下は0として扱う）
DB_CEIL       = 0.0         # 最高dB（クリップ上限）
FALLOFF_RATE  = 0.75        # フォールオフ係数（1フレームで前フレームの何割残すか）
ATTACK_RATE   = 1.0         # アタック係数（上昇は即時）


def _build_band_edges(num_bands: int, freq_min: float, freq_max: float,
                      sample_rate: int, fft_size: int) -> List[tuple]:
    """
    対数スケールで num_bands 個の周波数バンドのエッジ（FFTビンインデックス）を計算する。

    Returns
    -------
    list of (bin_lo, bin_hi) : 各バンドに対応するFFTビンの範囲（inclusive）
    """
    log_min = math.log10(freq_min)
    log_max = math.log10(freq_max)
    freq_per_bin = sample_rate / fft_size  # 1ビンあたりの周波数幅 (Hz)
    edges = []
    for i in range(num_bands):
        f_lo = 10 ** (log_min + (log_max - log_min) * i / num_bands)
        f_hi = 10 ** (log_min + (log_max - log_min) * (i + 1) / num_bands)
        bin_lo = max(1, int(f_lo / freq_per_bin))
        bin_hi = max(bin_lo, int(f_hi / freq_per_bin))
        edges.append((bin_lo, bin_hi))
    return edges


class SpectrumEngine:
    """
    1トラック分のスペクトルデータを管理するクラス。
    audio_engineスレッドから push_chunk() でデータを更新し、
    UIスレッドから get_bands() でバンドデータを取得する。
    """

    # FFTサイズ（チャンクサイズに合わせる。2の累乗が高速）
    FFT_SIZE = 2048

    def __init__(self, sample_rate: int = SAMPLE_RATE, num_bands: int = NUM_BANDS):
        self._sample_rate = sample_rate
        self._num_bands   = num_bands
        self._lock        = threading.Lock()

        # フォールオフ後のバンド値（0.0〜1.0）
        self._bands: np.ndarray = np.zeros(num_bands, dtype=np.float32)

        # ハン窓（スペクトル漏れ低減）
        self._window = np.hanning(self.FFT_SIZE).astype(np.float32)

        # バンドエッジ（ビンインデックス）
        self._band_edges = _build_band_edges(
            num_bands, FREQ_MIN, FREQ_MAX, sample_rate, self.FFT_SIZE
        )

        # 直近チャンクのリングバッファ（FFT_SIZE サンプル分）
        self._buf: np.ndarray = np.zeros(self.FFT_SIZE, dtype=np.float32)

    def push_chunk(self, chunk: np.ndarray):
        """
        チャンク（shape: (CHUNK_SAMPLES, 2) float32）を受け取り、
        FFTを計算してバンドデータを更新する。
        audio_engineスレッドから呼ばれる。
        """
        if chunk is None or len(chunk) == 0:
            return

        # モノラル化（L+R の平均）
        mono = chunk.mean(axis=1).astype(np.float32)

        # リングバッファに追加（最新 FFT_SIZE サンプルを保持）
        n = len(mono)
        if n >= self.FFT_SIZE:
            self._buf[:] = mono[-self.FFT_SIZE:]
        else:
            self._buf[:-n] = self._buf[n:]
            self._buf[-n:] = mono

        # ハン窓適用 → FFT
        windowed = self._buf * self._window
        spectrum  = np.abs(np.fft.rfft(windowed, n=self.FFT_SIZE))

        # 振幅 → dBに変換（ゼロ除算防止）
        eps = 1e-10
        db = 20.0 * np.log10(spectrum / (self.FFT_SIZE / 2) + eps)

        # バンドごとに最大値を集約
        new_bands = np.zeros(self._num_bands, dtype=np.float32)
        for i, (b_lo, b_hi) in enumerate(self._band_edges):
            b_hi_clamp = min(b_hi + 1, len(db))
            if b_lo < b_hi_clamp:
                band_db = float(np.max(db[b_lo:b_hi_clamp]))
            else:
                band_db = float(db[b_lo]) if b_lo < len(db) else DB_FLOOR
            # dBを 0.0〜1.0 に正規化
            normalized = (band_db - DB_FLOOR) / (DB_CEIL - DB_FLOOR)
            new_bands[i] = float(np.clip(normalized, 0.0, 1.0))

        # フォールオフ処理（アタックは即時、リリースは減衰）
        with self._lock:
            rising  = new_bands > self._bands
            self._bands = np.where(
                rising,
                new_bands * ATTACK_RATE + self._bands * (1.0 - ATTACK_RATE),
                self._bands * FALLOFF_RATE + new_bands * (1.0 - FALLOFF_RATE)
            )

    def get_bands(self) -> np.ndarray:
        """
        現在のバンドデータ（0.0〜1.0 の float32 配列）を返す。
        UIスレッドから呼ばれる。
        """
        with self._lock:
            return self._bands.copy()

    def reset(self):
        """バンドデータをゼロリセットする（停止時に呼ぶ）。"""
        with self._lock:
            self._bands[:] = 0.0
        self._buf[:] = 0.0


class SpectrumManager:
    """
    全トラック分の SpectrumEngine を管理するシングルトン的クラス。
    audio_engine から push_chunk() を呼び、
    UIウィジェットから get_bands() を呼ぶ。
    """

    def __init__(self, num_tracks: int = 16,
                 sample_rate: int = SAMPLE_RATE,
                 num_bands: int = NUM_BANDS):
        self._engines: dict = {
            i: SpectrumEngine(sample_rate, num_bands)
            for i in range(num_tracks)
        }
        # MASTER用（track_id = -1）
        self._engines[-1] = SpectrumEngine(sample_rate, num_bands)

    def push_chunk(self, track_id: int, chunk: np.ndarray):
        """指定トラックのチャンクをFFT処理する。"""
        engine = self._engines.get(track_id)
        if engine is not None:
            engine.push_chunk(chunk)

    def get_bands(self, track_id: int) -> np.ndarray:
        """指定トラックのバンドデータを返す。"""
        engine = self._engines.get(track_id)
        if engine is None:
            return np.zeros(NUM_BANDS, dtype=np.float32)
        return engine.get_bands()

    def reset(self, track_id: Optional[int] = None):
        """指定トラック（またはすべて）をリセットする。"""
        if track_id is None:
            for e in self._engines.values():
                e.reset()
        else:
            engine = self._engines.get(track_id)
            if engine:
                engine.reset()
