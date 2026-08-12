import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication
from mixer_ui import MixerMainWindow


def main():
    app = QApplication(sys.argv)
    window = MixerMainWindow()
    assert window._loop_btn is not None
    assert window._loop_in_btn is not None
    assert window._loop_out_btn is not None
    assert window._loop_all_btn is not None
    assert window._loop_out_btn.isEnabled() is False
    assert window._marker_bar is not None
    window._marker_bar.set_loop_range(True, 1.0, 2.0)
    window._marker_bar.set_loop_range(False)
    QTimer.singleShot(0, app.quit)
    app.exec_()
    window.close()
    print("Loop UI smoke: OK")


if __name__ == "__main__":
    main()
