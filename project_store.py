"""
project_store.py
プロジェクト保存・読み込みを担当するモジュール。
Phase 4: 16トラック対応・スキーマバージョン4.0。
Phase 20: マーカー機能追加。
"""

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from track_model import TrackModel


@dataclass
class Marker:
    """
    タイムラインマーカー。再生位置に名前を付けて保存できる。
    """
    marker_id: int          # 一意なID（追加順に連番）
    time_sec: float         # マーカー位置（秒）
    label: str = ""         # マーカー名（空文字可）

    def to_dict(self) -> dict:
        return {
            "marker_id": self.marker_id,
            "time_sec": round(self.time_sec, 4),
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Marker":
        return cls(
            marker_id=int(data.get("marker_id", 0)),
            time_sec=float(data.get("time_sec", 0.0)),
            label=str(data.get("label", "")),
        )

    def get_display_label(self) -> str:
        """表示用ラベル（空の場合は時間を返す）。"""
        if self.label:
            return self.label
        m = int(self.time_sec // 60)
        s = self.time_sec % 60
        return f"{m}:{s:04.1f}"


class MarkerManager:
    """
    マーカーのリストを管理するクラス。
    追加・削除・ジャンプ先取得・ソートを担当する。
    """

    def __init__(self):
        self._markers: List[Marker] = []
        self._next_id: int = 0

    def add(self, time_sec: float, label: str = "") -> Marker:
        """指定位置にマーカーを追加して返す。"""
        m = Marker(marker_id=self._next_id, time_sec=time_sec, label=label)
        self._next_id += 1
        self._markers.append(m)
        self._markers.sort(key=lambda x: x.time_sec)
        return m

    def remove(self, marker_id: int) -> bool:
        """指定IDのマーカーを削除する。成功時True。"""
        before = len(self._markers)
        self._markers = [m for m in self._markers if m.marker_id != marker_id]
        return len(self._markers) < before

    def rename(self, marker_id: int, label: str) -> bool:
        """指定IDのマーカーのラベルを変更する。成功時True。"""
        for m in self._markers:
            if m.marker_id == marker_id:
                m.label = label
                return True
        return False

    def get_all(self) -> List[Marker]:
        """時間順にソートされたマーカーリストを返す。"""
        return list(self._markers)

    def get_by_id(self, marker_id: int) -> Optional[Marker]:
        """IDでマーカーを取得する。"""
        for m in self._markers:
            if m.marker_id == marker_id:
                return m
        return None

    def clear(self):
        """全マーカーを削除する。"""
        self._markers.clear()
        self._next_id = 0

    def to_list(self) -> list:
        """保存用リストに変換する。"""
        return [m.to_dict() for m in self._markers]

    def load_from_list(self, data: list):
        """保存データからマーカーを復元する。"""
        self._markers = [Marker.from_dict(d) for d in data]
        self._markers.sort(key=lambda x: x.time_sec)
        if self._markers:
            self._next_id = max(m.marker_id for m in self._markers) + 1
        else:
            self._next_id = 0

# プロジェクトファイルのスキーマバージョン
SCHEMA_VERSION = "8.0"


class ProjectStore:
    """
    プロジェクトファイル（.m4t / .json）の保存・読み込みを管理する。

    保存形式（JSON）:
    {
        "schema_version": "4.0",
        "app": "Mixer4Track",
        "master_volume": 1.0,
        "current_bank": 0,
        "tracks": [ { ...TrackModel.to_dict()... }, ... ]  # 最大16トラック
    }
    """

    DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Mixer4Track")
    DEFAULT_FILE = "project.m4t"

    def __init__(self, project_path: Optional[str] = None):
        self._path = project_path or os.path.join(self.DEFAULT_DIR, self.DEFAULT_FILE)

    def save(self, tracks: List[TrackModel], master_volume: float = 1.0,
             current_bank: int = 0, markers: Optional[List] = None) -> bool:
        """
        トラック設定・マスター音量・現在のバンクをJSONファイルに保存する。

        Args:
            tracks: 保存するトラックモデルのリスト（最大16）
            master_volume: マスター音量（0.0〜1.5）
            current_bank: 現在表示中のバンク（0 or 1）

        Returns:
            保存成功なら True、失敗なら False
        """
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = {
            "schema_version": SCHEMA_VERSION,
            "app": "Mixer4Track",
            "master_volume": round(master_volume, 4),
            "current_bank": current_bank,
            "tracks": [t.to_dict() for t in tracks],
            "markers": [m.to_dict() for m in (markers or [])],
        }
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[ProjectStore] 保存完了: {self._path}")
            return True
        except Exception as e:
            print(f"[ProjectStore] 保存失敗: {e}")
            return False

    def load(self) -> Optional[Tuple[List[TrackModel], float, int, list]]:
        """
        JSONファイルからトラック設定・マスター音量・バンクを読み込む。

        Returns:
            (tracks, master_volume, current_bank) のタプル。失敗時は None。
        """
        if not os.path.isfile(self._path):
            print(f"[ProjectStore] ファイルが見つかりません: {self._path}")
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # スキーマバージョンチェック（後方互換性のため警告のみ）
            version = data.get("schema_version", "1.0")
            if version != SCHEMA_VERSION:
                print(f"[ProjectStore] スキーマバージョン不一致: {version} (期待: {SCHEMA_VERSION})")

            tracks = [TrackModel.from_dict(d) for d in data.get("tracks", [])]
            master_volume = float(data.get("master_volume", 1.0))
            master_volume = max(0.0, min(1.5, master_volume))  # 範囲クランプ
            current_bank = int(data.get("current_bank", 0))
            current_bank = max(0, min(1, current_bank))  # 0 or 1
            marker_data = data.get("markers", [])

            print(f"[ProjectStore] 読み込み完了: {self._path} ({len(tracks)} tracks, bank={current_bank})")
            return tracks, master_volume, current_bank, marker_data

        except Exception as e:
            print(f"[ProjectStore] 読み込み失敗: {e}")
            return None

    def get_path(self) -> str:
        return self._path

    @staticmethod
    def get_recent_projects(max_count: int = 10) -> List[str]:
        """
        最近使ったプロジェクトファイルのリストを返す（将来拡張用）。
        現在は DEFAULT_DIR 内の .m4t ファイルを更新日時順で返す。
        """
        default_dir = ProjectStore.DEFAULT_DIR
        if not os.path.isdir(default_dir):
            return []
        files = [
            os.path.join(default_dir, f)
            for f in os.listdir(default_dir)
            if f.lower().endswith((".m4t", ".json"))
        ]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files[:max_count]
