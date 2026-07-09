"""
ミキサーUIのスクリーンショットを撮影するスクリプト。
"""
import sys
import os
os.environ["SDL_AUDIODRIVER"] = "dummy"

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QPixmap

app = QApplication(sys.argv)

# MixerMainWindowを起動
from mixer_ui import MixerMainWindow
from audio_engine import AudioEngine

window = MixerMainWindow()
window.resize(1440, 1200)
window.show()

def take_screenshot():
    # スクリーンショット撮影
    pixmap = window.grab()
    pixmap.save("/home/ubuntu/mixer_screenshot.png", "PNG")
    print("スクリーンショット保存: /home/ubuntu/mixer_screenshot.png")
    app.quit()

QTimer.singleShot(800, take_screenshot)
app.exec_()
