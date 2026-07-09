"""
フェーダーY位置の詳細測定スクリプト。
各ラベルの実際のレンダリング高さを確認する。
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
    # TrackWidget内の全ウィジェットのY位置
    print("=== TrackWidget 各要素の実際のY位置 ===")
    for name, widget in [
        ("_track_label", tw._track_label),
        ("_file_label", tw._file_label),
        ("_load_btn", tw._load_btn),
        ("_gain_slider", tw._gain_slider),
        ("_eq_widget", tw._eq_widget),
        ("_mute_btn", tw._mute_btn),
        ("_fader", tw._fader),
    ]:
        pos = widget.mapTo(tw, widget.rect().topLeft())
        print(f"  {name}: Y={pos.y()}, H={widget.height()}, bottom={pos.y()+widget.height()}")
    
    # MasterTrackWidget内の全ウィジェットのY位置
    print("\n=== MasterTrackWidget 各要素の実際のY位置 ===")
    master_lbl = None
    for child in mw.children():
        from PyQt5.QtWidgets import QLabel
        if isinstance(child, QLabel) and child.text() == "MASTER":
            master_lbl = child
            break
    
    for name, widget in [
        ("_clip_label", mw._clip_label),
        ("_export_btn", mw._export_btn),
        ("_geq_low_btn", mw._geq_low_btn),
        ("_geq_status_lbl", mw._geq_status_lbl),
        ("_fx_on_btn", mw._fx_on_btn),
        ("_fx_combo", mw._fx_combo),
        ("_fx_type_lbl", mw._fx_type_lbl),
        ("_fader", mw._fader),
    ]:
        pos = widget.mapTo(mw, widget.rect().topLeft())
        print(f"  {name}: Y={pos.y()}, H={widget.height()}, bottom={pos.y()+widget.height()}")
    
    # フェーダー差分
    fader_y_track = tw._fader.mapTo(tw, tw._fader.rect().topLeft()).y()
    fader_y_master = mw._fader.mapTo(mw, mw._fader.rect().topLeft()).y()
    print(f"\n=== フェーダーY位置 ===")
    print(f"  Track フェーダーY: {fader_y_track}px")
    print(f"  Master フェーダーY: {fader_y_master}px")
    print(f"  差分: {fader_y_track - fader_y_master}px")
    
    # EQWidget内の詳細
    eq = tw._eq_widget
    print(f"\n=== EQWidget内部詳細 ===")
    print(f"  EQWidget Y={eq.mapTo(tw, eq.rect().topLeft()).y()}, H={eq.height()}")
    for name, widget in [
        ("_eq_knob_area", eq._eq_knob_area),
        ("_preset_area", eq._preset_area),
        ("_knob_high", eq._knob_high),
        ("_knob_mid_freq", eq._knob_mid_freq),
        ("_knob_mid_gain", eq._knob_mid_gain),
        ("_knob_low", eq._knob_low),
        ("_preset_combo", eq._preset_combo),
    ]:
        pos = widget.mapTo(eq, widget.rect().topLeft())
        print(f"  {name}: Y={pos.y()}, H={widget.height()}")
    
    app.quit()

QTimer.singleShot(500, measure)
app.exec_()
