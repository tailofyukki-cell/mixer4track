"""
フェーダー後のラベル群の高さを詳細測定するスクリプト。
"""
import sys
import os
os.environ["SDL_AUDIODRIVER"] = "dummy"

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer

app = QApplication(sys.argv)

from track_model import TrackModel
from mixer_ui import TrackWidget, MasterTrackWidget

track = TrackModel(track_id=0)
tw = TrackWidget(track)
tw.resize(160, 1200)
tw.show()

mw = MasterTrackWidget()
mw.resize(160, 1200)
mw.show()

def measure():
    print("=== TrackWidget フェーダー後の要素 ===")
    fader_bottom = tw._fader.mapTo(tw, tw._fader.rect().topLeft()).y() + tw._fader.height()
    print(f"  フェーダー下端: {fader_bottom}px")
    
    for name, widget in [
        ("_vol_label", tw._vol_label),
        ("_db_label", tw._db_label),
        ("_pan_slider", tw._pan_slider),
        ("_eq_curve", tw._eq_curve),
    ]:
        pos = widget.mapTo(tw, widget.rect().topLeft())
        print(f"  {name}: Y={pos.y()}, H={widget.height()}, bottom={pos.y()+widget.height()}")
    
    print("\n=== MasterTrackWidget フェーダー後の要素 ===")
    fader_bottom_m = mw._fader.mapTo(mw, mw._fader.rect().topLeft()).y() + mw._fader.height()
    print(f"  フェーダー下端: {fader_bottom_m}px")
    
    for name, widget in [
        ("_vol_label", mw._vol_label),
        ("_db_label", mw._db_label),
        ("_geq_curve", mw._geq_curve),
    ]:
        pos = widget.mapTo(mw, widget.rect().topLeft())
        print(f"  {name}: Y={pos.y()}, H={widget.height()}, bottom={pos.y()+widget.height()}")
    
    # GEQカーブ前のスペース
    geq_y = mw._geq_curve.mapTo(mw, mw._geq_curve.rect().topLeft()).y()
    print(f"\n  GEQカーブY: {geq_y}")
    print(f"  フェーダー後のスペース: {geq_y - fader_bottom_m}px")
    
    # Trackのフェーダー後スペース
    eq_y = tw._eq_curve.mapTo(tw, tw._eq_curve.rect().topLeft()).y()
    print(f"\n  TrackのEQカーブY: {eq_y}")
    print(f"  Trackフェーダー後のスペース: {eq_y - fader_bottom}px")
    
    print(f"\n  差分: {(geq_y - fader_bottom_m) - (eq_y - fader_bottom)}px")
    
    app.quit()

QTimer.singleShot(500, measure)
app.exec_()
