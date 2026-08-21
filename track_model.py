"""
track_model.py
各トラックの状態を管理するモデルクラス。
将来的に EQ設定・エフェクト設定・波形データ・トラックカラーなどを追加できる構造。
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any


@dataclass
class TrackModel:
    """1トラック分の状態を保持するデータクラス。"""

    track_id: int                       # トラック番号（0始まり）
    name: str = ""                      # トラック名
    file_path: Optional[str] = None     # 音声ファイルパス
    volume: float = 0.80                # 音量 0.0〜1.0
    pan: float = 0.0                    # パン -1.0(L)〜0.0(C)〜1.0(R)
    muted: bool = False                 # ミュート状態
    solo: bool = False                  # ソロ状態
    color: str = "#4a90d9"             # トラックカラー（将来拡張用）

    # Phase 5: EQパラメータ（EQParamsと同期）
    eq_low_gain:  float = 0.0            # EQ Low shelf gain (dB)
    eq_mid_gain:  float = 0.0            # EQ Mid peak gain (dB)
    eq_mid_freq:  float = 1000.0         # EQ Mid center freq (Hz)
    eq_mid_q:     float = 1.0            # EQ Mid Q
    eq_high_gain: float = 0.0            # EQ High shelf gain (dB)
    eq_enabled: bool = True              # EQ ON/OFF
    effect_preset: str = "None"           # エフェクトプリセット名（Phase 6）
    effect_enabled: bool = False          # エフェクト ON/OFF（Phase 6）
    aux_enabled: bool = False             # AUX ON/OFF: TrueのトラックのみにFXを適用（Phase 19）
    gain_db: float = 0.0                  # 入力ゲイン補正 -24dB〜+24dB（Phase 7）
    xfade_assign: str = "THRU"            # X-FADER割当: A / B / THRU（Phase 25）
    waveform_data: Optional[list] = field(default=None, repr=False)  # 波形データ

    def get_display_name(self) -> str:
        """UI 表示用のトラック名を返す。"""
        if self.name:
            return self.name
        return f"Track {self.track_id + 1}"

    def get_file_display_name(self) -> str:
        """ファイル名のみを返す（パスなし）。"""
        if self.file_path:
            import os
            return os.path.basename(self.file_path)
        return "（未読み込み）"

    def get_pan_display(self) -> str:
        """パン値を表示用文字列に変換する。"""
        if abs(self.pan) < 0.01:
            return "C"
        side = "R" if self.pan > 0 else "L"
        val = int(abs(self.pan) * 100)
        return f"{side}{val}"

    def get_volume_percent(self) -> int:
        """音量を 0〜100 の整数で返す。"""
        return int(self.volume * 100)

    def get_volume_db(self) -> str:
        """音量を dB 表示文字列で返す。"""
        import math
        if self.volume <= 0:
            return "-∞ dB"
        db = 20 * math.log10(self.volume)
        return f"{db:+.1f} dB"

    def is_audible(self, any_solo_active: bool) -> bool:
        """このトラックが実際に音を出すべきかを返す。"""
        if self.muted:
            return False
        if any_solo_active and not self.solo:
            return False
        return True

    def to_dict(self) -> dict:
        """プロジェクト保存用に辞書形式で返す（将来拡張用）。"""
        return {
            "track_id": self.track_id,
            "name": self.name,
            "file_path": self.file_path,
            "volume": self.volume,
            "pan": self.pan,
            "muted": self.muted,
            "solo": self.solo,
            "color": self.color,
            "eq_low_gain":  self.eq_low_gain,
            "eq_mid_gain":  self.eq_mid_gain,
            "eq_mid_freq":  self.eq_mid_freq,
            "eq_mid_q":     self.eq_mid_q,
            "eq_high_gain": self.eq_high_gain,
            "eq_enabled":    self.eq_enabled,
            "effect_preset":  self.effect_preset,
            "effect_enabled": self.effect_enabled,
            "gain_db":        self.gain_db,
            "aux_enabled":    self.aux_enabled,
            "xfade_assign":   self.xfade_assign,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrackModel":
        """辞書からトラックモデルを復元する（将来拡張用）。"""
        return cls(
            track_id=data.get("track_id", 0),
            name=data.get("name", ""),
            file_path=data.get("file_path"),
            volume=data.get("volume", 0.80),
            pan=data.get("pan", 0.0),
            muted=data.get("muted", False),
            solo=data.get("solo", False),
            color=data.get("color", "#4a90d9"),
            eq_low_gain=data.get("eq_low_gain",  data.get("eq_low",  0.0)),
            eq_mid_gain=data.get("eq_mid_gain",  data.get("eq_mid",  0.0)),
            eq_mid_freq=data.get("eq_mid_freq",  1000.0),
            eq_mid_q=data.get("eq_mid_q",        1.0),
            eq_high_gain=data.get("eq_high_gain", data.get("eq_high", 0.0)),
            eq_enabled=data.get("eq_enabled",     True),
            effect_preset=data.get("effect_preset",  "None"),
            effect_enabled=data.get("effect_enabled", False),
            gain_db=data.get("gain_db", 0.0),
            aux_enabled=data.get("aux_enabled", False),
            xfade_assign=str(data.get("xfade_assign", "THRU")).upper()
                if str(data.get("xfade_assign", "THRU")).upper() in ("A", "B", "THRU") else "THRU",
        )
