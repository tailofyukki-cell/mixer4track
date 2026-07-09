"""
main.py
Mixer4Track — 4トラック音楽ミキサー
エントリーポイント
"""

import sys
import os

# サンドボックス環境（オーディオデバイスなし）ではダミードライバを使用
# Windows 実行時はこの環境変数は設定されないので通常動作する
if os.environ.get("SDL_AUDIODRIVER") != "dummy":
    pass  # 通常環境：何もしない

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from mixer_ui import MixerMainWindow


def main():
    # High DPI 対応
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Mixer4Track")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("100DayChallenge")

    window = MixerMainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
