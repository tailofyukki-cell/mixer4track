"""
各ウィジェットの実際の高さを詳細に測定するスクリプト。
スペーサーを0にした状態で、フェーダー前の要素の合計高さを測定する。
"""
import sys
import os
os.environ["SDL_AUDIODRIVER"] = "dummy"

from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget, QLabel, QPushButton, QFrame, QSizePolicy, QSpacerItem
from PyQt5.QtCore import QTimer, Qt

app = QApplication(sys.argv)

from track_model import TrackModel
from mixer_ui import TrackWidget, MasterTrackWidget, EQWidget
from eq_engine import EQParams

# TrackWidgetを作成
track = TrackModel(track_id=0)
tw = TrackWidget(track)
tw.show()
tw.adjustSize()

# MasterTrackWidgetを作成（スペーサーを0にした状態）
mw = MasterTrackWidget()
mw.show()
mw.adjustSize()

def measure():
    print(f"=== 詳細高さ測定結果 ===")
    print(f"TrackWidget 全体高さ: {tw.height()}px")
    print(f"MasterTrackWidget 全体高さ: {mw.height()}px")
    
    # TrackWidgetの各要素のY位置を測定
    print(f"\n=== TrackWidget 各要素のY位置（ウィジェット内相対）===")
    for name, widget in [
        ("_track_label", tw._track_label),
        ("_file_label", tw._file_label),
        ("_load_btn", tw._load_btn),
        ("_gain_slider", tw._gain_slider),
        ("_eq_widget", tw._eq_widget),
        ("_mute_btn", tw._mute_btn),
        ("_fader", tw._fader),
        ("_vol_label", tw._vol_label),
        ("_db_label", tw._db_label),
        ("_pan_slider", tw._pan_slider),
    ]:
        pos = widget.mapTo(tw, widget.rect().topLeft())
        print(f"  {name}: Y={pos.y()}, H={widget.height()}, bottom={pos.y()+widget.height()}")
    
    # MasterTrackWidgetの各要素のY位置を測定
    print(f"\n=== MasterTrackWidget 各要素のY位置（ウィジェット内相対）===")
    for name, widget in [
        ("_clip_label", mw._clip_label),
        ("_export_btn", mw._export_btn),
        ("_geq_low_btn", mw._geq_low_btn),
        ("_geq_hi_btn", mw._geq_hi_btn),
        ("_geq_status_lbl", mw._geq_status_lbl),
        ("_fx_on_btn", mw._fx_on_btn),
        ("_fx_combo", mw._fx_combo),
        ("_fx_type_lbl", mw._fx_type_lbl),
        ("_fader", mw._fader),
        ("_vol_label", mw._vol_label),
        ("_db_label", mw._db_label),
    ]:
        pos = widget.mapTo(mw, widget.rect().topLeft())
        print(f"  {name}: Y={pos.y()}, H={widget.height()}, bottom={pos.y()+widget.height()}")
    
    # フェーダーのY位置
    fader_y_track = tw._fader.mapTo(tw, tw._fader.rect().topLeft()).y()
    fader_y_master = mw._fader.mapTo(mw, mw._fader.rect().topLeft()).y()
    print(f"\n=== フェーダーY位置（現在のスペーサー462px込み）===")
    print(f"  TrackWidget フェーダーY: {fader_y_track}px")
    print(f"  MasterTrackWidget フェーダーY: {fader_y_master}px")
    print(f"  差分: {fader_y_track - fader_y_master}px (正=MASTERが上, 負=MASTERが下)")
    print(f"  必要なスペーサー調整: 現在462 + ({fader_y_track - fader_y_master}) = {462 + (fader_y_track - fader_y_master)}px")
    
    app.quit()

QTimer.singleShot(500, measure)
app.exec_()
