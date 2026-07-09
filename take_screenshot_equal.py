"""
全フェーダーを同じ値（80%）に設定してスクリーンショットを撮影するスクリプト。
フェーダー位置の揃いを確認するため。
"""
import sys
import os
os.environ["SDL_AUDIODRIVER"] = "dummy"

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QPixmap

app = QApplication(sys.argv)

from mixer_ui import MixerMainWindow

window = MixerMainWindow()
window.resize(1440, 1200)
window.show()

def setup_and_shoot():
    # MASTERフェーダーを80に設定（トラックと同じ値）
    window._master_widget._fader.set_value(80, emit=False)
    
    # 少し待ってからスクリーンショット
    QTimer.singleShot(300, take_screenshot)

def take_screenshot():
    pixmap = window.grab()
    pixmap.save("/home/ubuntu/mixer_screenshot_equal.png", "PNG")
    print("スクリーンショット保存: /home/ubuntu/mixer_screenshot_equal.png")
    app.quit()

QTimer.singleShot(500, setup_and_shoot)
app.exec_()
