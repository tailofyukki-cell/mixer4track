"""Mixer4Track_Source.zipのUTF-8メタデータと展開内容を検証する。"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile


ARCHIVE = Path(__file__).resolve().parent.parent / "Mixer4Track_Source.zip"
REQUIRED = {
    "mixer4track_git/audio_engine.py",
    "mixer4track_git/audio_param_broker.py",
    "mixer4track_git/package_source.py",
    "mixer4track_git/multithread_event_processing_spec.md",
}


def main() -> None:
    if not ARCHIVE.is_file():
        raise SystemExit(f"missing archive: {ARCHIVE}")

    with ZipFile(ARCHIVE, "r") as archive:
        entries = archive.infolist()
        names = {entry.filename for entry in entries}
        missing = REQUIRED - names
        if missing:
            raise SystemExit(f"missing required entries: {sorted(missing)}")
        non_ascii_entries = [
            entry for entry in entries
            if not entry.is_dir() and not entry.filename.isascii()
        ]
        not_utf8 = [entry.filename for entry in non_ascii_entries if not (entry.flag_bits & 0x800)]
        if not_utf8:
            raise SystemExit(f"missing UTF-8 filename flag: {not_utf8}")

        with tempfile.TemporaryDirectory(prefix="mixer4track_zip_") as tmp:
            destination = Path(tmp)
            archive.extractall(destination)
            missing_after_extract = [name for name in REQUIRED if not (destination / name).is_file()]
            if missing_after_extract:
                raise SystemExit(f"missing after extract: {missing_after_extract}")

    print(f"archive={ARCHIVE}")
    print(f"entries={len(entries)}")
    if non_ascii_entries:
        print("utf8_filename_flags=PASS")
    else:
        print("utf8_filename_flags=NOT_APPLICABLE (all entry names are ASCII)")
    print("extract_name_check=PASS")


if __name__ == "__main__":
    main()
