"""
eq_engine.py — 3バンドEQエンジン（Phase 5 / Phase 12改）
自前Biquad IIRフィルタ（numpy only、scipy不要）

バンド構成:
  Low  : ローシェルビング  / 固定100Hz  / ±15dB
  Mid  : ピーキングEQ     / 250Hz〜5kHz可変 / ±15dB / Q=0.5〜4.0
  High : ハイシェルビング / 固定10kHz  / ±15dB

リアルタイム適用方式:
  apply_eq(pcm_float32_stereo) -> np.ndarray
  ※ 入力は shape (N, 2) の float32 配列（-1.0〜+1.0）

Phase 12改: ステートフル化（チャンク間フィルタ状態保持）
  - 各バンド・各チャンネルの IIR フィルタ状態（z1, z2）をチャンク間で保持
  - EQパラメータ変更時（プリセット切り替え時）に20msクロスフェードで滑らかに遷移
  - reset_state() メソッドを追加（トラック再生開始時に呼ぶ）
"""

import math
import numpy as np
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


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


def _sosfilt_stateful(sos: np.ndarray, x: np.ndarray,
                      zi: np.ndarray) -> tuple:
    """
    SOS形式のIIRフィルタを適用する（ステートフル版）。
    Direct Form II Transposed をnumpyで実装。

    sos: shape (n_sections, 6)  [b0, b1, b2, 1, a1, a2]
    x:   shape (N,) float64
    zi:  shape (n_sections, 2) float64  各セクションの [z1, z2] 状態

    戻り値: (y, zo)
      y:  shape (N,) float64  フィルタ出力
      zo: shape (n_sections, 2) float64  更新後の状態
    """
    y = x.astype(np.float64)
    n_sections = sos.shape[0]
    zo = np.zeros((n_sections, 2), dtype=np.float64)

    for s_idx in range(n_sections):
        b0, b1, b2, _, a1, a2 = sos[s_idx]
        z1 = float(zi[s_idx, 0])
        z2 = float(zi[s_idx, 1])
        N = len(y)
        out = np.empty(N, dtype=np.float64)
        y_arr = y
        for i in range(N):
            xi = y_arr[i]
            yi = b0 * xi + z1
            z1 = b1 * xi - a1 * yi + z2
            z2 = b2 * xi - a2 * yi
            out[i] = yi
        zo[s_idx, 0] = z1
        zo[s_idx, 1] = z2
        y = out

    return y, zo


def _sosfilt_steady_state_zi(sos: np.ndarray, x0: float) -> np.ndarray:
    """
    Direct Form II Transposed の定常状態初期値を計算する。
    定常入力 x[n] = x0 に対する定常状態の z1, z2 を返す。
    sos: shape (n_sections, 6)
    戻り値: shape (n_sections, 2)  各セクションの [z1_ss, z2_ss]
    """
    n_sections = sos.shape[0]
    zi = np.zeros((n_sections, 2), dtype=np.float64)
    cur_x0 = x0
    for s_idx in range(n_sections):
        b0, b1, b2, _, a1, a2 = sos[s_idx]
        denom = 1.0 + a1 + a2
        if abs(denom) < 1e-12:
            # 不安定なフィルタ：ゼロ初期化
            zi[s_idx] = 0.0
            cur_x0 = cur_x0 * (b0 + b1 + b2)
        else:
            y0 = cur_x0 * (b0 + b1 + b2) / denom
            z2_ss = b2 * cur_x0 - a2 * y0
            z1_ss = b1 * cur_x0 - a1 * y0 + z2_ss
            zi[s_idx, 0] = z1_ss
            zi[s_idx, 1] = z2_ss
            cur_x0 = y0  # 次のセクションの入力はこのセクションの出力
    return zi


def _sosfilt_scipy_stateful(sos: np.ndarray, x: np.ndarray,
                             zi: np.ndarray) -> tuple:
    """
    scipy.signal.sosfilt が利用可能な場合はそちらを使う高速版（ステートフル）。
    利用不可の場合は _sosfilt_stateful にフォールバック。

    zi: shape (n_sections, 2)  各セクションの [z1, z2] 状態
    戻り値: (y, zo)
    """
    try:
        from scipy.signal import sosfilt as scipy_sosfilt
        # scipy の sosfilt は zi shape (n_sections, 2) を受け付ける
        y, zo = scipy_sosfilt(sos, x.astype(np.float64), zi=zi)
        return y, zo
    except ImportError:
        return _sosfilt_stateful(sos, x, zi)


# ---------------------------------------------------------------------------
# EQエンジン（ステートフル版）
# ---------------------------------------------------------------------------

class EQEngine:
    """
    1トラック分のEQ処理エンジン（ステートフル版）。
    apply_eq() で numpy float32 stereo バッファにEQを適用する。

    Phase 12改:
    - 各バンド・各チャンネルのフィルタ状態（z1, z2）をチャンク間で保持
    - EQパラメータ変更時（プリセット切り替え時）に20msクロスフェードで遷移
    """

    # クロスフェード長（サンプル数）。20ms相当
    CROSSFADE_SAMPLES = 882  # 44100 * 0.02

    # フィルタバンド数（Low / Mid / High の3バンド）
    N_BANDS = 3
    # ステレオチャンネル数
    N_CH = 2

    def __init__(self, sample_rate: int = 44100):
        self._sr = sample_rate
        self._params = EQParams()

        # 新パラメータ用フィルタ状態: shape (N_BANDS, N_CH, 1, 2)
        self._filter_zi = np.zeros((self.N_BANDS, self.N_CH, 1, 2), dtype=np.float64)

        # クロスフェード用：旧パラメータと旧フィルタ状態を保持
        self._old_params: Optional[EQParams] = None
        self._old_filter_zi = np.zeros((self.N_BANDS, self.N_CH, 1, 2), dtype=np.float64)

        # クロスフェード状態
        self._crossfade_pos: int = 0  # クロスフェード残りサンプル数（0=完了）

    @property
    def params(self) -> EQParams:
        return self._params

    def set_params(self, params: EQParams):
        """イコライザーパラメータを変更する。パラメータが変化した場合はクロスフェードを開始する。"""
        params.clamp()
        # パラメータが実質的に変化している場合のみクロスフェード開始
        old = self._params
        changed = (
            abs(old.low_gain_db  - params.low_gain_db)  > 0.01 or
            abs(old.mid_gain_db  - params.mid_gain_db)  > 0.01 or
            abs(old.mid_freq_hz  - params.mid_freq_hz)  > 1.0  or
            abs(old.mid_q        - params.mid_q)        > 0.01 or
            abs(old.high_gain_db - params.high_gain_db) > 0.01
        )
        if changed:
            # 旧パラメータと旧フィルタ状態を保存（クロスフェード中に旧EQで処理するため）
            self._old_params = old
            self._old_filter_zi = self._filter_zi.copy()
            self._crossfade_pos = self.CROSSFADE_SAMPLES
            # 新パラメータ用フィルタ状態を「旧出力の最後値」で定常状態初期化
            # これにより新EQフィルタの過渡応答による音途切れを防ぐ
            self._init_new_filter_zi(params)
        self._params = params

    def _init_new_filter_zi(self, params: EQParams):
        """新EQフィルタの初期状態を定常状態で初期化する。
        旧フィルタの最後出力値を定常入力と仮定して計算する。"""
        p = params
        self._filter_zi[:] = 0.0
        if p.is_flat():
            return
        # 旧フィルタの最後出力値を定常入力として新フィルタの定常状態を計算
        # _old_filter_ziの最後状態から推定するのは複雑なので、
        # 「現在のフィルタ出力の最後値」を定常入力として使用する
        # 旧フィルタの最後状態 z1[0,ch] から出力値を推定
        for ch in range(self.N_CH):
            # 旧EQの最後出力値を推定：旧フィルタの z1 が「次の入力に対する待機値」
            # Direct Form II Transposed: y[n] = b0*x[n] + z1[n-1]
            # z1 は「前回の入力に対する待機値」なので、
            # 最後出力値 ≈ _old_filter_zi[0, ch, 0, 0] + b0 * 最後入力
            # 簡単化：旧フィルタの最後出力値を 0.0 と仮定して定常状態初期化
            # （実際の最後出力値はクロスフェードにより补完される）
            if abs(p.low_gain_db) > 0.01:
                sos = _low_shelf_sos(p.LOW_FC, p.low_gain_db, self._sr)
                # 旧フィルタの最後出力値を定常入力として使用
                # z1[0,ch,0,0] は「前回の入力に対する待機値」なので
                # 最後出力値 ≈ z1 として定常状態を計算
                x0 = float(self._old_filter_zi[0, ch, 0, 0])
                zi_ss = _sosfilt_steady_state_zi(sos, x0)
                self._filter_zi[0, ch] = zi_ss

            if abs(p.mid_gain_db) > 0.01:
                sos = _peak_eq_sos(p.mid_freq_hz, p.mid_gain_db, p.mid_q, self._sr)
                x0 = float(self._old_filter_zi[1, ch, 0, 0])
                zi_ss = _sosfilt_steady_state_zi(sos, x0)
                self._filter_zi[1, ch] = zi_ss

            if abs(p.high_gain_db) > 0.01:
                sos = _high_shelf_sos(p.HIGH_FC, p.high_gain_db, self._sr)
                x0 = float(self._old_filter_zi[2, ch, 0, 0])
                zi_ss = _sosfilt_steady_state_zi(sos, x0)
                self._filter_zi[2, ch] = zi_ss

    def reset_state(self):
        """内部バッファをリセットする（トラック再生開始時に呼ぶ）。"""
        self._filter_zi[:] = 0.0
        self._old_filter_zi[:] = 0.0
        self._old_params = None
        self._crossfade_pos = 0

    def apply_eq(self, pcm: np.ndarray) -> np.ndarray:
        """
        EQを適用する（ステートフル版）。
        pcm: shape (N, 2) float32 stereo, range -1.0～+1.0
        戻り値: 同じ shape の float32 配列
        """
        if self._params.is_flat() and self._crossfade_pos == 0:
            # フラットかつクロスフェード不要はバイパス
            return pcm

        # クロスフェード処理：旧EQ出力と新EQ出力をブレンド
        if self._crossfade_pos > 0 and self._old_params is not None:
            n = len(pcm)
            fade_len = min(self._crossfade_pos, n)
            cf_start = self.CROSSFADE_SAMPLES - self._crossfade_pos

            # 旧パラメータで実際に処理した出力（フェードアウト側）
            old_out = self._apply_eq_with_params(
                pcm[:fade_len], self._old_params, self._old_filter_zi
            )
            # 新パラメータでEQを適用（フェードイン側）
            out = self._apply_eq_stateful(pcm)

            fade_in = np.linspace(
                cf_start / self.CROSSFADE_SAMPLES,
                (cf_start + fade_len) / self.CROSSFADE_SAMPLES,
                fade_len, dtype=np.float32
            )
            fade_in = np.clip(fade_in, 0.0, 1.0)
            fade_out_w = 1.0 - fade_in

            # 旧EQ出力 × fade_out + 新EQ出力 × fade_in
            out[:fade_len, 0] = old_out[:, 0] * fade_out_w + out[:fade_len, 0] * fade_in
            out[:fade_len, 1] = old_out[:, 1] * fade_out_w + out[:fade_len, 1] * fade_in

            self._crossfade_pos = max(0, self._crossfade_pos - n)
            if self._crossfade_pos == 0:
                self._old_params = None

            return out

        # 通常処理（クロスフェードなし）
        return self._apply_eq_stateful(pcm)

    def _apply_eq_stateful(self, pcm: np.ndarray) -> np.ndarray:
        """
        フィルタ状態を保持しながらEQを適用する（内部用）。
        pcm: shape (N, 2) float32
        戻り値: shape (N, 2) float32
        """
        if self._params.is_flat():
            return pcm.astype(np.float32)

        p = self._params
        result = pcm.astype(np.float64)

        # 各チャンネルに独立してフィルタ適用
        for ch in range(result.shape[1]):
            sig = result[:, ch]

            # Low shelf (band_idx=0)
            if abs(p.low_gain_db) > 0.01:
                sos = _low_shelf_sos(p.LOW_FC, p.low_gain_db, self._sr)
                zi = self._filter_zi[0, ch]  # shape (1, 2)
                sig, zo = _sosfilt_scipy_stateful(sos, sig, zi)
                self._filter_zi[0, ch] = zo

            # Mid peak (band_idx=1)
            if abs(p.mid_gain_db) > 0.01:
                sos = _peak_eq_sos(p.mid_freq_hz, p.mid_gain_db, p.mid_q, self._sr)
                zi = self._filter_zi[1, ch]  # shape (1, 2)
                sig, zo = _sosfilt_scipy_stateful(sos, sig, zi)
                self._filter_zi[1, ch] = zo

            # High shelf (band_idx=2)
            if abs(p.high_gain_db) > 0.01:
                sos = _high_shelf_sos(p.HIGH_FC, p.high_gain_db, self._sr)
                zi = self._filter_zi[2, ch]  # shape (1, 2)
                sig, zo = _sosfilt_scipy_stateful(sos, sig, zi)
                self._filter_zi[2, ch] = zo

            result[:, ch] = sig

        return result.astype(np.float32)

    def _apply_eq_with_params(self, pcm: np.ndarray, params: EQParams,
                               zi: np.ndarray) -> np.ndarray:
        """
        指定パラメータとフィルタ状態でEQを適用する（クロスフェード旧EQ用）。
        ziは内部で更新される（旧フィルタ状態を進行させる）。
        pcm: shape (N, 2) float32
        戻り値: shape (N, 2) float32
        """
        if params.is_flat():
            return pcm.astype(np.float32)

        p = params
        result = pcm.astype(np.float64)

        for ch in range(result.shape[1]):
            sig = result[:, ch]

            if abs(p.low_gain_db) > 0.01:
                sos = _low_shelf_sos(p.LOW_FC, p.low_gain_db, self._sr)
                sig, zo = _sosfilt_scipy_stateful(sos, sig, zi[0, ch])
                zi[0, ch] = zo

            if abs(p.mid_gain_db) > 0.01:
                sos = _peak_eq_sos(p.mid_freq_hz, p.mid_gain_db, p.mid_q, self._sr)
                sig, zo = _sosfilt_scipy_stateful(sos, sig, zi[1, ch])
                zi[1, ch] = zo

            if abs(p.high_gain_db) > 0.01:
                sos = _high_shelf_sos(p.HIGH_FC, p.high_gain_db, self._sr)
                sig, zo = _sosfilt_scipy_stateful(sos, sig, zi[2, ch])
                zi[2, ch] = zo

            result[:, ch] = sig

        return result.astype(np.float32)

    def apply_eq_mono(self, pcm_mono: np.ndarray) -> np.ndarray:
        """
        モノラル信号にEQを適用する（ステートフル版）。
        pcm_mono: shape (N,) float32
        戻り値: shape (N,) float32
        """
        if self._params.is_flat():
            return pcm_mono

        p = self._params
        sig = pcm_mono.astype(np.float64)

        # モノラル用フィルタ状態（ch=0 を流用）
        if abs(p.low_gain_db) > 0.01:
            sos = _low_shelf_sos(p.LOW_FC, p.low_gain_db, self._sr)
            zi = self._filter_zi[0, 0]
            sig, zo = _sosfilt_scipy_stateful(sos, sig, zi)
            self._filter_zi[0, 0] = zo

        if abs(p.mid_gain_db) > 0.01:
            sos = _peak_eq_sos(p.mid_freq_hz, p.mid_gain_db, p.mid_q, self._sr)
            zi = self._filter_zi[1, 0]
            sig, zo = _sosfilt_scipy_stateful(sos, sig, zi)
            self._filter_zi[1, 0] = zo

        if abs(p.high_gain_db) > 0.01:
            sos = _high_shelf_sos(p.HIGH_FC, p.high_gain_db, self._sr)
            zi = self._filter_zi[2, 0]
            sig, zo = _sosfilt_scipy_stateful(sos, sig, zi)
            self._filter_zi[2, 0] = zo

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
