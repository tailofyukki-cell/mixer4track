"""
全フェーダーを50%位置に設定してスクリーンショットを撮影するスクリプト。
"""
import sys
import os
os.environ["SDL_AUDIODRIVER"] = "dummy"

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer

app = QApplication(sys.argv)

from mixer_ui import MixerMainWindow

window = MixerMainWindow()
window.resize(1440, 1200)
window.show()

def setup_and_shoot():
    # 全トラックフェーダーを50%（50/100）に設定
    for tw in window._track_widgets:
        tw._fader.set_value(50, emit=False)
    
    # MASTERフェーダーを50%（75/150）に設定
    window._master_widget._fader.set_value(75, emit=False)
    
    QTimer.singleShot(300, take_screenshot)

def take_screenshot():
    pixmap = window.grab()
    pixmap.save("/home/ubuntu/mixer_screenshot_50pct.png", "PNG")
    print("スクリーンショット保存: /home/ubuntu/mixer_screenshot_50pct.png")
    app.quit()

QTimer.singleShot(500, setup_and_shoot)
app.exec_()
