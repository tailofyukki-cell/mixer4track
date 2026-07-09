"""
project_store.py
プロジェクト保存・読み込みを担当するモジュール。
Phase 4: 16トラック対応・スキーマバージョン4.0。
"""

import json
import os
from typing import List, Optional, Tuple
from track_model import TrackModel

# プロジェクトファイルのスキーマバージョン
SCHEMA_VERSION = "7.0"


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
             current_bank: int = 0) -> bool:
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
        }
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[ProjectStore] 保存完了: {self._path}")
            return True
        except Exception as e:
            print(f"[ProjectStore] 保存失敗: {e}")
            return False

    def load(self) -> Optional[Tuple[List[TrackModel], float, int]]:
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

            print(f"[ProjectStore] 読み込み完了: {self._path} ({len(tracks)} tracks, bank={current_bank})")
            return tracks, master_volume, current_bank

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
