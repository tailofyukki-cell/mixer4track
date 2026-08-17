"""
effect_engine.py — プリセットエフェクターエンジン（Phase 6 / Phase 11改）
numpy only（scipy不要）で動作する6種エフェクト実装。
全DSP処理をnumpyベクトル演算で実装し、Pythonループを排除して高速化。

Phase 11改: ステートフル化（チャンク間バッファ保持）
  - Reverb  : 残響テールを次チャンクに引き継ぐ（チャンク境界での途切れ解消）
  - Delay   : リングバッファをチャンク間で保持（エコーの連続性を保証）
  - Chorus  : LFO位相をチャンク間で保持（位相ジャンプ解消）
  - Compressor: エンベロープ状態をチャンク間で保持
  - Limiter / Distortion: ステートレス（変更なし）

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
from typing import Dict, Any, Optional


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
# MasterLimiter: マスター出力専用ステレオリンク・リミッター
# ---------------------------------------------------------------------------

class MasterLimiter:
    """
    ステレオリンクされたブリックウォール型マスター・リミッター。

    立ち上がりはサンプル単位で即時に抑え、リリースだけを滑らかに戻す。
    これにより左右の定位を崩さず、チャンク境界でもゲインリダクションを
    保持して最終出力のデジタルクリップを防ぐ。
    """

    def __init__(self, sample_rate: int = 44100, release_ms: float = 120.0):
        self._sr = sample_rate
        self._release_ms = max(10.0, min(1000.0, float(release_ms)))
        self._gain = 1.0
        self._last_reduction_db = 0.0

    def reset_state(self):
        """再生開始・停止時にリダクション状態をリセットする。"""
        self._gain = 1.0
        self._last_reduction_db = 0.0

    def set_release_ms(self, release_ms: float):
        self._release_ms = max(10.0, min(1000.0, float(release_ms)))

    def process(self, pcm: np.ndarray, ceiling_db: float) -> tuple[np.ndarray, float]:
        """
        pcmを処理し、(出力PCM, 最大ゲインリダクションdB) を返す。
        ceilingは-12.0〜-0.1dBの範囲で安全に制限する。
        """
        if pcm.size == 0:
            return pcm.astype(np.float32), 0.0

        ceiling_db = max(-12.0, min(-0.1, float(ceiling_db)))
        ceiling = float(10.0 ** (ceiling_db / 20.0))
        peak = np.max(np.abs(pcm), axis=1).astype(np.float32)
        required = np.minimum(1.0, ceiling / np.maximum(peak, 1e-12))
        release_coef = math.exp(-1.0 / (self._sr * (self._release_ms / 1000.0)))

        gains = np.empty(len(pcm), dtype=np.float32)
        gain = float(self._gain)
        min_gain = 1.0
        for index, target in enumerate(required):
            target_gain = float(target)
            if target_gain < gain:
                # アタックは即時。ピークがceilingを超えないことを優先する。
                gain = target_gain
            else:
                # リリースのみ指数カーブで復帰させ、音量の揺れを抑える。
                gain = release_coef * gain + (1.0 - release_coef) * target_gain
            gains[index] = gain
            min_gain = min(min_gain, gain)

        self._gain = gain
        reduction_db = -20.0 * math.log10(max(min_gain, 1e-12))
        self._last_reduction_db = float(reduction_db)
        return (pcm.astype(np.float32) * gains[:, None]).astype(np.float32), float(reduction_db)

    def get_last_reduction_db(self) -> float:
        return self._last_reduction_db


# ---------------------------------------------------------------------------
# EffectEngine: 各エフェクトのDSP処理（ステートフル・numpy vectorized）
# ---------------------------------------------------------------------------

class EffectEngine:
    """
    プリセットエフェクターエンジン（ステートフル版）。
    チャンク間で内部バッファを保持し、チャンク境界での音途切れを防ぐ。
    エフェクト切り替え時はクロスフェードで滑らかに遷移する。
    """

    # クロスフェード長（サンプル数）。20ms相当
    CROSSFADE_SAMPLES = 882  # 44100 * 0.02

    def __init__(self, sample_rate: int = 44100):
        self._sr = sample_rate

        # --- Reverb 内部状態 ---
        # 各コムフィルタの残響テール（チャンク間で引き継ぐ）
        self._reverb_tails: Dict[int, np.ndarray] = {}  # delay_idx -> tail array

        # --- Delay 内部状態 ---
        # リングバッファ（ステレオ）
        self._delay_buf: Optional[np.ndarray] = None   # shape (buf_len, 2)
        self._delay_write_pos: int = 0
        self._delay_samp: int = 0

        # --- Chorus 内部状態 ---
        # LFO位相（チャンク間で継続）
        self._chorus_phase: float = 0.0
        # ディレイバッファ（ステレオ）
        self._chorus_buf: Optional[np.ndarray] = None  # shape (max_delay+2, 2)
        self._chorus_write_pos: int = 0
        self._chorus_max_delay: int = 0

        # --- Compressor 内部状態 ---
        self._comp_env: float = 0.0  # エンベロープ状態

        # --- クロスフェード状態 ---
        self._prev_preset: str = "None"
        self._crossfade_buf: Optional[np.ndarray] = None  # 前エフェクトの出力バッファ
        self._crossfade_pos: int = 0  # クロスフェード進行位置（0=完了）

    def reset_state(self):
        """内部バッファをリセットする（トラック再生開始時に呼ぶ）。"""
        self._reverb_tails = {}
        self._delay_buf = None
        self._delay_write_pos = 0
        self._delay_samp = 0
        self._chorus_phase = 0.0
        self._chorus_buf = None
        self._chorus_write_pos = 0
        self._chorus_max_delay = 0
        self._comp_env = 0.0
        self._prev_preset = "None"
        self._crossfade_buf = None
        self._crossfade_pos = 0

    def apply(self, pcm: np.ndarray, preset_name: str) -> np.ndarray:
        """
        PCMバッファにエフェクトを適用して返す。
        pcm: shape (N, 2) float32, range -1.0〜+1.0
        戻り値: 同形状のfloat32配列
        エフェクト切り替え時はクロスフェードで滑らかに遷移する。
        """
        if preset_name not in EFFECT_PRESETS:
            return pcm
        preset = EFFECT_PRESETS[preset_name]

        # エフェクト切り替え検出
        switching = (preset_name != self._prev_preset)
        if switching:
            # 前エフェクトの出力を保存してクロスフェード開始
            prev_out = self._apply_preset(pcm, self._prev_preset)
            self._crossfade_buf = prev_out
            self._crossfade_pos = self.CROSSFADE_SAMPLES
            # 内部バッファをリセット（新エフェクト用）
            self._reset_stateful_buffers()
            self._prev_preset = preset_name

        # 新エフェクトを適用
        out = self._apply_preset(pcm, preset_name)

        # クロスフェード処理
        if self._crossfade_pos > 0 and self._crossfade_buf is not None:
            n = len(pcm)
            fade_len = min(self._crossfade_pos, n)
            fade_out = np.linspace(1.0, 1.0 - fade_len / self.CROSSFADE_SAMPLES,
                                   fade_len, dtype=np.float32)
            fade_in  = np.linspace(0.0, fade_len / self.CROSSFADE_SAMPLES,
                                   fade_len, dtype=np.float32)
            out[:fade_len, 0] = (self._crossfade_buf[:fade_len, 0] * fade_out
                                 + out[:fade_len, 0] * fade_in)
            out[:fade_len, 1] = (self._crossfade_buf[:fade_len, 1] * fade_out
                                 + out[:fade_len, 1] * fade_in)
            self._crossfade_pos = max(0, self._crossfade_pos - n)
            if self._crossfade_pos == 0:
                self._crossfade_buf = None

        return out

    def _reset_stateful_buffers(self):
        """ステートフルバッファのみリセット（クロスフェード状態は保持）。"""
        self._reverb_tails = {}
        self._delay_buf = None
        self._delay_write_pos = 0
        self._delay_samp = 0
        self._chorus_phase = 0.0
        self._chorus_buf = None
        self._chorus_write_pos = 0
        self._chorus_max_delay = 0
        self._comp_env = 0.0

    def _apply_preset(self, pcm: np.ndarray, preset_name: str) -> np.ndarray:
        """プリセット名に応じてエフェクトを適用する（内部用）。"""
        if preset_name not in EFFECT_PRESETS:
            return pcm.copy()
        preset = EFFECT_PRESETS[preset_name]
        if preset.effect_type == "bypass":
            return pcm.copy()

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
        return pcm.copy()

    # ------------------------------------------------------------------
    # Reverb: ステートフル版（残響テールをチャンク間で引き継ぐ）
    # ------------------------------------------------------------------
    def _apply_reverb(self, pcm: np.ndarray, decay: float, mix: float) -> np.ndarray:
        """
        ステートフルリバーブ。
        各コムフィルタの残響テールを内部バッファに保持し、
        次チャンクの先頭に加算することでチャンク境界の途切れを解消する。
        """
        n_samples = len(pcm)
        mono = pcm.mean(axis=1).astype(np.float32)

        comb_delays_ms = [29.7, 37.1, 41.1, 43.7]
        reverb_mono = np.zeros(n_samples, dtype=np.float32)

        for d_idx, d_ms in enumerate(comb_delays_ms):
            d_samp = int(d_ms * self._sr / 1000)
            if d_samp < 1:
                continue

            # エコー回数を計算
            if decay >= 1.0:
                n_echoes = 20
            else:
                n_echoes = min(int(math.log(1e-2) / math.log(max(decay, 1e-9))), 40)

            # 前チャンクの残響テールを取得
            tail = self._reverb_tails.get(d_idx)

            # 現チャンクのコムフィルタ出力を計算
            comb_out = np.zeros(n_samples, dtype=np.float32)
            coef = 1.0
            for k in range(1, n_echoes + 1):
                offset = d_samp * k
                coef *= decay
                if offset < n_samples:
                    # 現チャンク内のエコー
                    comb_out[offset:] += mono[:n_samples - offset] * coef
                else:
                    # 次チャンクにはみ出すエコー（テールに保存）
                    break

            # 前チャンクのテールを現チャンクの先頭に加算
            if tail is not None:
                add_len = min(len(tail), n_samples)
                comb_out[:add_len] += tail[:add_len]

            # 次チャンク用のテールを計算・保存
            # テール長 = 最大エコーオフセット - チャンク長
            max_offset = d_samp * n_echoes
            tail_len = min(max_offset, self._sr)  # 最大1秒分
            new_tail = np.zeros(tail_len, dtype=np.float32)
            coef = 1.0
            for k in range(1, n_echoes + 1):
                offset = d_samp * k
                coef *= decay
                # チャンクをはみ出す部分をテールに記録
                if offset >= n_samples:
                    tail_offset = offset - n_samples
                    if tail_offset < tail_len:
                        src_len = min(n_samples, tail_len - tail_offset)
                        new_tail[tail_offset:tail_offset + src_len] += mono[:src_len] * coef
                else:
                    # チャンク内だが次チャンクにも響く
                    remaining = n_samples - offset
                    tail_src_len = min(remaining, tail_len)
                    new_tail[:tail_src_len] += mono[offset:offset + tail_src_len] * coef

            self._reverb_tails[d_idx] = new_tail
            reverb_mono += comb_out * 0.25

        # オールパスフィルタ
        allpass_delays_ms = [5.0, 1.7]
        g = 0.7
        for d_ms in allpass_delays_ms:
            d_samp = int(d_ms * self._sr / 1000)
            if d_samp < 1:
                continue
            ap_out = reverb_mono.copy() * (-g)
            ap_out[d_samp:] += reverb_mono[:n_samples - d_samp]
            ap_out[d_samp:] += ap_out[:n_samples - d_samp] * g
            reverb_mono = ap_out

        wet = np.stack([reverb_mono, reverb_mono], axis=1).astype(np.float32)
        return ((1.0 - mix) * pcm + mix * wet).astype(np.float32)

    # ------------------------------------------------------------------
    # Delay: ステートフル版（リングバッファをチャンク間で保持）
    # ------------------------------------------------------------------
    def _apply_delay(self, pcm: np.ndarray, delay_ms: float,
                     feedback: float, mix: float) -> np.ndarray:
        """
        ステートフルフィードバックディレイ。
        リングバッファをチャンク間で保持し、チャンク境界でエコーが途切れない。
        """
        delay_samp = int(delay_ms * self._sr / 1000)
        if delay_samp < 1:
            return pcm.copy()

        n_samples = len(pcm)
        feedback = max(0.0, min(0.9, feedback))

        # バッファサイズ変更検出（プリセット変更時）
        buf_len = delay_samp + 1
        if self._delay_buf is None or self._delay_samp != delay_samp:
            self._delay_buf = np.zeros((buf_len, 2), dtype=np.float32)
            self._delay_write_pos = 0
            self._delay_samp = delay_samp

        buf = self._delay_buf
        write_pos = self._delay_write_pos
        out = np.zeros_like(pcm)

        # サンプル単位でリングバッファを更新
        for i in range(n_samples):
            read_pos = (write_pos - delay_samp) % buf_len
            delayed = buf[read_pos]
            out[i] = pcm[i] * (1.0 - mix) + delayed * mix
            buf[write_pos] = pcm[i] + delayed * feedback
            write_pos = (write_pos + 1) % buf_len

        self._delay_write_pos = write_pos
        return out.astype(np.float32)

    # ------------------------------------------------------------------
    # Compressor: ステートフル版（エンベロープ状態をチャンク間で保持）
    # ------------------------------------------------------------------
    def _apply_compressor(self, pcm: np.ndarray, threshold_db: float,
                          ratio: float, attack_ms: float, release_ms: float) -> np.ndarray:
        """
        ステートフルコンプレッサー。
        エンベロープ状態をチャンク間で保持し、チャンク境界での急激なゲイン変化を防ぐ。
        """
        threshold_lin = 10 ** (threshold_db / 20.0)
        n_samples = len(pcm)

        DS = 8
        sr_ds = self._sr / DS
        attack_coef  = math.exp(-1.0 / max(attack_ms  * sr_ds / 1000.0, 1.0))
        release_coef = math.exp(-1.0 / max(release_ms * sr_ds / 1000.0, 1.0))

        level = np.abs(pcm).mean(axis=1).astype(np.float32)
        n_ds = n_samples // DS
        level_ds = level[:n_ds * DS].reshape(n_ds, DS).max(axis=1).astype(np.float64)

        # エンベロープフォロワー（前チャンクの状態から継続）
        env_ds = np.zeros(n_ds, dtype=np.float64)
        e = self._comp_env
        for i in range(n_ds):
            lv = level_ds[i]
            coef = attack_coef if lv > e else release_coef
            e = coef * e + (1.0 - coef) * lv
            env_ds[i] = e
        self._comp_env = e  # 次チャンクに引き継ぐ

        x_ds = np.arange(n_ds)
        x_full = np.linspace(0, n_ds - 1, n_samples)
        env = np.interp(x_full, x_ds, env_ds).astype(np.float64)

        gain = np.ones(n_samples, dtype=np.float64)
        mask = env > threshold_lin
        if mask.any():
            gain[mask] = (threshold_lin / np.maximum(env[mask], 1e-9)) ** (1.0 - 1.0 / max(ratio, 1.0))

        out = pcm.astype(np.float64)
        out[:, 0] *= gain
        out[:, 1] *= gain
        return out.astype(np.float32)

    # ------------------------------------------------------------------
    # Distortion: tanhソフトクリッピング（ステートレス・変更なし）
    # ------------------------------------------------------------------
    def _apply_distortion(self, pcm: np.ndarray, drive: float, mix: float) -> np.ndarray:
        """
        tanhソフトクリッピングによるディストーション（完全numpy vectorized）。
        ステートレスなので変更なし。
        """
        drive = max(1.0, drive)
        wet = np.tanh(pcm * drive) / math.tanh(drive)
        return ((1.0 - mix) * pcm + mix * wet).astype(np.float32)

    # ------------------------------------------------------------------
    # Chorus: ステートフル版（LFO位相・ディレイバッファをチャンク間で保持）
    # ------------------------------------------------------------------
    def _apply_chorus(self, pcm: np.ndarray, rate_hz: float,
                      depth_ms: float, mix: float) -> np.ndarray:
        """
        ステートフルコーラス。
        LFO位相とディレイバッファをチャンク間で保持し、
        チャンク境界での位相ジャンプと音途切れを解消する。
        """
        n_samples = len(pcm)
        depth_samp = depth_ms * self._sr / 1000.0
        if depth_samp < 1:
            return pcm.copy()

        max_delay = int(depth_samp * 2) + 4

        # ディレイバッファ初期化（サイズ変更時）
        if self._chorus_buf is None or self._chorus_max_delay != max_delay:
            self._chorus_buf = np.zeros((max_delay + 2, 2), dtype=np.float32)
            self._chorus_write_pos = 0
            self._chorus_max_delay = max_delay

        buf = self._chorus_buf
        buf_len = max_delay + 2
        write_pos = self._chorus_write_pos
        phase = self._chorus_phase
        out = np.zeros_like(pcm)

        # サンプル単位でLFO変調ディレイを処理
        phase_inc = 2.0 * math.pi * rate_hz / self._sr
        for i in range(n_samples):
            for ch in range(2):
                phase_offset = 0.0 if ch == 0 else math.pi * 0.5
                lfo = math.sin(phase + phase_offset)
                delay_s = depth_samp * (1.0 + lfo)
                delay_int = int(delay_s)
                frac = delay_s - delay_int
                read_pos0 = (write_pos - delay_int) % buf_len
                read_pos1 = (write_pos - delay_int - 1) % buf_len
                delayed = buf[read_pos0, ch] * (1.0 - frac) + buf[read_pos1, ch] * frac
                out[i, ch] = pcm[i, ch] * (1.0 - mix) + delayed * mix
            buf[write_pos] = pcm[i]
            write_pos = (write_pos + 1) % buf_len
            phase += phase_inc

        # 状態を保存
        self._chorus_write_pos = write_pos
        self._chorus_phase = phase % (2.0 * math.pi)  # オーバーフロー防止
        return out.astype(np.float32)

    # ------------------------------------------------------------------
    # Limiter: ハードリミッター（ステートレス・変更なし）
    # ------------------------------------------------------------------
    def _apply_limiter(self, pcm: np.ndarray, ceiling_db: float) -> np.ndarray:
        """
        ブリックウォールリミッター（完全numpy vectorized）。
        ステートレスなので変更なし。
        """
        ceiling_lin = 10 ** (ceiling_db / 20.0)
        return np.clip(pcm, -ceiling_lin, ceiling_lin).astype(np.float32)
