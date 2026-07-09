"""
eq_engine.py — 3バンドEQエンジン（Phase 5）
自前Biquad IIRフィルタ（numpy only、scipy不要）

バンド構成:
  Low  : ローシェルビング  / 固定100Hz  / ±15dB
  Mid  : ピーキングEQ     / 250Hz〜5kHz可変 / ±15dB / Q=0.5〜4.0
  High : ハイシェルビング / 固定10kHz  / ±15dB

リアルタイム適用方式:
  apply_eq(pcm_float32_stereo) -> np.ndarray
  ※ 入力は shape (N, 2) の float32 配列（-1.0〜+1.0）
"""

import math
import numpy as np
from dataclasses import dataclass, field
from typing import List, Dict, Any


# ---------------------------------------------------------------------------
# EQパラメータ
# ---------------------------------------------------------------------------
@dataclass
class EQParams:
    """1トラック分のEQパラメータ"""
    low_gain_db:  float = 0.0    # ローシェルビング ゲイン（-15〜+15 dB）
    mid_gain_db:  float = 0.0    # ミッドピーキング ゲイン（-15〜+15 dB）
    mid_freq_hz:  float = 1000.0 # ミッド中心周波数（250〜5000 Hz）
    mid_q:        float = 1.0    # ミッドQ値（0.5〜4.0）
    high_gain_db: float = 0.0    # ハイシェルビング ゲイン（-15〜+15 dB）

    LOW_FC:   float = 100.0
    HIGH_FC:  float = 10000.0
    GAIN_MIN: float = -15.0
    GAIN_MAX: float = +15.0
    FREQ_MIN: float = 250.0
    FREQ_MAX: float = 5000.0
    Q_MIN:    float = 0.5
    Q_MAX:    float = 4.0

    def is_flat(self) -> bool:
        """全バンドがフラット（0dB）かどうか"""
        return (
            abs(self.low_gain_db)  < 0.01 and
            abs(self.mid_gain_db)  < 0.01 and
            abs(self.high_gain_db) < 0.01
        )

    def clamp(self):
        """パラメータを有効範囲にクランプ"""
        self.low_gain_db  = max(self.GAIN_MIN, min(self.GAIN_MAX, self.low_gain_db))
        self.mid_gain_db  = max(self.GAIN_MIN, min(self.GAIN_MAX, self.mid_gain_db))
        self.mid_freq_hz  = max(self.FREQ_MIN, min(self.FREQ_MAX, self.mid_freq_hz))
        self.mid_q        = max(self.Q_MIN,    min(self.Q_MAX,    self.mid_q))
        self.high_gain_db = max(self.GAIN_MIN, min(self.GAIN_MAX, self.high_gain_db))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "low_gain_db":  self.low_gain_db,
            "mid_gain_db":  self.mid_gain_db,
            "mid_freq_hz":  self.mid_freq_hz,
            "mid_q":        self.mid_q,
            "high_gain_db": self.high_gain_db,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EQParams":
        p = cls()
        p.low_gain_db  = float(d.get("low_gain_db",  0.0))
        p.mid_gain_db  = float(d.get("mid_gain_db",  0.0))
        p.mid_freq_hz  = float(d.get("mid_freq_hz",  1000.0))
        p.mid_q        = float(d.get("mid_q",        1.0))
        p.high_gain_db = float(d.get("high_gain_db", 0.0))
        p.clamp()
        return p


# ---------------------------------------------------------------------------
# EQプリセット
# ---------------------------------------------------------------------------
EQ_PRESETS: Dict[str, EQParams] = {
    "Flat": EQParams(0.0, 0.0, 1000.0, 1.0, 0.0),
    "Bass Boost": EQParams(
        low_gain_db=10.0, mid_gain_db=0.0, mid_freq_hz=250.0,
        mid_q=1.0, high_gain_db=-2.0
    ),
    "Presence": EQParams(
        low_gain_db=-3.0, mid_gain_db=6.0, mid_freq_hz=3000.0,
        mid_q=1.5, high_gain_db=4.0
    ),
    "Warmth": EQParams(
        low_gain_db=5.0, mid_gain_db=3.0, mid_freq_hz=500.0,
        mid_q=0.8, high_gain_db=-4.0
    ),
    "Brightness": EQParams(
        low_gain_db=-2.0, mid_gain_db=0.0, mid_freq_hz=1000.0,
        mid_q=1.0, high_gain_db=8.0
    ),
    "Vocal": EQParams(
        low_gain_db=-5.0, mid_gain_db=5.0, mid_freq_hz=2000.0,
        mid_q=2.0, high_gain_db=3.0
    ),
    "Kick Drum": EQParams(
        low_gain_db=8.0, mid_gain_db=-4.0, mid_freq_hz=400.0,
        mid_q=1.0, high_gain_db=2.0
    ),
    "Hi-Hat": EQParams(
        low_gain_db=-10.0, mid_gain_db=-2.0, mid_freq_hz=2000.0,
        mid_q=1.0, high_gain_db=8.0
    ),
}


# ---------------------------------------------------------------------------
# Biquadフィルタ係数計算（Audio EQ Cookbook準拠）
# ---------------------------------------------------------------------------

def _low_shelf_sos(fc: float, gain_db: float, sr: float) -> np.ndarray:
    """ローシェルビングフィルタ SOS係数 shape=(1,6)"""
    A  = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * fc / sr
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    S = 1.0  # shelf slope
    alpha = sin_w0 / 2.0 * math.sqrt((A + 1.0/A) * (1.0/S - 1.0) + 2.0)
    sqA = math.sqrt(A)
    b0 =    A * ((A+1) - (A-1)*cos_w0 + 2*sqA*alpha)
    b1 =  2*A * ((A-1) - (A+1)*cos_w0)
    b2 =    A * ((A+1) - (A-1)*cos_w0 - 2*sqA*alpha)
    a0 =         (A+1) + (A-1)*cos_w0 + 2*sqA*alpha
    a1 =    -2 * ((A-1) + (A+1)*cos_w0)
    a2 =         (A+1) + (A-1)*cos_w0 - 2*sqA*alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _high_shelf_sos(fc: float, gain_db: float, sr: float) -> np.ndarray:
    """ハイシェルビングフィルタ SOS係数 shape=(1,6)"""
    A  = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * fc / sr
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    S = 1.0
    alpha = sin_w0 / 2.0 * math.sqrt((A + 1.0/A) * (1.0/S - 1.0) + 2.0)
    sqA = math.sqrt(A)
    b0 =    A * ((A+1) + (A-1)*cos_w0 + 2*sqA*alpha)
    b1 = -2*A * ((A-1) + (A+1)*cos_w0)
    b2 =    A * ((A+1) + (A-1)*cos_w0 - 2*sqA*alpha)
    a0 =         (A+1) - (A-1)*cos_w0 + 2*sqA*alpha
    a1 =     2 * ((A-1) - (A+1)*cos_w0)
    a2 =         (A+1) - (A-1)*cos_w0 - 2*sqA*alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _peak_eq_sos(fc: float, gain_db: float, Q: float, sr: float) -> np.ndarray:
    """ピーキングEQフィルタ SOS係数 shape=(1,6)"""
    A  = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * fc / sr
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * Q)
    b0 =  1.0 + alpha * A
    b1 = -2.0 * cos_w0
    b2 =  1.0 - alpha * A
    a0 =  1.0 + alpha / A
    a1 = -2.0 * cos_w0
    a2 =  1.0 - alpha / A
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _sosfilt(sos: np.ndarray, x: np.ndarray) -> np.ndarray:
    """
    SOS形式のIIRフィルタを適用する（scipy不要・numpy vectorized実装）。
    Direct Form II Transposed をnumpyで実装。
    sos: shape (n_sections, 6)  [b0, b1, b2, 1, a1, a2]
    x:   shape (N,) float64
    """
    y = x.astype(np.float64)
    for section in sos:
        b0, b1, b2, _, a1, a2 = section
        # Direct Form II Transposed: サンプル単位ループをnumpyで高速化
        # 再帰的な依存があるため完全ベクトル化は不可だが
        # numpy配列アクセスでPythonオーバーヘッドを削減
        N = len(y)
        out = np.empty(N, dtype=np.float64)
        z1, z2 = 0.0, 0.0
        # numpy配列として一括読み込み（メモリアクセス高速化）
        y_arr = y
        for i in range(N):
            xi = y_arr[i]
            yi = b0 * xi + z1
            z1 = b1 * xi - a1 * yi + z2
            z2 = b2 * xi - a2 * yi
            out[i] = yi
        y = out
    return y


def _sosfilt_scipy_fallback(sos: np.ndarray, x: np.ndarray) -> np.ndarray:
    """
    scipy.signal.sosfilt が利用可能な場合はそちらを使う高速版。
    利用不可の場合は _sosfilt にフォールバック。
    """
    try:
        from scipy.signal import sosfilt as scipy_sosfilt
        return scipy_sosfilt(sos, x.astype(np.float64))
    except ImportError:
        return _sosfilt(sos, x)


# ---------------------------------------------------------------------------
# EQエンジン
# ---------------------------------------------------------------------------

class EQEngine:
    """
    1トラック分のEQ処理エンジン。
    apply_eq() で numpy float32 stereo バッファにEQを適用する。
    """

    def __init__(self, sample_rate: int = 44100):
        self._sr = sample_rate
        self._params = EQParams()

    @property
    def params(self) -> EQParams:
        return self._params

    def set_params(self, params: EQParams):
        params.clamp()
        self._params = params

    def apply_eq(self, pcm: np.ndarray) -> np.ndarray:
        """
        EQを適用する。
        pcm: shape (N, 2) float32 stereo, range -1.0〜+1.0
        戻り値: 同じ shape の float32 配列
        """
        if self._params.is_flat():
            return pcm  # フラット時はバイパス

        p = self._params
        result = pcm.astype(np.float64)

        # 各チャンネルに独立してフィルタ適用
        for ch in range(result.shape[1]):
            sig = result[:, ch]

            # Low shelf
            if abs(p.low_gain_db) > 0.01:
                sos = _low_shelf_sos(p.LOW_FC, p.low_gain_db, self._sr)
                sig = _sosfilt_scipy_fallback(sos, sig)

            # Mid peak
            if abs(p.mid_gain_db) > 0.01:
                sos = _peak_eq_sos(p.mid_freq_hz, p.mid_gain_db, p.mid_q, self._sr)
                sig = _sosfilt_scipy_fallback(sos, sig)

            # High shelf
            if abs(p.high_gain_db) > 0.01:
                sos = _high_shelf_sos(p.HIGH_FC, p.high_gain_db, self._sr)
                sig = _sosfilt_scipy_fallback(sos, sig)

            result[:, ch] = sig

        return result.astype(np.float32)

    def apply_eq_mono(self, pcm_mono: np.ndarray) -> np.ndarray:
        """
        モノラル信号にEQを適用する。
        pcm_mono: shape (N,) float32
        戻り値: shape (N,) float32
        """
        if self._params.is_flat():
            return pcm_mono

        p = self._params
        sig = pcm_mono.astype(np.float64)

        if abs(p.low_gain_db) > 0.01:
            sos = _low_shelf_sos(p.LOW_FC, p.low_gain_db, self._sr)
            sig = _sosfilt_scipy_fallback(sos, sig)

        if abs(p.mid_gain_db) > 0.01:
            sos = _peak_eq_sos(p.mid_freq_hz, p.mid_gain_db, p.mid_q, self._sr)
            sig = _sosfilt_scipy_fallback(sos, sig)

        if abs(p.high_gain_db) > 0.01:
            sos = _high_shelf_sos(p.HIGH_FC, p.high_gain_db, self._sr)
            sig = _sosfilt_scipy_fallback(sos, sig)

        return sig.astype(np.float32)


# ---------------------------------------------------------------------------
# EQカーブ表示用：周波数特性計算
# ---------------------------------------------------------------------------

def get_response_db(params: EQParams, sr: int = 44100, n_points: int = 200) -> list:
    """
    EQParamsから周波数特性カーブを計算する。
    戻り値: [(freq_hz, gain_db), ...] のリスト（20Hz〜20kHz、対数スケール）
    フラット時は全点 0.0dB を返す。
    """
    if params.is_flat():
        freqs = np.logspace(np.log10(20), np.log10(20000), n_points)
        return [(float(f), 0.0) for f in freqs]

    freqs = np.logspace(np.log10(20), np.log10(20000), n_points)
    total_db = np.zeros(n_points)

    def _freq_response_db(sos: np.ndarray) -> np.ndarray:
        """SOS係数から各周波数のゲイン(dB)を計算する。"""
        b0, b1, b2, _, a1, a2 = sos[0]
        w = 2.0 * np.pi * freqs / sr
        z = np.exp(1j * w)
        H = (b0 + b1 / z + b2 / (z ** 2)) / (1.0 + a1 / z + a2 / (z ** 2))
        return 20.0 * np.log10(np.maximum(np.abs(H), 1e-10))

    p = params
    if abs(p.low_gain_db) > 0.01:
        total_db += _freq_response_db(_low_shelf_sos(p.LOW_FC, p.low_gain_db, sr))
    if abs(p.mid_gain_db) > 0.01:
        total_db += _freq_response_db(_peak_eq_sos(p.mid_freq_hz, p.mid_gain_db, p.mid_q, sr))
    if abs(p.high_gain_db) > 0.01:
        total_db += _freq_response_db(_high_shelf_sos(p.HIGH_FC, p.high_gain_db, sr))

    return [(float(f), float(g)) for f, g in zip(freqs, total_db)]
