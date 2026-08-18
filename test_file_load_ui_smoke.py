"""実音声ファイルの読込完了UI処理と下部バーの回帰スモークテスト。"""
import os
import sys
import tempfile
import wave

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from PyQt5.QtWidgets import QApplication

from mixer_ui import MixerMainWindow


def _write_test_wav(path: str) -> None:
    samples = np.full((4410, 2), 1200, dtype=np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(samples.tobytes())


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MixerMainWindow()
    window.resize(1280, 900)
    window.show()
    app.processEvents()

    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "load_smoke.wav")
        _write_test_wav(wav_path)
        assert window._engine.load_file(0, wav_path)
        window._on_load_finished(0, True, wav_path)
        app.processEvents()

        assert window._tracks[0].file_path == wav_path
        assert "load_smoke.wav" in window._track_widgets[0]._file_label.text()
        assert window._marker_bar is not None
        assert window._marker_bar._duration_sec > 0.0

    for width in (1024, 1280):
        transport = window._build_transport()
        transport.resize(width, 110)
        transport.show()
        app.processEvents()
        buttons = (
            window._undo_btn, window._redo_btn, window._play_btn, window._pause_btn,
            window._stop_btn, window._loop_btn, window._save_btn, window._open_btn,
            window._add_marker_btn,
        )
        for button in buttons:
            assert button.geometry().intersects(transport.rect())
        for index, button in enumerate(buttons):
            for other in buttons[index + 1:]:
                overlap = button.geometry().intersected(other.geometry())
                assert overlap.width() == 0 or overlap.height() == 0, (
                    f"Transport controls overlap at {width}px: {button.text()} / {other.text()}"
                )
        transport.close()

    window.close()
    app.processEvents()
    print("File-load and transport-layout smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
