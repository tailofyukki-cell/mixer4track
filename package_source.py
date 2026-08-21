"""Mixer4Trackのソース配布ZIPを再現可能に生成する。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / "Mixer4Track_Source.zip"
EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules"}
EXCLUDED_NAMES = {"Mixer4Track_Source.zip"}


def should_include(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return not any(part in EXCLUDED_PARTS for part in relative.parts) and path.name not in EXCLUDED_NAMES


def write_utf8_entry(archive: ZipFile, source: Path) -> None:
    relative = source.relative_to(ROOT.parent).as_posix()
    info = ZipInfo(relative)
    info.compress_type = ZIP_DEFLATED
    # Windows互換のUTF-8ファイル名フラグを明示する。
    info.flag_bits |= 0x800
    archive.writestr(info, source.read_bytes())


def main() -> None:
    files = sorted(path for path in ROOT.rglob("*") if path.is_file() and should_include(path))
    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED, allowZip64=True) as archive:
        for source in files:
            write_utf8_entry(archive, source)
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    print(f"created={OUTPUT}")
    print(f"entries={len(files)}")
    print(f"sha256={digest}")


if __name__ == "__main__":
    main()
