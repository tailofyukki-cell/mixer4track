"""
各ウィジェットの実際の高さを測定するスクリプト。
"""
import sys
import os
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["SDL_VIDEODRIVER"] = "dummy"

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer

app = QApplication(sys.argv)

from track_model import TrackModel
from mixer_ui import TrackWidget, MasterTrackWidget, EQWidget
from eq_engine import EQParams

# TrackWidgetを作成して高さを測定
track = TrackModel(track_id=0)
tw = TrackWidget(track)
tw.show()
tw.adjustSize()

# MasterTrackWidgetを作成して高さを測定
mw = MasterTrackWidget()
mw.show()
mw.adjustSize()

# EQWidgetを作成して高さを測定
eq_params = EQParams()
eq = EQWidget(track_id=0, params=eq_params)
eq.show()
eq.adjustSize()

def measure():
    print(f"=== 高さ測定結果 ===")
    print(f"TrackWidget 全体高さ: {tw.height()}px")
    print(f"MasterTrackWidget 全体高さ: {mw.height()}px")
    print(f"EQWidget 高さ: {eq.height()}px")
    
    # TrackWidgetの各要素の高さを測定
    print(f"\n=== TrackWidget 内部要素 ===")
    print(f"  _track_label: {tw._track_label.height()}px")
    print(f"  _file_label: {tw._file_label.height()}px")
    print(f"  _load_btn: {tw._load_btn.height()}px")
    print(f"  _gain_slider: {tw._gain_slider.height()}px")
    print(f"  _eq_widget: {tw._eq_widget.height()}px")
    print(f"  _mute_btn: {tw._mute_btn.height()}px")
    print(f"  _fader: {tw._fader.height()}px")
    print(f"  _vol_label: {tw._vol_label.height()}px")
    print(f"  _db_label: {tw._db_label.height()}px")
    print(f"  _pan_slider: {tw._pan_slider.height()}px")
    
    # フェーダーのY位置
    fader_y_track = tw._fader.mapTo(tw, tw._fader.pos()).y()
    fader_y_master = mw._fader.mapTo(mw, mw._fader.pos()).y()
    print(f"\n=== フェーダーY位置 ===")
    print(f"  TrackWidget フェーダーY: {fader_y_track}px")
    print(f"  MasterTrackWidget フェーダーY: {fader_y_master}px")
    print(f"  差分: {fader_y_track - fader_y_master}px (正=MASTERが上)")
    
    # MASTERの各要素
    print(f"\n=== MasterTrackWidget 内部要素 ===")
    print(f"  _clip_label: {mw._clip_label.height()}px")
    print(f"  _export_btn: {mw._export_btn.height()}px")
    print(f"  _fader: {mw._fader.height()}px")
    print(f"  _vol_label: {mw._vol_label.height()}px")
    print(f"  _db_label: {mw._db_label.height()}px")
    
    app.quit()

QTimer.singleShot(500, measure)
app.exec_()
