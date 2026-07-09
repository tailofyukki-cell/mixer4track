"""
geq_engine.py
16バンド グラフィックイコライザー（GEQ）のDSP実装。

Phase 9: MASTERトラックGEQ機能
  - GEQ Low  : 31Hz, 63Hz, 125Hz, 250Hz, 315Hz, 400Hz, 500Hz, 630Hz  (8バンド)
  - GEQ Hi   : 800Hz, 1kHz, 2kHz, 4kHz, 6kHz, 8kHz, 12kHz, 16kHz    (8バンド)
  - 各バンド: ピーキングフィルタ（2次IIR）
  - ゲイン範囲: -15dB ～ +15dB、初期値 0dB
"""

import math
import numpy as np
from typing import List, Tuple

# ------------------------------------------------------------------
# GEQバンド定義
# ------------------------------------------------------------------

GEQ_LOW_BANDS: List[Tuple[str, float]] = [
    ("31Hz",  31.0),
    ("63Hz",  63.0),
    ("125Hz", 125.0),
    ("250Hz", 250.0),
    ("315Hz", 315.0),
    ("400Hz", 400.0),
    ("500Hz", 500.0),
    ("630Hz", 630.0),
]

GEQ_HI_BANDS: List[Tuple[str, float]] = [
    ("800Hz",  800.0),
    ("1kHz",  1000.0),
    ("2kHz",  2000.0),
    ("4kHz",  4000.0),
    ("6kHz",  6000.0),
    ("8kHz",  8000.0),
    ("12kHz", 12000.0),
    ("16kHz", 16000.0),
]

GEQ_ALL_BANDS = GEQ_LOW_BANDS + GEQ_HI_BANDS  # 16バンド

GEQ_GAIN_MIN = -15.0
GEQ_GAIN_MAX = +15.0
GEQ_GAIN_DEFAULT = 0.0

# バンドごとのQ値（隣接バンドとのオーバーラップを考慮）
_Q_VALUES = {
    31.0:    1.4,
    63.0:    1.4,
    125.0:   1.4,
    250.0:   1.4,
    315.0:   1.6,
    400.0:   1.6,
    500.0:   1.6,
    630.0:   1.6,
    800.0:   1.6,
    1000.0:  1.6,
    2000.0:  1.6,
    4000.0:  1.6,
    6000.0:  1.8,
    8000.0:  1.8,
    12000.0: 1.8,
    16000.0: 1.8,
}


# ------------------------------------------------------------------
# GEQParams（16バンドのゲイン値を保持）
# ------------------------------------------------------------------

class GEQParams:
    """16バンドGEQのゲイン値を保持するデータクラス。"""

    def __init__(self):
        # {center_freq: gain_db}
        self._gains: dict = {freq: 0.0 for _, freq in GEQ_ALL_BANDS}

    def set_gain(self, freq: float, gain_db: float):
        """指定周波数バンドのゲインを設定する。"""
        if freq in self._gains:
            self._gains[freq] = max(GEQ_GAIN_MIN, min(GEQ_GAIN_MAX, gain_db))

    def get_gain(self, freq: float) -> float:
        """指定周波数バンドのゲインを取得する。"""
        return self._gains.get(freq, 0.0)

    def get_all_gains(self) -> dict:
        """全バンドのゲイン辞書を返す。"""
        return dict(self._gains)

    def is_flat(self) -> bool:
        """全バンドが0dBかどうかを返す。"""
        return all(abs(g) < 0.01 for g in self._gains.values())

    def reset_low(self):
        """GEQ Lowバンドをリセットする。"""
        for _, freq in GEQ_LOW_BANDS:
            self._gains[freq] = 0.0

    def reset_hi(self):
        """GEQ Hiバンドをリセットする。"""
        for _, freq in GEQ_HI_BANDS:
            self._gains[freq] = 0.0

    def reset_all(self):
        """全バンドをリセットする。"""
        for freq in self._gains:
            self._gains[freq] = 0.0

    def to_dict(self) -> dict:
        return {str(k): v for k, v in self._gains.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "GEQParams":
        obj = cls()
        for k, v in d.items():
            try:
                obj._gains[float(k)] = float(v)
            except (ValueError, KeyError):
                pass
        return obj


# ------------------------------------------------------------------
# ピーキングフィルタ係数計算
# ------------------------------------------------------------------

def _peaking_coeffs(fc: float, gain_db: float, Q: float, fs: float) -> Tuple:
    """
    2次ピーキングフィルタのIIR係数を計算する。
    Returns: (b0, b1, b2, a1, a2)
    """
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * fc / fs
    alpha = math.sin(w0) / (2.0 * Q)

    b0 =  1.0 + alpha * A
    b1 = -2.0 * math.cos(w0)
    b2 =  1.0 - alpha * A
    a0 =  1.0 + alpha / A
    a1 = -2.0 * math.cos(w0)
    a2 =  1.0 - alpha / A

    return (b0/a0, b1/a0, b2/a0, a1/a0, a2/a0)


# ------------------------------------------------------------------
# GEQEngine
# ------------------------------------------------------------------

class GEQEngine:
    """
    16バンドグラフィックイコライザーのDSPエンジン。
    各バンドのピーキングフィルタを直列に適用する。
    """

    def __init__(self, sample_rate: int = 44100):
        self._fs = sample_rate
        self._params = GEQParams()
        # フィルタ状態（各バンド × 2チャンネル × 2ステート）
        self._states: dict = {}  # {freq: np.ndarray shape(2,2)}
        self._reset_states()

    def _reset_states(self):
        for _, freq in GEQ_ALL_BANDS:
            self._states[freq] = np.zeros((2, 2), dtype=np.float64)

    def set_params(self, params: GEQParams):
        """GEQパラメータを設定する。"""
        self._params = params

    def apply(self, pcm: np.ndarray) -> np.ndarray:
        """
        PCMデータにGEQを適用する。
        pcm: shape (n_samples, 2), dtype float32
        Returns: shape (n_samples, 2), dtype float32
        """
        if self._params.is_flat():
            return pcm

        out = pcm.astype(np.float64)

        for _, freq in GEQ_ALL_BANDS:
            gain_db = self._params.get_gain(freq)
            if abs(gain_db) < 0.01:
                continue

            Q = _Q_VALUES.get(freq, 1.4)
            b0, b1, b2, a1, a2 = _peaking_coeffs(freq, gain_db, Q, self._fs)

            # 2チャンネルに適用（scipy.signal.lfilterをnumpyで実装）
            for ch in range(2):
                x = out[:, ch]
                y = np.zeros_like(x)
                s = self._states[freq][ch]  # [s1, s2]

                # Direct Form II Transposed
                for i in range(len(x)):
                    yn = b0 * x[i] + s[0]
                    s[0] = b1 * x[i] - a1 * yn + s[1]
                    s[1] = b2 * x[i] - a2 * yn
                    y[i] = yn

                out[:, ch] = y
                self._states[freq][ch] = s

        return out.astype(np.float32)

    def apply_vectorized(self, pcm: np.ndarray) -> np.ndarray:
        """
        scipy.signal.lfilterを使った高速版（利用可能な場合）。
        """
        try:
            from scipy.signal import lfilter
        except ImportError:
            return self.apply(pcm)

        if self._params.is_flat():
            return pcm

        out = pcm.astype(np.float64)

        for _, freq in GEQ_ALL_BANDS:
            gain_db = self._params.get_gain(freq)
            if abs(gain_db) < 0.01:
                continue

            Q = _Q_VALUES.get(freq, 1.4)
            b0, b1, b2, a1, a2 = _peaking_coeffs(freq, gain_db, Q, self._fs)
            b = np.array([b0, b1, b2])
            a = np.array([1.0, a1, a2])

            for ch in range(2):
                out[:, ch] = lfilter(b, a, out[:, ch])

        return out.astype(np.float32)


# ------------------------------------------------------------------
# GEQカーブ計算（表示用）
# ------------------------------------------------------------------

def get_geq_response_db(params: GEQParams, n_points: int = 200,
                         fs: int = 44100) -> List[Tuple[float, float]]:
    """
    GEQの周波数特性を計算して返す。
    Returns: [(freq_hz, gain_db), ...]
    """
    if params.is_flat():
        return []

    freqs = np.logspace(np.log10(20), np.log10(20000), n_points)
    total_response = np.zeros(n_points)

    for _, fc in GEQ_ALL_BANDS:
        gain_db = params.get_gain(fc)
        if abs(gain_db) < 0.01:
            continue

        Q = _Q_VALUES.get(fc, 1.4)
        A = 10.0 ** (gain_db / 40.0)
        w0 = 2.0 * math.pi * fc / fs
        alpha = math.sin(w0) / (2.0 * Q)

        b0 =  1.0 + alpha * A
        b1 = -2.0 * math.cos(w0)
        b2 =  1.0 - alpha * A
        a0 =  1.0 + alpha / A
        a1 = -2.0 * math.cos(w0)
        a2 =  1.0 - alpha / A

        # 周波数応答 H(e^jw) を計算
        w = 2.0 * np.pi * freqs / fs
        ejw = np.exp(-1j * w)
        ejw2 = np.exp(-2j * w)
        H = (b0/a0 + (b1/a0) * ejw + (b2/a0) * ejw2) / \
            (1.0 + (a1/a0) * ejw + (a2/a0) * ejw2)
        total_response += 20.0 * np.log10(np.abs(H) + 1e-10)

    return [(float(f), float(g)) for f, g in zip(freqs, total_response)]
