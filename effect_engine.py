"""
effect_engine.py — プリセットエフェクターエンジン（Phase 6）
numpy only（scipy不要）で動作する6種エフェクト実装。
全DSP処理をnumpyベクトル演算で実装し、Pythonループを排除して高速化。

エフェクト一覧:
  Reverb      : 簡易FDN（コムフィルタ + オールパスフィルタ）
  Delay       : フィードバックディレイ
  Compressor  : RMSベースのダイナミクスコンプレッサー
  Distortion  : tanhソフトクリッピング
  Chorus      : LFO変調ディレイ（線形補間）
  Limiter     : ハードリミッター（ブリックウォール）

使い方:
  engine = EffectEngine(sample_rate=44100)
  out = engine.apply(pcm_float32_stereo, preset_name)
  # pcm_float32_stereo: shape (N, 2), dtype float32, range -1.0〜+1.0
"""

import math
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, Any


# ---------------------------------------------------------------------------
# エフェクトプリセット定義
# ---------------------------------------------------------------------------

@dataclass
class EffectPreset:
    """1エフェクトプリセットのパラメータ定義"""
    name: str
    effect_type: str          # "reverb" / "delay" / "compressor" / "distortion" / "chorus" / "limiter"
    params: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "effect_type": self.effect_type,
            "params": dict(self.params),
        }


# プリセットカタログ（表示名 -> EffectPreset）
EFFECT_PRESETS: Dict[str, EffectPreset] = {
    "None": EffectPreset("None", "bypass", {}),

    # --- Reverb ---
    "Reverb: Room": EffectPreset("Reverb: Room", "reverb", {
        "decay": 0.4, "mix": 0.30,
    }),
    "Reverb: Hall": EffectPreset("Reverb: Hall", "reverb", {
        "decay": 0.7, "mix": 0.40,
    }),
    "Reverb: Plate": EffectPreset("Reverb: Plate", "reverb", {
        "decay": 0.55, "mix": 0.35,
    }),

    # --- Delay ---
    "Delay: Short": EffectPreset("Delay: Short", "delay", {
        "delay_ms": 120.0, "feedback": 0.35, "mix": 0.30,
    }),
    "Delay: Long": EffectPreset("Delay: Long", "delay", {
        "delay_ms": 350.0, "feedback": 0.50, "mix": 0.35,
    }),
    "Delay: Slap": EffectPreset("Delay: Slap", "delay", {
        "delay_ms": 60.0, "feedback": 0.15, "mix": 0.25,
    }),

    # --- Compressor ---
    "Comp: Gentle": EffectPreset("Comp: Gentle", "compressor", {
        "threshold_db": -18.0, "ratio": 2.0, "attack_ms": 20.0, "release_ms": 200.0,
    }),
    "Comp: Medium": EffectPreset("Comp: Medium", "compressor", {
        "threshold_db": -24.0, "ratio": 4.0, "attack_ms": 10.0, "release_ms": 100.0,
    }),
    "Comp: Hard": EffectPreset("Comp: Hard", "compressor", {
        "threshold_db": -30.0, "ratio": 8.0, "attack_ms": 5.0, "release_ms": 50.0,
    }),

    # --- Distortion ---
    "Dist: Soft": EffectPreset("Dist: Soft", "distortion", {
        "drive": 2.0, "mix": 0.40,
    }),
    "Dist: Medium": EffectPreset("Dist: Medium", "distortion", {
        "drive": 5.0, "mix": 0.60,
    }),
    "Dist: Heavy": EffectPreset("Dist: Heavy", "distortion", {
        "drive": 12.0, "mix": 0.80,
    }),

    # --- Chorus ---
    "Chorus: Light": EffectPreset("Chorus: Light", "chorus", {
        "rate_hz": 0.8, "depth_ms": 5.0, "mix": 0.30,
    }),
    "Chorus: Deep": EffectPreset("Chorus: Deep", "chorus", {
        "rate_hz": 1.5, "depth_ms": 12.0, "mix": 0.50,
    }),
    "Chorus: Flanger": EffectPreset("Chorus: Flanger", "chorus", {
        "rate_hz": 0.4, "depth_ms": 3.0, "mix": 0.60,
    }),

    # --- Limiter ---
    "Limiter: -3dB": EffectPreset("Limiter: -3dB", "limiter", {
        "ceiling_db": -3.0,
    }),
    "Limiter: -6dB": EffectPreset("Limiter: -6dB", "limiter", {
        "ceiling_db": -6.0,
    }),
    "Limiter: -1dB": EffectPreset("Limiter: -1dB", "limiter", {
        "ceiling_db": -1.0,
    }),
}

# カテゴリ別グループ（UI表示用）
EFFECT_CATEGORIES = {
    "None":        ["None"],
    "Reverb":      ["Reverb: Room", "Reverb: Hall", "Reverb: Plate"],
    "Delay":       ["Delay: Short", "Delay: Long", "Delay: Slap"],
    "Compressor":  ["Comp: Gentle", "Comp: Medium", "Comp: Hard"],
    "Distortion":  ["Dist: Soft", "Dist: Medium", "Dist: Heavy"],
    "Chorus":      ["Chorus: Light", "Chorus: Deep", "Chorus: Flanger"],
    "Limiter":     ["Limiter: -3dB", "Limiter: -6dB", "Limiter: -1dB"],
}


# ---------------------------------------------------------------------------
# EffectEngine: 各エフェクトのDSP処理（numpy vectorized）
# ---------------------------------------------------------------------------

class EffectEngine:
    """
    プリセットエフェクターエンジン。
    全処理をnumpyベクトル演算で実装し、Pythonループを排除して高速化。
    """

    def __init__(self, sample_rate: int = 44100):
        self._sr = sample_rate

    def apply(self, pcm: np.ndarray, preset_name: str) -> np.ndarray:
        """
        PCMバッファにエフェクトを適用して返す。
        pcm: shape (N, 2) float32, range -1.0〜+1.0
        戻り値: 同形状のfloat32配列
        """
        if preset_name not in EFFECT_PRESETS:
            return pcm
        preset = EFFECT_PRESETS[preset_name]
        if preset.effect_type == "bypass":
            return pcm

        p = preset.params
        if preset.effect_type == "reverb":
            return self._apply_reverb(pcm, p.get("decay", 0.5), p.get("mix", 0.3))
        elif preset.effect_type == "delay":
            return self._apply_delay(pcm, p.get("delay_ms", 200.0),
                                     p.get("feedback", 0.4), p.get("mix", 0.3))
        elif preset.effect_type == "compressor":
            return self._apply_compressor(pcm,
                                          p.get("threshold_db", -20.0),
                                          p.get("ratio", 4.0),
                                          p.get("attack_ms", 10.0),
                                          p.get("release_ms", 100.0))
        elif preset.effect_type == "distortion":
            return self._apply_distortion(pcm, p.get("drive", 4.0), p.get("mix", 0.5))
        elif preset.effect_type == "chorus":
            return self._apply_chorus(pcm, p.get("rate_hz", 1.0),
                                      p.get("depth_ms", 8.0), p.get("mix", 0.4))
        elif preset.effect_type == "limiter":
            return self._apply_limiter(pcm, p.get("ceiling_db", -3.0))
        return pcm

    # ------------------------------------------------------------------
    # Reverb: FFT畳み込みによる高速リバーブ
    # ------------------------------------------------------------------
    def _apply_reverb(self, pcm: np.ndarray, decay: float, mix: float) -> np.ndarray:
        """
        簡易リバーブ（FFT畳み込み版）。
        コムフィルタのインパルス応答を生成し、np.fft.rfftで高速畳み込み。
        """
        n_samples = len(pcm)
        mono = pcm.mean(axis=1).astype(np.float32)

        comb_delays_ms = [29.7, 37.1, 41.1, 43.7]
        reverb_mono = np.zeros(n_samples, dtype=np.float32)

        for d_ms in comb_delays_ms:
            d_samp = int(d_ms * self._sr / 1000)
            if d_samp < 1:
                continue

            # コムフィルタのインパルス応答を直接構築（反韻の重ね合わせ）
            # 最大反韻回数を制限して高速化（-40dB以下で打ち切り）
            if decay >= 1.0:
                n_echoes = 20
            else:
                n_echoes = min(int(math.log(1e-2) / math.log(max(decay, 1e-9))), 40)

            # インパルス応答 h[k*d_samp] = decay^k
            ir_len = d_samp * n_echoes + 1
            ir = np.zeros(ir_len, dtype=np.float32)
            ir[0] = 1.0
            coef = 1.0
            for k in range(1, n_echoes + 1):
                offset = d_samp * k
                if offset >= ir_len:
                    break
                coef *= decay
                ir[offset] = coef

            # FFT畳み込み（線形畳み込み）
            conv_len = n_samples + ir_len - 1
            fft_size = 1 << int(math.ceil(math.log2(conv_len)))  # 次の2のべき乗
            X = np.fft.rfft(mono, n=fft_size)
            H = np.fft.rfft(ir, n=fft_size)
            comb_out = np.fft.irfft(X * H, n=fft_size)[:n_samples].astype(np.float32)
            reverb_mono += comb_out * 0.25

        # オールパスフィルタ（短い遅延なのでインパルス応答が短く、FFT畳み込みが効果的）
        allpass_delays_ms = [5.0, 1.7]
        g = 0.7
        for d_ms in allpass_delays_ms:
            d_samp = int(d_ms * self._sr / 1000)
            if d_samp < 1:
                continue
            # オールパス近似: 入力 + 遅延信号の加算
            ap_out = reverb_mono.copy() * (-g)
            ap_out[d_samp:] += reverb_mono[:n_samples - d_samp]
            ap_out[d_samp:] += ap_out[:n_samples - d_samp] * g
            reverb_mono = ap_out

        wet = np.stack([reverb_mono, reverb_mono], axis=1).astype(np.float32)
        return ((1.0 - mix) * pcm + mix * wet).astype(np.float32)

    # ------------------------------------------------------------------
    # Delay: numpy vectorized フィードバックディレイ
    # ------------------------------------------------------------------
    def _apply_delay(self, pcm: np.ndarray, delay_ms: float,
                     feedback: float, mix: float) -> np.ndarray:
        """
        フィードバックディレイ（numpy vectorized版）。
        フィードバックの反響を指数減衰の重ね合わせで計算。
        """
        delay_samp = int(delay_ms * self._sr / 1000)
        if delay_samp < 1:
            return pcm

        n_samples = len(pcm)
        feedback = max(0.0, min(0.9, feedback))

        wet = np.zeros_like(pcm)

        # フィードバック反響の最大回数（エネルギーが-60dB以下になるまで）
        if feedback <= 0.0:
            n_echoes = 0
        elif feedback >= 1.0:
            n_echoes = 50
        else:
            n_echoes = min(int(math.log(1e-3) / math.log(feedback)), 200)

        coef = 1.0
        for k in range(1, n_echoes + 1):
            offset = delay_samp * k
            if offset >= n_samples:
                break
            coef *= feedback
            wet[offset:] += pcm[:n_samples - offset] * coef

        return ((1.0 - mix) * pcm + mix * wet).astype(np.float32)

    # ------------------------------------------------------------------
    # Compressor: scipy.signal.lfilterによる高速エンベロープフォロワー
    # ------------------------------------------------------------------
    def _apply_compressor(self, pcm: np.ndarray, threshold_db: float,
                          ratio: float, attack_ms: float, release_ms: float) -> np.ndarray:
        """
        RMSベースのコンプレッサー。
        scipy利用可能な場合はlfilterで高速化、不可の場合はnumpyフォールバック。
        """
        threshold_lin = 10 ** (threshold_db / 20.0)

        n_samples = len(pcm)
        level = np.abs(pcm).mean(axis=1).astype(np.float32)

        # エンベロープフォロワー：ダウンサンプリングで高速化
        # コンプレッサーのアタック/リリースは通常数十ms単位なので、
        # 1/8ダウンサンプリングしてエンベロープ計算、その後アップサンプリング
        DS = 8  # ダウンサンプリング率
        sr_ds = self._sr / DS
        attack_coef  = math.exp(-1.0 / max(attack_ms  * sr_ds / 1000.0, 1.0))
        release_coef = math.exp(-1.0 / max(release_ms * sr_ds / 1000.0, 1.0))

        # ダウンサンプリング: 各DSサンプルの最大値を取る
        n_ds = n_samples // DS
        level_ds = level[:n_ds * DS].reshape(n_ds, DS).max(axis=1).astype(np.float64)

        # エンベロープフォロワー（ダウンサンプリング済み信号に対してループ）
        env_ds = np.zeros(n_ds, dtype=np.float64)
        e = 0.0
        for i in range(n_ds):
            lv = level_ds[i]
            coef = attack_coef if lv > e else release_coef
            e = coef * e + (1.0 - coef) * lv
            env_ds[i] = e

        # アップサンプリング（線形補間）
        x_ds = np.arange(n_ds)
        x_full = np.linspace(0, n_ds - 1, n_samples)
        env = np.interp(x_full, x_ds, env_ds).astype(np.float64)

        # ゲインリダクション（numpy vectorized）
        gain = np.ones(n_samples, dtype=np.float64)
        mask = env > threshold_lin
        if mask.any():
            gain[mask] = (threshold_lin / np.maximum(env[mask], 1e-9)) ** (1.0 - 1.0 / max(ratio, 1.0))

        out = pcm.astype(np.float64)
        out[:, 0] *= gain
        out[:, 1] *= gain
        return out.astype(np.float32)

    # ------------------------------------------------------------------
    # Distortion: tanhソフトクリッピング（完全vectorized）
    # ------------------------------------------------------------------
    def _apply_distortion(self, pcm: np.ndarray, drive: float, mix: float) -> np.ndarray:
        """
        tanhソフトクリッピングによるディストーション（完全numpy vectorized）。
        """
        drive = max(1.0, drive)
        wet = np.tanh(pcm * drive) / math.tanh(drive)
        return ((1.0 - mix) * pcm + mix * wet).astype(np.float32)

    # ------------------------------------------------------------------
    # Chorus: numpy vectorized LFO変調ディレイ（線形補間）
    # ------------------------------------------------------------------
    def _apply_chorus(self, pcm: np.ndarray, rate_hz: float,
                      depth_ms: float, mix: float) -> np.ndarray:
        """
        LFO変調ディレイによるコーラス（numpy vectorized版）。
        LFOで変調したディレイインデックスをnp.take/advanced indexingで取得。
        """
        n_samples = len(pcm)
        depth_samp = depth_ms * self._sr / 1000.0
        if depth_samp < 1:
            return pcm

        max_delay = int(depth_samp * 2) + 4
        wet = np.zeros_like(pcm)

        # 時間軸ベクトル
        t_idx = np.arange(n_samples, dtype=np.float64)

        for ch in range(2):
            phase_offset = 0.0 if ch == 0 else math.pi * 0.5
            # LFO変調量（サンプル数）
            lfo = np.sin(2.0 * math.pi * rate_hz * t_idx / self._sr + phase_offset)
            delay_samps = depth_samp * (1.0 + lfo)  # shape (N,)

            # 読み出しインデックス（整数部・小数部）
            read_float = t_idx - delay_samps
            read_int   = np.floor(read_float).astype(np.int64)
            frac       = (read_float - read_int).astype(np.float32)

            # パディング付き入力（負インデックスをゼロで埋める）
            pad = max_delay + 2
            padded = np.concatenate([np.zeros(pad, dtype=np.float32), pcm[:, ch]])
            # インデックスをパディング分オフセット
            idx0 = np.clip(read_int + pad, 0, len(padded) - 1)
            idx1 = np.clip(read_int + pad + 1, 0, len(padded) - 1)

            # 線形補間
            wet[:, ch] = padded[idx0] * (1.0 - frac) + padded[idx1] * frac

        return ((1.0 - mix) * pcm + mix * wet).astype(np.float32)

    # ------------------------------------------------------------------
    # Limiter: ハードリミッター（完全vectorized）
    # ------------------------------------------------------------------
    def _apply_limiter(self, pcm: np.ndarray, ceiling_db: float) -> np.ndarray:
        """
        ブリックウォールリミッター（完全numpy vectorized）。
        """
        ceiling_lin = 10 ** (ceiling_db / 20.0)
        return np.clip(pcm, -ceiling_lin, ceiling_lin).astype(np.float32)
