"""
全フェーダーを最大値に設定してスクリーンショットを撮影するスクリプト。
フェーダー位置の揃いを確認するため。
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
    # 全トラックフェーダーを最大値（100）に設定
    for tw in window._track_widgets:
        tw._fader.set_value(100, emit=False)
    
    # MASTERフェーダーを150に設定（最大値）
    window._master_widget._fader.set_value(150, emit=False)
    
    QTimer.singleShot(300, take_screenshot)

def take_screenshot():
    pixmap = window.grab()
    pixmap.save("/home/ubuntu/mixer_screenshot_max.png", "PNG")
    print("スクリーンショット保存: /home/ubuntu/mixer_screenshot_max.png")
    app.quit()

QTimer.singleShot(500, setup_and_shoot)
app.exec_()
