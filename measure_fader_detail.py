"""
フェーダーの実際の高さとGEQカーブのY位置を測定するスクリプト。
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
    # フェーダーの実際の高さ
    fader_h = tw._fader.height()
    fader_w = tw._fader.width()
    print(f"=== TrackWidget フェーダー ===")
    print(f"  高さ: {fader_h}px, 幅: {fader_w}px")
    print(f"  max_value: {tw._fader._max_value}")
    print(f"  default_value: {tw._fader._default_value}")
    print(f"  current_value: {tw._fader._value}")
    
    # value=80のときのハンドルY位置
    handle_h = 24
    value = 80
    max_val = tw._fader._max_value
    handle_y = int((1.0 - value / max_val) * (fader_h - handle_h)) + handle_h // 2
    print(f"  value=80のハンドルY（フェーダー内相対）: {handle_y}px")
    print(f"  フェーダー高さに対する割合: {handle_y/fader_h*100:.1f}%")
    
    # value=50のときのハンドルY位置（中央）
    handle_y_50 = int((1.0 - 50 / max_val) * (fader_h - handle_h)) + handle_h // 2
    print(f"  value=50のハンドルY（中央）: {handle_y_50}px")
    
    # EQカーブのY位置
    eq_curve = tw._eq_curve
    pos = eq_curve.mapTo(tw, eq_curve.rect().topLeft())
    print(f"\n=== TrackWidget EQカーブ ===")
    print(f"  Y={pos.y()}, H={eq_curve.height()}, bottom={pos.y()+eq_curve.height()}")
    
    # GEQカーブのY位置
    geq_curve = mw._geq_curve
    pos_geq = geq_curve.mapTo(mw, geq_curve.rect().topLeft())
    print(f"\n=== MasterTrackWidget GEQカーブ ===")
    print(f"  Y={pos_geq.y()}, H={geq_curve.height()}, bottom={pos_geq.y()+geq_curve.height()}")
    
    # フェーダーのY位置（ウィジェット内相対）
    fader_pos_track = tw._fader.mapTo(tw, tw._fader.rect().topLeft())
    fader_pos_master = mw._fader.mapTo(mw, mw._fader.rect().topLeft())
    print(f"\n=== フェーダーY位置 ===")
    print(f"  Track フェーダーY: {fader_pos_track.y()}, bottom: {fader_pos_track.y()+tw._fader.height()}")
    print(f"  Master フェーダーY: {fader_pos_master.y()}, bottom: {fader_pos_master.y()+mw._fader.height()}")
    
    # EQカーブとGEQカーブの差分
    eq_bottom = pos.y() + eq_curve.height()
    geq_bottom = pos_geq.y() + geq_curve.height()
    print(f"\n=== EQ/GEQカーブ下端の差分 ===")
    print(f"  Track EQカーブ下端: {eq_bottom}px")
    print(f"  Master GEQカーブ下端: {geq_bottom}px")
    print(f"  差分: {eq_bottom - geq_bottom}px")
    
    app.quit()

QTimer.singleShot(500, measure)
app.exec_()
