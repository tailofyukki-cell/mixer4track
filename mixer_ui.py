"""
mixer_ui.py
16トラックミキサーのメインウィンドウ（PyQt5）。
Phase 2: マスター音量（TrackWidget同形式）・ミックス書き出し・クリッピング警告。
Phase 3: プロジェクト保存 / 読み込み（JSON）。
Phase 4: 16トラック対応・8トラックずつ2バンク切り替え・バンク切り替え時自動保存。
Phase 3 (追加): 各トラックのKEYラベル下に波形表示ウィジェット（WaveformView）を実装。
"""

import sys
import os
import math
import random
from typing import List, Optional

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSlider, QLabel, QPushButton, QFileDialog, QFrame, QSizePolicy,
    QMessageBox, QProgressDialog, QComboBox, QSpacerItem,
    QDialog, QDialogButtonBox, QListWidget, QListWidgetItem
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, pyqtSlot, QPoint, QRectF
from PyQt5.QtGui import (
    QPainter, QColor, QFont, QPen, QBrush, QPalette
)

from track_model import TrackModel
from audio_engine import AudioEngine, ExportResult
from project_store import ProjectStore
from eq_engine import EQEngine, EQParams, EQ_PRESETS
from effect_engine import EFFECT_PRESETS as FX_PRESETS, EFFECT_CATEGORIES
from geq_engine import (
    GEQParams, GEQEngine, GEQ_LOW_BANDS, GEQ_HI_BANDS,
    get_geq_response_db, GEQ_GAIN_MIN, GEQ_GAIN_MAX
)
import mic_engine


# ===========================================================================
# マイクデバイス選択ダイアログ
# ===========================================================================
class MicDeviceDialog(QDialog):
    """
    マイク入力デバイスを選択するダイアログ。
    mic_engine.get_input_devices() で取得したデバイス一覧を表示し、
    ユーザーが選択したデバイスの (index, name) を返す。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("マイクデバイスを選択")
        self.setMinimumWidth(360)
        self.setStyleSheet("""
            QDialog { background-color: #1e1e2e; color: #e8e8e8; }
            QLabel { color: #e8e8e8; font-size: 11px; }
            QListWidget {
                background-color: #2a2a3e; color: #e8e8e8;
                border: 1px solid #444; border-radius: 4px;
                font-size: 11px;
            }
            QListWidget::item:selected { background-color: #4a90d9; color: #fff; }
            QListWidget::item:hover { background-color: #3a3a5e; }
            QPushButton {
                background-color: #2c3e50; color: #e8e8e8;
                border: 1px solid #444; border-radius: 4px;
                padding: 4px 16px; font-size: 11px;
            }
            QPushButton:hover { background-color: #34495e; }
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        label = QLabel("入力デバイスを選択してください:")
        layout.addWidget(label)

        self._list = QListWidget()
        self._list.setMinimumHeight(160)
        self._devices = mic_engine.get_input_devices()
        for idx, name in self._devices:
            item = QListWidgetItem(f"{name}")
            item.setData(Qt.UserRole, (idx, name))
            self._list.addItem(item)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self._list)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btns.setStyleSheet("""
            QPushButton { background-color: #2c3e50; color: #e8e8e8;
                border: 1px solid #444; border-radius: 4px; padding: 4px 16px; }
            QPushButton:hover { background-color: #34495e; }
        """)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def selected_device(self):
        """
        選択されたデバイスの (index, name) を返す。
        未選択の場合は None を返す。
        """
        item = self._list.currentItem()
        if item:
            return item.data(Qt.UserRole)
        return None


# ===========================================================================
# EQカーブ・スペアナ 拡大表示ウィンドウ
# ===========================================================================
class ExpandedSpectrumWindow(QWidget):
    """
    EQCurveView / GEQCurveView をダブルクリックしたときに開く拡大表示ウィンドウ。
    メインUIのウィジェットと同じ描画ロジックを使いつつ、600×400px の大きなキャンバスで表示する。
    リアルタイムでスペクトルデータと EQ カーブを同期する。
    """

    def __init__(self, title: str, accent_color: str = "#4a90d9",
                 is_geq: bool = False, parent=None,
                 track_id: int = -1):
        super().__init__(parent, Qt.Window)
        self._accent = accent_color
        self._is_geq = is_geq
        self._db_range = 18.0
        self._response: list = []
        self._spectrum_bands = None
        self._track_id = track_id

        self.setWindowTitle(title)
        self.resize(600, 440)
        self.setMinimumSize(400, 300)
        self.setStyleSheet("background-color: #0d1117;")
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        # シークバー（ウィンドウ下部に配置）
        self._seek_bar = TrackSeekBar(accent_color=accent_color, parent=self)
        self._seek_bar.seeked.connect(self._on_seeked)
        self._seek_bar.setGeometry(0, 400, 600, 36)

    def _on_seeked(self, pos_sec: float):
        """シークバー操作時のコールバック。親ウィンドウのエンジンにシークを依頼する。"""
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, '_engine'):
                parent._engine.seek_track(self._track_id, pos_sec)
                break
            parent = parent.parent() if hasattr(parent, 'parent') else None

    def update_seek_bar(self, pos_sec: float, duration_sec: float):
        """シークバーの位置と総時間を更新する。"""
        self._seek_bar.set_position(pos_sec)
        if self._seek_bar._duration_sec != duration_sec:
            self._seek_bar.set_duration(duration_sec)

    def set_seek_bar_peaks(self, peaks: list):
        """波形サムネイルデータをシークバーに設定する。"""
        self._seek_bar.set_peaks(peaks)

    def resizeEvent(self, event):
        """ウィンドウリサイズ時にシークバーを下部に定位する。"""
        h = self.height()
        self._seek_bar.setGeometry(0, h - 36, self.width(), 36)

    # ------------------------------------------------------------------
    # データ更新（親ウィジェットから呼ばれる）
    # ------------------------------------------------------------------
    def update_curve(self, response: list):
        """周波数特性データ [(freq, gain_db), ...] を受け取って再描画。"""
        self._response = response
        self.update()

    def update_spectrum(self, bands):
        """スペクトルバンドデータを受け取って再描画。"""
        self._spectrum_bands = bands
        self.update()

    # ------------------------------------------------------------------
    # 描画
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        import math as _math
        from PyQt5.QtGui import QPainterPath

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        mx, my = 16, 16
        draw_w = w - mx * 2
        draw_h = h - my * 2
        center_y = my + draw_h // 2
        accent = self._accent if not self._is_geq else "#D4A017"

        # 背景
        painter.fillRect(0, 0, w, h, QColor("#0d1117"))

        # スペクトラムアナライザー バー
        if self._spectrum_bands is not None and len(self._spectrum_bands) > 0:
            import numpy as _np
            bands = self._spectrum_bands
            num_bands = len(bands)
            bar_w = max(1, draw_w // num_bands)
            bar_color = QColor(accent)
            bar_color.setAlpha(55)
            painter.setPen(Qt.NoPen)
            for i, val in enumerate(bands):
                if val <= 0.01:
                    continue
                bx = mx + int(i * draw_w / num_bands)
                bar_h = int(val * draw_h)
                by = my + draw_h - bar_h
                painter.fillRect(bx, by, max(1, bar_w - 1), bar_h, bar_color)

        # グリッド線（±18dB まで 6dB 刻み）
        for db in [-12, -6, 0, 6, 12]:
            y = center_y - int(db / self._db_range * (draw_h // 2))
            if db == 0:
                pen = QPen(QColor("#3a4050"), 1, Qt.SolidLine)
            else:
                pen = QPen(QColor("#2a3040"), 1, Qt.DotLine)
            painter.setPen(pen)
            painter.drawLine(mx, y, w - mx, y)
            # dB ラベル
            painter.setPen(QPen(QColor("#4a5060"), 1))
            painter.setFont(QFont("Arial", 8))
            painter.drawText(2, y - 6, mx - 4, 14, Qt.AlignRight | Qt.AlignVCenter,
                             f"{db:+d}")

        # 周波数グリッド＆ラベル（拡大版: より多くの周波数を表示）
        freq_labels = [
            (50, "50"), (100, "100"), (200, "200"), (500, "500"),
            (1000, "1k"), (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k")
        ]
        log_min = _math.log10(20)
        log_max = _math.log10(20000)
        painter.setFont(QFont("Arial", 8))
        for fc, lbl in freq_labels:
            ratio = (_math.log10(fc) - log_min) / (log_max - log_min)
            x = mx + int(ratio * draw_w)
            painter.setPen(QPen(QColor("#2a3040"), 1, Qt.DotLine))
            painter.drawLine(x, my, x, my + draw_h)
            painter.setPen(QPen(QColor("#5a6070"), 1))
            painter.drawText(x - 16, my + draw_h + 2, 32, 14,
                             Qt.AlignCenter, lbl)

        # EQ / GEQ カーブ
        if not self._response:
            painter.setPen(QPen(QColor(accent).lighter(60), 1.5, Qt.SolidLine))
            painter.drawLine(mx, center_y, w - mx, center_y)
        else:
            is_flat = all(abs(g) < 0.05 for _, g in self._response)
            if is_flat:
                painter.setPen(QPen(QColor(accent).lighter(60), 1.5))
                painter.drawLine(mx, center_y, w - mx, center_y)
            else:
                fill_color = QColor(accent)
                fill_color.setAlpha(40)
                path_fill = QPainterPath()

                first_f, first_g = self._response[0]
                ratio0 = (_math.log10(first_f) - log_min) / (log_max - log_min)
                x0 = mx + int(ratio0 * draw_w)
                y0 = center_y - int(first_g / self._db_range * (draw_h // 2))
                path_fill.moveTo(x0, center_y)
                path_fill.lineTo(x0, y0)

                points = []
                for freq, gain_db in self._response:
                    ratio = (_math.log10(freq) - log_min) / (log_max - log_min)
                    x = mx + int(ratio * draw_w)
                    y = center_y - int(gain_db / self._db_range * (draw_h // 2))
                    y = max(my, min(my + draw_h, y))
                    path_fill.lineTo(x, y)
                    points.append((x, y))

                if points:
                    path_fill.lineTo(points[-1][0], center_y)
                path_fill.closeSubpath()
                painter.fillPath(path_fill, QBrush(fill_color))

                curve_color = QColor(accent)
                curve_color.setAlpha(220)
                pen = QPen(curve_color, 2.0)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                for i in range(len(points) - 1):
                    painter.drawLine(points[i][0], points[i][1],
                                     points[i + 1][0], points[i + 1][1])

        painter.end()


# ===========================================================================
# EQカーブ表示ウィジェット
# ===========================================================================
class EQCurveView(QWidget):
    """
    EQの周波数特性カーブを表示するウィジェット。
    - update_curve(params): EQParamsを受け取ってカーブを再描画
    - フラット時は中央の直線のみ表示
    - 操作時は山/谷のカーブを描画（添付画像2枚目のイメージ）
    """

    def __init__(self, accent_color: str = "#4a90d9", parent=None):
        super().__init__(parent)
        self._response: list = []
        self._accent = accent_color
        self._db_range = 18.0
        self.setMinimumHeight(64)
        self.setMaximumHeight(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet("background-color: #0d1117; border-radius: 3px;")
        self._spectrum_bands = None
        self._expanded_win: Optional['ExpandedSpectrumWindow'] = None  # 拡大ウィンドウ参照
        self._track_label: str = ""  # ウィンドウタイトル用

    def set_track_label(self, label: str):
        """拡大ウィンドウのタイトルに使うラベルを設定する。"""
        self._track_label = label

    def update_curve(self, params):
        """EQParamsを受け取って周波数特性カーブを再描画する。"""
        from eq_engine import get_response_db
        self._response = get_response_db(params, n_points=150)
        self.update()
        if self._expanded_win is not None:
            self._expanded_win.update_curve(self._response)

    def update_spectrum(self, bands):
        """スペクトルバンドデータを更新して再描画する。"""
        self._spectrum_bands = bands
        self.update()
        if self._expanded_win is not None:
            self._expanded_win.update_spectrum(bands)

    def clear(self):
        """カーブをフラットに戻す。"""
        self._response = []
        self.update()
        if self._expanded_win is not None:
            self._expanded_win.update_curve([])

    def mouseDoubleClickEvent(self, event):
        """ダブルクリックで拡大ウィンドウを開く（または最前面に移動）。"""
        if self._expanded_win is not None and not self._expanded_win.isHidden():
            self._expanded_win.raise_()
            self._expanded_win.activateWindow()
            return
        title = f"EQ Spectrum - {self._track_label}" if self._track_label else "EQ Spectrum"
        # track_idを抽出（ラベルが 'Track N' 形式の場合）
        _tid = -1
        if self._track_label and self._track_label.startswith("Track "):
            try:
                _tid = int(self._track_label.split(" ")[1]) - 1
            except (ValueError, IndexError):
                pass
        win = ExpandedSpectrumWindow(
            title=title,
            accent_color=self._accent,
            is_geq=False,
            track_id=_tid
        )
        win.update_curve(self._response)
        win.update_spectrum(self._spectrum_bands)
        win.destroyed.connect(self._on_expanded_closed)
        self._expanded_win = win
        win.show()

    def _on_expanded_closed(self):
        """拡大ウィンドウが閉じられたときに参照をクリアする。"""
        self._expanded_win = None

    def paintEvent(self, event):
        from eq_engine import EQParams
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        mx, my = 4, 4  # margin
        draw_w = w - mx * 2
        draw_h = h - my * 2
        center_y = my + draw_h // 2

        # 背景
        painter.fillRect(0, 0, w, h, QColor("#0d1117"))

        # ─── スペクトラムアナライザー バー描画（EQカーブの背景に重ねる）───
        if self._spectrum_bands is not None and len(self._spectrum_bands) > 0:
            import numpy as _np
            bands = self._spectrum_bands
            num_bands = len(bands)
            bar_w = max(1, draw_w // num_bands)
            bar_color = QColor(self._accent)
            bar_color.setAlpha(55)  # 半透明（EQカーブが見えるように）
            painter.setPen(Qt.NoPen)
            for i, val in enumerate(bands):
                if val <= 0.01:
                    continue
                bx = mx + int(i * draw_w / num_bands)
                bar_h = int(val * draw_h)
                by = my + draw_h - bar_h
                painter.fillRect(bx, by, max(1, bar_w - 1), bar_h, bar_color)

        # グリッド線（0dB・±6dB・±12dB）
        for db in [-12, -6, 0, 6, 12]:
            y = center_y - int(db / self._db_range * (draw_h // 2))
            if db == 0:
                pen = QPen(QColor("#2a3040"), 1, Qt.SolidLine)
            else:
                pen = QPen(QColor("#1a2030"), 1, Qt.DotLine)
            painter.setPen(pen)
            painter.drawLine(mx, y, w - mx, y)

        # 周波数ラベル（100Hz / 1kHz / 10kHz）
        painter.setPen(QPen(QColor("#2a3040"), 1))
        painter.setFont(QFont("Arial", 6))
        import math as _math
        for fc, lbl in [(100, "100"), (1000, "1k"), (10000, "10k")]:
            ratio = (_math.log10(fc) - _math.log10(20)) / (_math.log10(20000) - _math.log10(20))
            x = mx + int(ratio * draw_w)
            painter.drawLine(x, my, x, my + draw_h)
            painter.drawText(x - 8, my + draw_h - 2, 20, 10, Qt.AlignCenter, lbl)

        # EQカーブ
        if not self._response:
            # フラット: 中央に実線
            painter.setPen(QPen(QColor(self._accent).lighter(60), 1, Qt.SolidLine))
            painter.drawLine(mx, center_y, w - mx, center_y)
        else:
            is_flat = all(abs(g) < 0.05 for _, g in self._response)

            if is_flat:
                # フラット: 細い線
                painter.setPen(QPen(QColor(self._accent).lighter(60), 1))
                painter.drawLine(mx, center_y, w - mx, center_y)
            else:
                # 塗りつぶしエリア（半透明）
                from PyQt5.QtGui import QPainterPath
                fill_color = QColor(self._accent)
                fill_color.setAlpha(40)
                path_fill = QPainterPath()
                import math as _math2
                log_min = _math2.log10(20)
                log_max = _math2.log10(20000)

                first_f, first_g = self._response[0]
                ratio0 = (_math2.log10(first_f) - log_min) / (log_max - log_min)
                x0 = mx + int(ratio0 * draw_w)
                y0 = center_y - int(first_g / self._db_range * (draw_h // 2))
                path_fill.moveTo(x0, center_y)
                path_fill.lineTo(x0, y0)

                points = []
                for freq, gain_db in self._response:
                    ratio = (_math2.log10(freq) - log_min) / (log_max - log_min)
                    x = mx + int(ratio * draw_w)
                    y = center_y - int(gain_db / self._db_range * (draw_h // 2))
                    y = max(my, min(my + draw_h, y))
                    path_fill.lineTo(x, y)
                    points.append((x, y))

                last_x = points[-1][0]
                path_fill.lineTo(last_x, center_y)
                path_fill.closeSubpath()
                painter.fillPath(path_fill, QBrush(fill_color))

                # カーブ線
                curve_color = QColor(self._accent)
                curve_color.setAlpha(220)
                pen = QPen(curve_color, 1.5)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                for i in range(len(points) - 1):
                    painter.drawLine(points[i][0], points[i][1],
                                     points[i+1][0], points[i+1][1])

        painter.end()


# ===========================================================================
# GEQカーブ表示ウィジェット（MASTERトラック下部用）
# ===========================================================================
class GEQCurveView(QWidget):
    """
    MASTERトラック下部に表示するGEQカーブウィジェット。
    GEQ Low/Hiの合成周波数特性を表示する。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._response: list = []  # [(freq_hz, gain_db), ...]
        self._db_range = 18.0
        self.setMinimumHeight(64)
        self.setMaximumHeight(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet("background-color: #0d1117; border-radius: 3px;")
        self._spectrum_bands = None
        self._expanded_win: Optional['ExpandedSpectrumWindow'] = None  # 拡大ウィンドウ参照

    def update_spectrum(self, bands):
        """スペクトルバンドデータを更新して再描画する。"""
        self._spectrum_bands = bands
        self.update()
        if self._expanded_win is not None:
            self._expanded_win.update_spectrum(bands)

    def update_curve(self, params: GEQParams):
        """GEQParamsを受け取ってカーブを再描画する。"""
        self._response = get_geq_response_db(params)
        self.update()
        if self._expanded_win is not None:
            self._expanded_win.update_curve(self._response)

    def clear(self):
        self._response = []
        self.update()
        if self._expanded_win is not None:
            self._expanded_win.update_curve([])

    def mouseDoubleClickEvent(self, event):
        """ダブルクリックで拡大ウィンドウを開く（または最前面に移動）。"""
        if self._expanded_win is not None and not self._expanded_win.isHidden():
            self._expanded_win.raise_()
            self._expanded_win.activateWindow()
            return
        win = ExpandedSpectrumWindow(
            title="GEQ Spectrum - MASTER",
            accent_color="#D4A017",
            is_geq=True
        )
        win.update_curve(self._response)
        win.update_spectrum(self._spectrum_bands)
        win.destroyed.connect(self._on_expanded_closed)
        self._expanded_win = win
        win.show()

    def _on_expanded_closed(self):
        """拡大ウィンドウが閉じられたときに参照をクリアする。"""
        self._expanded_win = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        mx, my = 4, 4
        draw_w = w - mx * 2
        draw_h = h - my * 2
        center_y = my + draw_h // 2

        painter.fillRect(0, 0, w, h, QColor("#0d1117"))

        # ─── スペクトラムアナライザー バー描画（GEQカーブの背景に重ねる）───
        if self._spectrum_bands is not None and len(self._spectrum_bands) > 0:
            bands = self._spectrum_bands
            num_bands = len(bands)
            bar_w = max(1, draw_w // num_bands)
            bar_color = QColor("#D4A017")  # GEQアクセントカラー（黄色系）
            bar_color.setAlpha(55)
            painter.setPen(Qt.NoPen)
            for i, val in enumerate(bands):
                if val <= 0.01:
                    continue
                bx = mx + int(i * draw_w / num_bands)
                bar_h = int(val * draw_h)
                by = my + draw_h - bar_h
                painter.fillRect(bx, by, max(1, bar_w - 1), bar_h, bar_color)

        # グリッド線
        import math as _math
        for db in [-12, -6, 0, 6, 12]:
            y = center_y - int(db / self._db_range * (draw_h // 2))
            pen = QPen(QColor("#2a3040") if db == 0 else QColor("#1a2030"), 1,
                       Qt.SolidLine if db == 0 else Qt.DotLine)
            painter.setPen(pen)
            painter.drawLine(mx, y, w - mx, y)

        # 周波数ラベル
        painter.setPen(QPen(QColor("#2a3040"), 1))
        painter.setFont(QFont("Arial", 6))
        for fc, lbl in [(100, "100"), (1000, "1k"), (10000, "10k")]:
            ratio = (_math.log10(fc) - _math.log10(20)) / (_math.log10(20000) - _math.log10(20))
            x = mx + int(ratio * draw_w)
            painter.drawLine(x, my, x, my + draw_h)
            painter.drawText(x - 8, my + draw_h - 2, 20, 10, Qt.AlignCenter, lbl)

        # GEQカーブ（黄色系）
        accent = "#D4A017"
        if not self._response:
            painter.setPen(QPen(QColor(accent).lighter(60), 1, Qt.SolidLine))
            painter.drawLine(mx, center_y, w - mx, center_y)
        else:
            is_flat = all(abs(g) < 0.05 for _, g in self._response)
            if is_flat:
                painter.setPen(QPen(QColor(accent).lighter(60), 1))
                painter.drawLine(mx, center_y, w - mx, center_y)
            else:
                from PyQt5.QtGui import QPainterPath
                fill_color = QColor(accent)
                fill_color.setAlpha(40)
                path_fill = QPainterPath()
                log_min = _math.log10(20)
                log_max = _math.log10(20000)

                first_f, first_g = self._response[0]
                ratio0 = (_math.log10(first_f) - log_min) / (log_max - log_min)
                x0 = mx + int(ratio0 * draw_w)
                y0 = center_y - int(first_g / self._db_range * (draw_h // 2))
                path_fill.moveTo(x0, center_y)
                path_fill.lineTo(x0, y0)

                points = []
                for freq, gain_db in self._response:
                    ratio = (_math.log10(freq) - log_min) / (log_max - log_min)
                    x = mx + int(ratio * draw_w)
                    y = center_y - int(gain_db / self._db_range * (draw_h // 2))
                    y = max(my, min(my + draw_h, y))
                    path_fill.lineTo(x, y)
                    points.append((x, y))

                last_x = points[-1][0]
                path_fill.lineTo(last_x, center_y)
                path_fill.closeSubpath()
                painter.fillPath(path_fill, QBrush(fill_color))

                curve_color = QColor(accent)
                curve_color.setAlpha(220)
                pen = QPen(curve_color, 1.5)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                for i in range(len(points) - 1):
                    painter.drawLine(points[i][0], points[i][1],
                                     points[i+1][0], points[i+1][1])

        painter.end()


# ===========================================================================
# KnobWidget （ノブUI）
# ===========================================================================
class KnobWidget(QWidget):
    """
    ミキサーノブ風の回転ノブウィジェット。
    ドラッグ（上下）で値を変更、ダブルクリックでリセット。
    """
    valueChanged = pyqtSignal(float)

    def __init__(self, label: str, min_val: float, max_val: float,
                 default_val: float, unit: str = "",
                 color: str = "#3aaf6e", parent=None):
        super().__init__(parent)
        self._label    = label
        self._min      = min_val
        self._max      = max_val
        self._default  = default_val
        self._value    = default_val
        self._unit     = unit
        self._color    = color
        self._dragging = False
        self._drag_start_x   = 0
        self._drag_start_val = default_val
        self.setFixedSize(60, 84)
        self.setCursor(Qt.SizeHorCursor)
        self.setToolTip(f"{label}: {default_val}{unit}")

    def get_value(self) -> float:
        return self._value

    def set_value(self, value: float, emit: bool = True):
        value = max(self._min, min(self._max, value))
        if abs(value - self._value) > 1e-6:
            self._value = value
            self.setToolTip(f"{self._label}: {value:.1f}{self._unit}")
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_start_x   = event.globalX()
            self._drag_start_val = self._value

    def mouseMoveEvent(self, event):
        if self._dragging:
            dx = event.globalX() - self._drag_start_x  # 右にドラッグで増加
            sensitivity = (self._max - self._min) / 120.0
            self.set_value(self._drag_start_val + dx * sensitivity)

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def mouseDoubleClickEvent(self, event):
        self.set_value(self._default)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # ラベル（上）
        painter.setPen(QPen(QColor("#aaaaaa"), 1))
        painter.setFont(QFont("Arial", 7, QFont.Bold))
        painter.drawText(0, 0, w, 14, Qt.AlignCenter, self._label)

        # ノブ本体
        cx, cy, r = w // 2, 14 + 22, 20
        # 外枚（ベゼル）
        painter.setBrush(QBrush(QColor("#222222")))
        painter.setPen(QPen(QColor("#555555"), 2))
        painter.drawEllipse(cx - r - 2, cy - r - 2, (r + 2) * 2, (r + 2) * 2)
        # ノブ面
        ratio = (self._value - self._min) / (self._max - self._min) if self._max != self._min else 0.5
        knob_color = QColor(self._color)
        if self._dragging:
            knob_color = knob_color.lighter(120)
        painter.setBrush(QBrush(knob_color))
        painter.setPen(QPen(QColor("#333333"), 1))
        painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        # インジケーター線（-135°〜+135°、12時方向が中心=0）
        # 12時方向=90°、左端=-135°+90°=-45°相当、右端=+135°+90°=225°相当
        # ratio=0.5のとき angle_deg=0 → 12時方向(90°)
        angle_deg = 90.0 - (-135.0 + ratio * 270.0)
        angle_rad = math.radians(angle_deg)
        ix = cx + int((r - 4) * math.cos(angle_rad))
        iy = cy - int((r - 4) * math.sin(angle_rad))
        painter.setPen(QPen(QColor("#111111"), 2))
        painter.drawLine(cx, cy, ix, iy)
        # スケールマーク（左端・右端）
        painter.setPen(QPen(QColor("#666666"), 1))
        painter.setFont(QFont("Arial", 6))
        left_str  = f"{self._min:.0f}" if self._unit != "Hz" else f"{self._min:.0f}"
        right_str = f"+{self._max:.0f}" if self._max > 0 and self._unit != "Hz" else f"{self._max:.0f}"
        painter.drawText(0, cy + r, w // 2, 14, Qt.AlignLeft,  left_str)
        painter.drawText(w // 2, cy + r, w // 2, 14, Qt.AlignRight, right_str)

        # 値表示（下）
        painter.setPen(QPen(QColor("#cccccc"), 1))
        painter.setFont(QFont("Arial", 7))
        if self._unit == "Hz":
            val_str = f"{int(self._value)}Hz"
        else:
            val_str = f"{self._value:+.1f}{self._unit}"
        painter.drawText(0, cy + r + 12, w, 14, Qt.AlignCenter, val_str)
        painter.end()


# ===========================================================================
# EQWidget （縦並びノブ・プリセット・切り替えボタン）
# ===========================================================================
class EQWidget(QFrame):
    """
    3バンドEQウィジェット。
    ノブ並び: 縦並び [HIGH] [MID GAIN] [MID FREQ] [LOW]（上から順）
    EQモード / プリセットモードの切り替えボタンあり。
    """
    eq_changed = pyqtSignal(int, object)  # (track_id, EQParams)

    KNOB_COLOR = "#3aaf6e"  # 緑ノブ（実機イメージ）

    def __init__(self, track_id: int, params: EQParams, accent: str = "#4a90d9", parent=None):
        super().__init__(parent)
        self._track_id  = track_id
        self._params    = params
        self._accent    = accent
        self._updating  = False
        self._eq_mode   = True   # True=EQノブ有効, False=プリセット有効
        self._setup_ui()

    # FREQノブの12時方向デフォルト値（線形スケールの中央）
    FREQ_DEFAULT = (250.0 + 5000.0) / 2.0  # = 2625.0 Hz

    def _setup_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
            EQWidget {
                background-color: #1a1a1a;
                border: 1px solid #333;
                border-radius: 3px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ヘッダー行: EQラベル + EQ/PRESET切り替えボタン
        header_row = QHBoxLayout()
        header_row.setSpacing(4)
        header_row.setContentsMargins(0, 0, 0, 0)
        eq_lbl = QLabel("EQ")
        eq_lbl.setStyleSheet(f"color: {self._accent}; font-size: 9px; font-weight: bold; letter-spacing: 1px;")
        header_row.addWidget(eq_lbl)
        header_row.addStretch()
        # EQモードボタン
        self._eq_mode_btn = QPushButton("EQ")
        self._eq_mode_btn.setFixedSize(28, 16)
        self._eq_mode_btn.setCheckable(True)
        self._eq_mode_btn.setChecked(True)
        self._eq_mode_btn.clicked.connect(lambda: self._set_eq_mode(True))
        # PRESETモードボタン
        self._preset_mode_btn = QPushButton("PRE")
        self._preset_mode_btn.setFixedSize(28, 16)
        self._preset_mode_btn.setCheckable(True)
        self._preset_mode_btn.setChecked(False)
        self._preset_mode_btn.clicked.connect(lambda: self._set_eq_mode(False))
        header_row.addWidget(self._eq_mode_btn)
        header_row.addWidget(self._preset_mode_btn)
        layout.addLayout(header_row)
        self._update_mode_buttons()

        # EQノブエリア（縦並び）
        self._eq_knob_area = QWidget()
        knob_layout = QVBoxLayout(self._eq_knob_area)
        knob_layout.setContentsMargins(4, 4, 4, 4)
        knob_layout.setSpacing(8)
        knob_layout.setAlignment(Qt.AlignHCenter)

        self._knob_high = KnobWidget(
            "HIGH", -15.0, 15.0, self._params.high_gain_db,
            unit="dB", color=self.KNOB_COLOR
        )
        self._knob_mid_gain = KnobWidget(
            "MID", -15.0, 15.0, self._params.mid_gain_db,
            unit="dB", color=self.KNOB_COLOR
        )
        # FREQのデフォルトは12時方向（スケール中央=2625Hz）
        self._knob_mid_freq = KnobWidget(
            "FREQ", 250.0, 5000.0, self.FREQ_DEFAULT,
            unit="Hz", color=self.KNOB_COLOR
        )
        self._knob_low = KnobWidget(
            "LOW", -15.0, 15.0, self._params.low_gain_db,
            unit="dB", color=self.KNOB_COLOR
        )

        # 順序: HIGH → FREQ → MID → LOW
        for knob in [self._knob_high, self._knob_mid_freq, self._knob_mid_gain, self._knob_low]:
            knob_layout.addWidget(knob, alignment=Qt.AlignHCenter)
        layout.addWidget(self._eq_knob_area)

        # --- 区切り線 ---
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #333;")
        layout.addWidget(sep)

        # プリセットエリア（常時表示）
        self._preset_area = QWidget()
        preset_layout = QVBoxLayout(self._preset_area)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        preset_layout.setSpacing(4)
        preset_lbl = QLabel("PRESET")
        preset_lbl.setAlignment(Qt.AlignCenter)
        preset_lbl.setStyleSheet(f"color: {self._accent}; font-size: 9px; font-weight: bold; letter-spacing: 1px;")
        preset_layout.addWidget(preset_lbl)
        self._preset_combo = QComboBox()
        self._preset_combo.setStyleSheet("""
            QComboBox {
                background-color: #2a2a2a; color: #ddd;
                border: 1px solid #555; border-radius: 3px;
                font-size: 11px; font-weight: bold; padding: 4px 6px;
                min-height: 24px;
            }
            QComboBox::drop-down { border: none; width: 18px; }
            QComboBox QAbstractItemView {
                background-color: #2a2a2a; color: #ddd;
                selection-background-color: #3a5068;
                font-size: 11px; font-weight: bold;
                padding: 4px;
            }
        """)
        for name in EQ_PRESETS:
            self._preset_combo.addItem(name)
        self._preset_combo.setCurrentText("Flat")
        self._preset_combo.currentTextChanged.connect(self._on_preset_changed)
        preset_layout.addWidget(self._preset_combo)
        layout.addWidget(self._preset_area)

        # 初期表示: 両方表示、プリセット側は暗くして操作不可
        self._apply_dimming()

        # シグナル接続
        self._knob_low.valueChanged.connect(self._on_knob_changed)
        self._knob_mid_gain.valueChanged.connect(self._on_knob_changed)
        self._knob_mid_freq.valueChanged.connect(self._on_knob_changed)
        self._knob_high.valueChanged.connect(self._on_knob_changed)

    def _set_eq_mode(self, eq_mode: bool):
        """EQモード(True)またはプリセットモード(False)に切り替える。両方常時表示、無効側はグレーアウト。"""
        self._eq_mode = eq_mode
        self._update_mode_buttons()
        self._apply_dimming()
        # モード切り替え時: 有効な側のパラメータを送信
        # EQモード -> 現在のノブ値を送信
        # PREモード -> フラット(EQバイパス)を送信
        self.eq_changed.emit(self._track_id, self.get_effective_params())

    def _apply_dimming(self):
        """無効側のエリアをグレーアウトし、操作も無効化する。"""
        # EQノブエリア
        eq_active = self._eq_mode
        self._eq_knob_area.setEnabled(eq_active)
        self._eq_knob_area.setStyleSheet(
            "" if eq_active else
            "QWidget { opacity: 0.3; } KnobWidget { background: transparent; }"
        )
        # ノブの色を切り替える
        dim_color = "#3a3a3a"
        active_color = self.KNOB_COLOR
        for knob in [self._knob_high, self._knob_mid_freq, self._knob_mid_gain, self._knob_low]:
            knob._color = active_color if eq_active else dim_color
            knob.update()
        # プリセットエリア
        preset_active = not self._eq_mode
        self._preset_area.setEnabled(preset_active)
        # プリセットコンボの色
        if preset_active:
            self._preset_combo.setStyleSheet("""
                QComboBox {
                    background-color: #2a2a2a; color: #ddd;
                    border: 1px solid #555; border-radius: 3px;
                    font-size: 11px; font-weight: bold; padding: 4px 6px;
                    min-height: 24px;
                }
                QComboBox::drop-down { border: none; width: 18px; }
                QComboBox QAbstractItemView {
                    background-color: #2a2a2a; color: #ddd;
                    selection-background-color: #3a5068;
                    font-size: 11px; font-weight: bold; padding: 4px;
                }
            """)
        else:
            self._preset_combo.setStyleSheet("""
                QComboBox {
                    background-color: #1a1a1a; color: #444;
                    border: 1px solid #2a2a2a; border-radius: 3px;
                    font-size: 11px; font-weight: bold; padding: 4px 6px;
                    min-height: 24px;
                }
                QComboBox::drop-down { border: none; width: 18px; }
                QComboBox QAbstractItemView {
                    background-color: #1a1a1a; color: #444;
                    font-size: 11px; padding: 4px;
                }
            """)

    def _update_mode_buttons(self):
        """イコライザー/PRESETボタンの表示を更新する。有効側は明るく、無効側は暗く。"""
        active_style = f"""
            QPushButton {{
                background-color: {self._accent}; color: #111;
                border: 1px solid {self._accent}; border-radius: 2px;
                font-size: 7px; font-weight: bold;
            }}
        """
        inactive_style = """
            QPushButton {
                background-color: #2a2a2a; color: #555;
                border: 1px solid #333; border-radius: 2px;
                font-size: 7px;
            }
            QPushButton:hover { background-color: #3a3a3a; color: #888; }
        """
        self._eq_mode_btn.setStyleSheet(active_style if self._eq_mode else inactive_style)
        self._preset_mode_btn.setStyleSheet(inactive_style if self._eq_mode else active_style)
        self._eq_mode_btn.setChecked(self._eq_mode)
        self._preset_mode_btn.setChecked(not self._eq_mode)

    def is_eq_mode(self) -> bool:
        """EQモードが有効ならTrue、プリセットモードならFalse。"""
        return self._eq_mode

    def get_effective_params(self) -> EQParams:
        """現在有効なEQパラメータを返す。プリセットモード時はフラットなEQParamsを返す。"""
        if self._eq_mode:
            return self._params
        else:
            return EQParams()  # フラット（バイパス）

    def _on_knob_changed(self, _):
        if self._updating:
            return
        if not self._eq_mode:
            return  # EQモード無効時は無視
        self._params.low_gain_db  = self._knob_low.get_value()
        self._params.mid_gain_db  = self._knob_mid_gain.get_value()
        self._params.mid_freq_hz  = self._knob_mid_freq.get_value()
        self._params.high_gain_db = self._knob_high.get_value()
        self.eq_changed.emit(self._track_id, self._params)

    def _on_preset_changed(self, name: str):
        if self._updating or name not in EQ_PRESETS:
            return
        if self._eq_mode:  # EQモード時はプリセット変更を無視
            return
        preset = EQ_PRESETS[name]
        self._params.low_gain_db  = preset.low_gain_db
        self._params.mid_gain_db  = preset.mid_gain_db
        self._params.mid_freq_hz  = preset.mid_freq_hz
        self._params.mid_q        = preset.mid_q
        self._params.high_gain_db = preset.high_gain_db
        self._updating = True
        self._knob_low.set_value(preset.low_gain_db, emit=False)
        self._knob_mid_gain.set_value(preset.mid_gain_db, emit=False)
        self._knob_mid_freq.set_value(preset.mid_freq_hz, emit=False)
        self._knob_high.set_value(preset.high_gain_db, emit=False)
        self._updating = False
        # プリセットモード時はプリセットのパラメータをそのまま送信
        self.eq_changed.emit(self._track_id, self._params)

    def restore_params(self, params: EQParams):
        """プロジェクト読み込み時にEQParamsをUIに復元する。"""
        self._params = params
        self._updating = True
        self._knob_low.set_value(params.low_gain_db, emit=False)
        self._knob_mid_gain.set_value(params.mid_gain_db, emit=False)
        self._knob_mid_freq.set_value(params.mid_freq_hz, emit=False)
        self._knob_high.set_value(params.high_gain_db, emit=False)
        self._preset_combo.setCurrentText("Flat" if params.is_flat() else "")
        self._updating = False

    def get_params(self) -> EQParams:
        return self._params


# ===========================================================================
# カラーパレット
# ===========================================================================
class Colors:
    BG_MAIN        = "#1a1a1a"
    BG_TRACK       = "#242424"
    BG_TRACK_DARK  = "#1e1e1e"
    BORDER         = "#3a3a3a"
    FADER_GROOVE   = "#111111"
    FADER_HANDLE   = "#c8c8c8"
    FADER_ACTIVE   = "#e0e0e0"
    TEXT_PRIMARY   = "#e8e8e8"
    TEXT_SECONDARY = "#888888"
    TEXT_LABEL     = "#aaaaaa"
    BTN_MUTE_OFF   = "#333333"
    BTN_MUTE_ON    = "#e67e22"
    BTN_SOLO_OFF   = "#333333"
    BTN_SOLO_ON    = "#27ae60"
    BTN_LOAD       = "#2c3e50"
    BTN_LOAD_HOV   = "#34495e"
    BTN_PLAY       = "#27ae60"
    BTN_PLAY_HOV   = "#2ecc71"
    BTN_STOP       = "#c0392b"
    BTN_STOP_HOV   = "#e74c3c"
    BTN_PAUSE      = "#d4a017"
    BTN_PAUSE_HOV  = "#f0c040"
    BTN_RESUME     = "#2980b9"
    BTN_RESUME_HOV = "#3498db"
    BTN_EXPORT     = "#6c3483"
    BTN_EXPORT_HOV = "#8e44ad"
    BTN_SAVE       = "#1a5276"
    BTN_SAVE_HOV   = "#2471a3"
    BTN_OPEN       = "#145a32"
    BTN_OPEN_HOV   = "#1e8449"
    BTN_BANK_A     = "#2e4057"
    BTN_BANK_A_ACT = "#4a90d9"
    BTN_BANK_B     = "#2e4057"
    BTN_BANK_B_ACT = "#e74c3c"
    METER_LOW      = "#27ae60"
    METER_MID      = "#f39c12"
    METER_HIGH     = "#e74c3c"
    METER_BG       = "#111111"
    MASTER_ACCENT  = "#f1c40f"
    # Bank A: 青〜シアン系8色（各トラック個別色）
    ACCENT_COLORS_A = [
        "#4a90d9",  # Track 1: 青
        "#3ab0e8",  # Track 2: 明るい青
        "#2ec4b6",  # Track 3: ティール
        "#5ba3e8",  # Track 4: 薄青
        "#00bcd4",  # Track 5: シアン
        "#4fc3f7",  # Track 6: 水色
        "#26a69a",  # Track 7: 青緑
        "#7986cb",  # Track 8: インディゴ
    ]
    # Bank B: 赤/橙/緑/紫系8色
    ACCENT_COLORS_B = [
        "#e74c3c",  # Track 9: 赤
        "#e67e22",  # Track 10: 橙
        "#27ae60",  # Track 11: 緑
        "#9b59b6",  # Track 12: 紫
        "#f39c12",  # Track 13: 黄橙
        "#e91e63",  # Track 14: ピンク
        "#8bc34a",  # Track 15: 黄緑
        "#ff7043",  # Track 16: 深橙
    ]
    CLIP_WARNING   = "#e74c3c"


# ===========================================================================
# バックグラウンド書き出しスレッド
# ===========================================================================
class ExportWorker(QThread):
    """録音バッファのWAV書き出しをバックグラウンドで実行するスレッド。"""

    finished = pyqtSignal(object)

    def __init__(self, engine: AudioEngine, output_path: str):
        super().__init__()
        self._engine = engine
        self._output_path = output_path

    def run(self):
        result = self._engine.export_rec_buffer(self._output_path)
        self.finished.emit(result)


class LoadWorker(QThread):
    """音声ファイルの読み込みとEQ適用をバックグラウンドで実行するスレッド。"""
    finished = pyqtSignal(int, bool, str)  # (track_id, ok, path)

    def __init__(self, engine: AudioEngine, track_id: int, file_path: str):
        super().__init__()
        self._engine   = engine
        self._track_id = track_id
        self._path     = file_path

    def run(self):
        ok = self._engine.load_file(self._track_id, self._path)
        self.finished.emit(self._track_id, ok, self._path)


class EQWorker(QThread):
    """ノブ変更時のEQ再生成をバックグラウンドで実行するスレッド。"""
    finished = pyqtSignal(int)  # track_id

    def __init__(self, engine: AudioEngine, track_id: int, params):
        super().__init__()
        self._engine   = engine
        self._track_id = track_id
        self._params   = params

    def run(self):
        self._engine.update_eq(self._track_id, self._params)
        self.finished.emit(self._track_id)


class EffectWorker(QThread):
    """update_effectをバックグラウンドで実行するスレッド。"""
    finished = pyqtSignal(int)  # track_id

    def __init__(self, engine: AudioEngine, track_id: int,
                 preset_name: str, enabled: bool):
        super().__init__()
        self._engine      = engine
        self._track_id    = track_id
        self._preset_name = preset_name
        self._enabled     = enabled

    def run(self):
        self._engine.update_effect(self._track_id, self._preset_name, self._enabled)
        self.finished.emit(self._track_id)


# ===========================================================================
# マーカーバーウィジェット
# ===========================================================================
class MarkerBar(QWidget):
    """
    タイムラインマーカーを表示するバー。
    - マーカーを三角形のフラグで表示する。
    - クリックでそのマーカー位置にジャンプ（marker_clickedシグナル）。
    - 右クリックでコンテキストメニュー（削除・名前変更）。
    """
    marker_clicked = pyqtSignal(float)          # ジャンプ先（秒）
    marker_delete_requested = pyqtSignal(int)   # 削除要求（marker_id）
    marker_rename_requested = pyqtSignal(int, str)  # 名前変更要求（marker_id, new_label）

    MARKER_COLOR   = "#ffd700"   # 通常マーカー色（ゴールド）
    HOVER_COLOR    = "#ffffff"   # ホバー時の色
    FLAG_W         = 10          # フラグ三角形の幅
    FLAG_H         = 10          # フラグ三角形の高さ

    def __init__(self, parent=None):
        super().__init__(parent)
        self._markers: list = []          # Markerオブジェクトのリスト
        self._duration_sec: float = 0.0
        self._hovered_id: int = -1
        # Phase 22: ループ範囲の表示状態
        self._loop_enabled: bool = False
        self._loop_start_sec: float = 0.0
        self._loop_end_sec: float = 0.0
        self.setMinimumHeight(18)
        self.setMaximumHeight(18)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_markers(self, markers: list, duration_sec: float):
        """マーカーリストと総時間を更新して再描画する。"""
        self._markers = list(markers)
        self._duration_sec = max(0.0, duration_sec)
        self.update()

    def set_duration(self, duration_sec: float):
        self._duration_sec = max(0.0, duration_sec)
        self.update()

    def set_loop_range(self, enabled: bool, start_sec: float = 0.0, end_sec: float = 0.0):
        """ループ範囲の表示状態を設定する。"""
        self._loop_enabled = bool(enabled and end_sec > start_sec)
        self._loop_start_sec = max(0.0, start_sec)
        self._loop_end_sec = max(0.0, end_sec)
        self.update()

    def _time_to_x(self, time_sec: float) -> int:
        if self._duration_sec <= 0:
            return 0
        w = self.width() - 4
        return 2 + int(time_sec / self._duration_sec * w)

    def _marker_at(self, x: int, y: int) -> int:
        """クリック位置に最も近いマーカーIDを返す（なければ-1）。"""
        for m in self._markers:
            mx = self._time_to_x(m.time_sec)
            if abs(x - mx) <= self.FLAG_W:
                return m.marker_id
        return -1

    def mousePressEvent(self, event):
        mid = self._marker_at(event.x(), event.y())
        if event.button() == Qt.LeftButton and mid >= 0:
            m = next((mk for mk in self._markers if mk.marker_id == mid), None)
            if m:
                self.marker_clicked.emit(m.time_sec)
        elif event.button() == Qt.RightButton and mid >= 0:
            self._show_context_menu(mid, event.globalPos())

    def mouseMoveEvent(self, event):
        mid = self._marker_at(event.x(), event.y())
        if mid != self._hovered_id:
            self._hovered_id = mid
            self.update()
            if mid >= 0:
                m = next((mk for mk in self._markers if mk.marker_id == mid), None)
                if m:
                    self.setToolTip(f"{m.get_display_label()}")
            else:
                self.setToolTip("")

    def leaveEvent(self, event):
        self._hovered_id = -1
        self.update()

    def _show_context_menu(self, marker_id: int, global_pos):
        from PyQt5.QtWidgets import QMenu, QAction
        m = next((mk for mk in self._markers if mk.marker_id == marker_id), None)
        if not m:
            return
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background: #222; color: #fff; border: 1px solid #444; }
            QMenu::item:selected { background: #444; }
        """)
        rename_act = menu.addAction(f"名前を変更: {m.get_display_label()}")
        delete_act = menu.addAction("削除")
        act = menu.exec_(global_pos)
        if act == rename_act:
            from PyQt5.QtWidgets import QInputDialog
            new_label, ok = QInputDialog.getText(
                self, "マーカー名の変更",
                "新しい名前を入力してください:",
                text=m.label
            )
            if ok:
                self.marker_rename_requested.emit(marker_id, new_label)
        elif act == delete_act:
            self.marker_delete_requested.emit(marker_id)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # 背景
        painter.fillRect(0, 0, w, h, QColor("#111111"))

        # 中央の細い横線
        painter.setPen(QPen(QColor("#333333"), 1))
        painter.drawLine(2, h // 2, w - 2, h // 2)

        # Phase 22: ループ範囲をシアンで強調表示
        if self._loop_enabled and self._duration_sec > 0:
            lx1 = self._time_to_x(self._loop_start_sec)
            lx2 = self._time_to_x(self._loop_end_sec)
            loop_fill = QColor("#00c8d7")
            loop_fill.setAlpha(65)
            painter.fillRect(min(lx1, lx2), 1, abs(lx2 - lx1), h - 2, loop_fill)
            loop_pen = QPen(QColor("#00d8e8"), 2)
            painter.setPen(loop_pen)
            painter.drawLine(lx1, 0, lx1, h)
            painter.drawLine(lx2, 0, lx2, h)
            painter.setFont(QFont("Arial", 8, QFont.Bold))
            painter.setPen(QPen(QColor("#7df7ff"), 1))
            painter.drawText(lx1 + 3, 0, max(0, lx2 - lx1 - 6), h,
                             Qt.AlignCenter, "↻ LOOP")

        if self._duration_sec <= 0:
            painter.end()
            return

        for m in self._markers:
            mx = self._time_to_x(m.time_sec)
            is_hovered = (m.marker_id == self._hovered_id)
            color = QColor(self.HOVER_COLOR if is_hovered else self.MARKER_COLOR)

            # 縦線
            painter.setPen(QPen(color, 1))
            painter.drawLine(mx, 0, mx, h)

            # 三角形フラグ（上向き）
            from PyQt5.QtGui import QPolygon
            from PyQt5.QtCore import QPoint
            triangle = QPolygon([
                QPoint(mx, h - 2),
                QPoint(mx - self.FLAG_W // 2, h - 2 - self.FLAG_H),
                QPoint(mx + self.FLAG_W // 2, h - 2 - self.FLAG_H),
            ])
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.NoPen)
            painter.drawPolygon(triangle)

            # ラベル（ホバー時のみ表示）
            if is_hovered:
                label = m.get_display_label()
                painter.setFont(QFont("Arial", 8))
                painter.setPen(QPen(QColor("#ffffff"), 1))
                lx = min(mx + 4, w - 80)
                painter.drawText(lx, 0, 80, h, Qt.AlignLeft | Qt.AlignVCenter, label)

        painter.end()


# ===========================================================================
# シークバーウィジェット
# ===========================================================================
class TrackSeekBar(QWidget):
    """
    トラックの再生位置を表示・操作するシークバー。
    - ドラッグまたはクリックでシーク位置を指定。
    - 波形サムネイル（ピークリスト）を表示。
    - 現在位置・総時間をテキストで表示。
    """
    # シークしたときに発火（秒単位）
    seeked = pyqtSignal(float)

    def __init__(self, accent_color: str = "#4a90d9", parent=None):
        super().__init__(parent)
        self._accent = accent_color
        self._pos_sec: float = 0.0
        self._duration_sec: float = 0.0
        self._peaks: list = []          # 波形サムネイル（正規化済み 0.0、1.0）
        self._dragging: bool = False
        self.setMinimumHeight(28)
        self.setMaximumHeight(36)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("クリックまたはドラッグでシーク")

    def set_peaks(self, peaks: list):
        """波形サムネイルデータを設定する。"""
        self._peaks = peaks
        self.update()

    def set_duration(self, duration_sec: float):
        """総再生時間（秒）を設定する。"""
        self._duration_sec = max(0.0, duration_sec)
        self.update()

    def set_position(self, pos_sec: float):
        """現在再生位置（秒）を設定する。ドラッグ中は無視。"""
        if self._dragging:
            return
        self._pos_sec = max(0.0, pos_sec)
        self.update()

    @staticmethod
    def _fmt_sec(sec: float) -> str:
        m = int(sec // 60)
        s = sec % 60
        return f"{m}:{s:04.1f}"

    def _pos_to_x(self, pos_sec: float) -> int:
        if self._duration_sec <= 0:
            return 0
        w = self.width() - 4
        return 2 + int(pos_sec / self._duration_sec * w)

    def _x_to_pos(self, x: int) -> float:
        if self._duration_sec <= 0:
            return 0.0
        w = self.width() - 4
        ratio = max(0.0, min(1.0, (x - 2) / w))
        return ratio * self._duration_sec

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._pos_sec = self._x_to_pos(event.x())
            self.update()
            self.seeked.emit(self._pos_sec)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._pos_sec = self._x_to_pos(event.x())
            self.update()
            self.seeked.emit(self._pos_sec)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        margin_x = 2
        bar_y = h - 8
        bar_h = 5
        wave_h = bar_y - 2

        # 背景
        painter.fillRect(0, 0, w, h, QColor("#1a1a1a"))

        # 波形サムネイル
        if self._peaks:
            n = len(self._peaks)
            wave_color = QColor(self._accent)
            wave_color.setAlpha(80)
            played_color = QColor(self._accent)
            played_color.setAlpha(160)
            pos_ratio = (self._pos_sec / self._duration_sec
                         if self._duration_sec > 0 else 0.0)
            for i, peak in enumerate(self._peaks):
                bx = margin_x + int(i * (w - margin_x * 2) / n)
                bw = max(1, int((w - margin_x * 2) / n) - 1)
                bh = max(1, int(peak * wave_h))
                by = wave_h - bh
                ratio_i = i / n
                c = played_color if ratio_i <= pos_ratio else wave_color
                painter.fillRect(bx, by, bw, bh, c)

        # バー軌道（グレー）
        painter.fillRect(margin_x, bar_y, w - margin_x * 2, bar_h, QColor("#333333"))

        # 再生済み部分（アクセント色）
        if self._duration_sec > 0:
            played_w = int(self._pos_sec / self._duration_sec * (w - margin_x * 2))
            if played_w > 0:
                painter.fillRect(margin_x, bar_y, played_w, bar_h, QColor(self._accent))

        # ツマミ
        thumb_x = self._pos_to_x(self._pos_sec)
        thumb_r = 5
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(QPen(QColor(self._accent), 1.5))
        painter.drawEllipse(thumb_x - thumb_r, bar_y + bar_h // 2 - thumb_r,
                            thumb_r * 2, thumb_r * 2)

        # 時間テキスト
        painter.setFont(QFont("Arial", 7))
        painter.setPen(QPen(QColor("#888888"), 1))
        pos_str = self._fmt_sec(self._pos_sec)
        dur_str = self._fmt_sec(self._duration_sec)
        painter.drawText(margin_x + 2, 0, 60, wave_h,
                         Qt.AlignLeft | Qt.AlignVCenter, pos_str)
        painter.drawText(w - 62, 0, 60, wave_h,
                         Qt.AlignRight | Qt.AlignVCenter, dur_str)

        painter.end()


# ===========================================================================
# レベルメーターウィジェット
# ===========================================================================
class LevelMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0.0
        self._peak = 0.0
        self._peak_hold = 0
        self.setMinimumSize(16, 120)
        self.setMaximumWidth(24)

    def set_level(self, level: float):
        self._level = max(0.0, min(1.0, level))
        if self._level >= self._peak:
            self._peak = self._level
            self._peak_hold = 30
        else:
            if self._peak_hold > 0:
                self._peak_hold -= 1
            else:
                self._peak = max(0.0, self._peak - 0.02)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        w, h = self.width(), self.height()
        margin = 2
        painter.fillRect(0, 0, w, h, QColor(Colors.METER_BG))
        bar_h = int(self._level * (h - margin * 2))
        if bar_h > 0:
            for y in range(h - margin - bar_h, h - margin):
                ratio = 1.0 - (y - margin) / (h - margin * 2)
                if ratio > 0.8:
                    color = QColor(Colors.METER_HIGH)
                elif ratio > 0.5:
                    color = QColor(Colors.METER_MID)
                else:
                    color = QColor(Colors.METER_LOW)
                painter.fillRect(margin, y, w - margin * 2, 1, color)
        if self._peak > 0.01:
            peak_y = int((1.0 - self._peak) * (h - margin * 2)) + margin
            painter.fillRect(margin, peak_y, w - margin * 2, 2, QColor("#ffffff"))
        painter.end()


# ===========================================================================
# VUメーターウィジェット（リアルタイムステレオ2ch）
# ===========================================================================
class VUMeterWidget(QWidget):
    """
    リアルタイムステレオ VU/ピークメーター。
    - 2ch（L/R）セグメント表示（緑/黄/赤）
    - dBスケール目盛（-60〜0 dB）
    - ピークホールドライン（白線）
    - クリップインジケーター（赤点滅）
    - ダブルクリックで拡大ウィンドウを開く
    """
    # dBスケール: -60dB 〜 0dB
    DB_MIN = -60.0
    DB_MAX =   0.0
    # セグメント数
    NUM_SEGS = 40

    def __init__(self, parent=None):
        super().__init__(parent)
        # RMS値（0.0～1.0）
        self._rms_l: float = 0.0
        self._rms_r: float = 0.0
        # ピークホールド（0.0～1.0）
        self._peak_l: float = 0.0
        self._peak_r: float = 0.0
        # ピークホールドカウンター（50msティック単位）
        self._peak_hold_l: int = 0
        self._peak_hold_r: int = 0
        PEAK_HOLD_TICKS = 60  # 3秒ホールド
        self._PEAK_HOLD_TICKS = PEAK_HOLD_TICKS
        # クリップフラグ
        self._clip_l: bool = False
        self._clip_r: bool = False
        # 拡大ウィンドウ参照
        self._expanded_win = None
        # クリップ点滅カウンター
        self._clip_blink: int = 0

        self.setMinimumSize(60, 200)
        self.setToolTip("ダブルクリックで拡大表示")

    # ------------------------------------------------------------------
    # データ更新
    # ------------------------------------------------------------------
    def update_vu(self, rms_l: float, rms_r: float,
                  peak_l: float, peak_r: float,
                  clip_l: bool, clip_r: bool):
        """VUメーター値を更新して再描画する。"""
        # RMSはスムージング
        self._rms_l = rms_l * 0.5 + self._rms_l * 0.5
        self._rms_r = rms_r * 0.5 + self._rms_r * 0.5

        # ピークホールド処理
        if peak_l >= self._peak_l:
            self._peak_l = peak_l
            self._peak_hold_l = self._PEAK_HOLD_TICKS
        else:
            if self._peak_hold_l > 0:
                self._peak_hold_l -= 1
            else:
                self._peak_l = max(0.0, self._peak_l - 0.01)

        if peak_r >= self._peak_r:
            self._peak_r = peak_r
            self._peak_hold_r = self._PEAK_HOLD_TICKS
        else:
            if self._peak_hold_r > 0:
                self._peak_hold_r -= 1
            else:
                self._peak_r = max(0.0, self._peak_r - 0.01)

        # クリップフラグ（一度セットされたらクリックまで保持）
        if clip_l:
            self._clip_l = True
            self._clip_blink = 0
        if clip_r:
            self._clip_r = True
            self._clip_blink = 0
        if self._clip_l or self._clip_r:
            self._clip_blink += 1

        self.update()
        # 拡大ウィンドウも同期更新
        if self._expanded_win is not None and not self._expanded_win.isHidden():
            self._expanded_win.update_vu(
                self._rms_l, self._rms_r,
                self._peak_l, self._peak_r,
                self._clip_l, self._clip_r
            )

    def reset_clip(self):
        """クリップフラグをリセットする。"""
        self._clip_l = False
        self._clip_r = False
        self._clip_blink = 0
        self.update()
        if self._expanded_win is not None and not self._expanded_win.isHidden():
            self._expanded_win.reset_clip()

    # ------------------------------------------------------------------
    # ダブルクリックで拡大ウィンドウを開く
    # ------------------------------------------------------------------
    def mouseDoubleClickEvent(self, event):
        if self._expanded_win is not None and not self._expanded_win.isHidden():
            self._expanded_win.raise_()
            self._expanded_win.activateWindow()
            return
        win = ExpandedVUMeterWindow(title="VU Meter - MASTER")
        win.update_vu(
            self._rms_l, self._rms_r,
            self._peak_l, self._peak_r,
            self._clip_l, self._clip_r
        )
        win.clip_reset_requested.connect(self.reset_clip)
        win.destroyed.connect(self._on_expanded_closed)
        self._expanded_win = win
        win.show()

    def _on_expanded_closed(self):
        self._expanded_win = None

    # ------------------------------------------------------------------
    # 描画（小型インライン表示）
    # ------------------------------------------------------------------
    @staticmethod
    def _lin_to_db(lin: float) -> float:
        """0.0～1.0 の線形振幅を dB に変換。"""
        if lin <= 0.0:
            return -120.0
        return 20.0 * math.log10(lin)

    @staticmethod
    def _db_to_ratio(db: float, db_min: float, db_max: float) -> float:
        """dB 値を 0.0～1.0 の表示割合に変換。"""
        return max(0.0, min(1.0, (db - db_min) / (db_max - db_min)))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        # 背景
        painter.fillRect(0, 0, w, h, QColor("#0d1117"))

        # 内側マージン
        mx, my = 2, 4
        # dB目盛エリア幅（12px）
        db_label_w = 14
        # クリップインジケーター高さ
        clip_h = 8
        # メーターエリア
        meter_top = my + clip_h + 2
        meter_bot = h - my - 16  # 下のdBラベル用スペース
        meter_h = max(1, meter_bot - meter_top)
        # L/Rチャンネル幅
        avail_w = w - mx * 2 - db_label_w
        ch_gap = 2
        ch_w = max(4, (avail_w - ch_gap) // 2)
        lx = mx + db_label_w
        rx = lx + ch_w + ch_gap

        seg_gap = 1
        seg_h = max(1, (meter_h - seg_gap * (self.NUM_SEGS - 1)) // self.NUM_SEGS)

        def draw_channel(x: int, rms: float, peak: float, clip: bool):
            db_rms  = self._lin_to_db(rms)
            db_peak = self._lin_to_db(peak)
            rms_ratio  = self._db_to_ratio(db_rms,  self.DB_MIN, self.DB_MAX)
            peak_ratio = self._db_to_ratio(db_peak, self.DB_MIN, self.DB_MAX)
            fill_segs = int(rms_ratio * self.NUM_SEGS)

            for seg in range(self.NUM_SEGS):
                seg_ratio = seg / self.NUM_SEGS
                sy = meter_bot - (seg + 1) * (seg_h + seg_gap) + seg_gap

                if seg < fill_segs:
                    if seg_ratio >= 0.875:  # 上位12.5%: 赤
                        color = QColor("#e74c3c")
                    elif seg_ratio >= 0.625:  # 中位25%: 黄
                        color = QColor("#f39c12")
                    else:  # 下位62.5%: 緑
                        color = QColor("#27ae60")
                else:
                    # 暗色背景セグメント
                    if seg_ratio >= 0.875:
                        color = QColor("#3a1010")
                    elif seg_ratio >= 0.625:
                        color = QColor("#3a2a00")
                    else:
                        color = QColor("#0a2010")
                painter.fillRect(x, sy, ch_w, seg_h, color)

            # ピークホールドライン
            if peak > 0.001:
                py = meter_bot - int(peak_ratio * meter_h)
                painter.fillRect(x, py, ch_w, 2, QColor("#ffffff"))

        draw_channel(lx, self._rms_l, self._peak_l, self._clip_l)
        draw_channel(rx, self._rms_r, self._peak_r, self._clip_r)

        # クリップインジケーター（L/R個別）
        if self._clip_l:
            blink_on = (self._clip_blink // 5) % 2 == 0
            c = QColor("#ff2222") if blink_on else QColor("#5a1010")
            painter.fillRect(lx, my, ch_w, clip_h, c)
        else:
            painter.fillRect(lx, my, ch_w, clip_h, QColor("#1a1a1a"))

        if self._clip_r:
            blink_on = (self._clip_blink // 5) % 2 == 0
            c = QColor("#ff2222") if blink_on else QColor("#5a1010")
            painter.fillRect(rx, my, ch_w, clip_h, c)
        else:
            painter.fillRect(rx, my, ch_w, clip_h, QColor("#1a1a1a"))

        # dB目盛ラベル（-60, -40, -20, -12, -6, -3, 0）
        painter.setFont(QFont("Arial", 6))
        painter.setPen(QPen(QColor("#666666"), 1))
        for db_mark in [0, -3, -6, -12, -20, -40, -60]:
            ratio = self._db_to_ratio(db_mark, self.DB_MIN, self.DB_MAX)
            y = int(meter_bot - ratio * meter_h)
            label = str(db_mark) if db_mark != 0 else "0"
            painter.drawText(mx, y - 4, db_label_w - 2, 10,
                             Qt.AlignRight | Qt.AlignVCenter, label)
            # グリッド線
            painter.setPen(QPen(QColor("#2a2a2a"), 1))
            painter.drawLine(lx, y, rx + ch_w, y)
            painter.setPen(QPen(QColor("#666666"), 1))

        # チャンネルラベル（L / R）
        painter.setFont(QFont("Arial", 7))
        painter.setPen(QPen(QColor("#888888"), 1))
        painter.drawText(lx, meter_bot + 2, ch_w, 12, Qt.AlignCenter, "L")
        painter.drawText(rx, meter_bot + 2, ch_w, 12, Qt.AlignCenter, "R")

        painter.end()


# ===========================================================================
# 拡大VUメーターウィンドウ
# ===========================================================================
class ExpandedVUMeterWindow(QWidget):
    """
    VUMeterWidgetをダブルクリックしたときに開く拡大表示ウィンドウ。
    400×600px、dB目盛・チャンネルラベル付き。
    """
    clip_reset_requested = pyqtSignal()

    DB_MIN = -60.0
    DB_MAX =   0.0
    NUM_SEGS = 60

    def __init__(self, title: str = "VU Meter", parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(title)
        self.resize(400, 600)
        self.setMinimumSize(260, 400)
        self.setStyleSheet("background-color: #0d1117;")
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._rms_l: float = 0.0
        self._rms_r: float = 0.0
        self._peak_l: float = 0.0
        self._peak_r: float = 0.0
        self._clip_l: bool = False
        self._clip_r: bool = False
        self._clip_blink: int = 0

        # クリップリセットボタン
        reset_btn = QPushButton("CLIP RESET", self)
        reset_btn.setGeometry(10, 10, 90, 22)
        reset_btn.setStyleSheet("""
            QPushButton {
                background-color: #3a1010; color: #e74c3c;
                border: 1px solid #c0392b; border-radius: 3px;
                font-size: 9px; font-weight: bold;
            }
            QPushButton:hover { background-color: #c0392b; color: #fff; }
        """)
        reset_btn.clicked.connect(self._on_clip_reset)

        # ピンボタン
        pin_btn = QPushButton("ピン", self)
        pin_btn.setGeometry(110, 10, 40, 22)
        pin_btn.setCheckable(True)
        pin_btn.setStyleSheet("""
            QPushButton { background-color: #222; color: #aaa;
                border: 1px solid #444; border-radius: 3px; font-size: 9px; }
            QPushButton:checked { background-color: #2c3e50; color: #4a90d9;
                border: 1px solid #4a90d9; }
        """)
        pin_btn.toggled.connect(lambda on: self.setWindowFlag(
            Qt.WindowStaysOnTopHint, on) or self.show())

    def update_vu(self, rms_l, rms_r, peak_l, peak_r, clip_l, clip_r):
        self._rms_l = rms_l
        self._rms_r = rms_r
        self._peak_l = peak_l
        self._peak_r = peak_r
        if clip_l:
            self._clip_l = True
        if clip_r:
            self._clip_r = True
        if self._clip_l or self._clip_r:
            self._clip_blink += 1
        self.update()

    def reset_clip(self):
        self._clip_l = False
        self._clip_r = False
        self._clip_blink = 0
        self.update()

    def _on_clip_reset(self):
        self.reset_clip()
        self.clip_reset_requested.emit()

    @staticmethod
    def _lin_to_db(lin: float) -> float:
        if lin <= 0.0:
            return -120.0
        return 20.0 * math.log10(lin)

    @staticmethod
    def _db_to_ratio(db: float, db_min: float, db_max: float) -> float:
        return max(0.0, min(1.0, (db - db_min) / (db_max - db_min)))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        painter.fillRect(0, 0, w, h, QColor("#0d1117"))

        # レイアウト定数
        mx = 4
        top_margin = 44  # CLIPインジケーター + タイトル用
        bot_margin = 24  # L/Rラベル用
        db_label_w = 28
        clip_h = 14
        meter_top = top_margin
        meter_bot = h - bot_margin
        meter_h = max(1, meter_bot - meter_top)
        avail_w = w - mx * 2 - db_label_w
        ch_gap = 6
        ch_w = max(8, (avail_w - ch_gap) // 2)
        lx = mx + db_label_w
        rx = lx + ch_w + ch_gap

        seg_gap = 2
        seg_h = max(2, (meter_h - seg_gap * (self.NUM_SEGS - 1)) // self.NUM_SEGS)

        def draw_channel(x: int, rms: float, peak: float, clip: bool):
            db_rms  = self._lin_to_db(rms)
            db_peak = self._lin_to_db(peak)
            rms_ratio  = self._db_to_ratio(db_rms,  self.DB_MIN, self.DB_MAX)
            peak_ratio = self._db_to_ratio(db_peak, self.DB_MIN, self.DB_MAX)
            fill_segs = int(rms_ratio * self.NUM_SEGS)

            for seg in range(self.NUM_SEGS):
                seg_ratio = seg / self.NUM_SEGS
                sy = meter_bot - (seg + 1) * (seg_h + seg_gap) + seg_gap

                if seg < fill_segs:
                    if seg_ratio >= 0.875:
                        color = QColor("#ff3333")
                    elif seg_ratio >= 0.625:
                        color = QColor("#ffaa00")
                    else:
                        color = QColor("#2ecc71")
                else:
                    if seg_ratio >= 0.875:
                        color = QColor("#3a1010")
                    elif seg_ratio >= 0.625:
                        color = QColor("#3a2a00")
                    else:
                        color = QColor("#0a2010")
                painter.fillRect(x, sy, ch_w, seg_h, color)

            # ピークホールドライン（白）
            if peak > 0.001:
                py = int(meter_bot - peak_ratio * meter_h)
                painter.fillRect(x, py, ch_w, 3, QColor("#ffffff"))

        draw_channel(lx, self._rms_l, self._peak_l, self._clip_l)
        draw_channel(rx, self._rms_r, self._peak_r, self._clip_r)

        # クリップインジケーター
        for clip_flag, x in [(self._clip_l, lx), (self._clip_r, rx)]:
            if clip_flag:
                blink_on = (self._clip_blink // 5) % 2 == 0
                c = QColor("#ff2222") if blink_on else QColor("#5a1010")
            else:
                c = QColor("#1a1a1a")
            painter.fillRect(x, top_margin - clip_h - 2, ch_w, clip_h, c)
            painter.setPen(QPen(QColor("#444"), 1))
            painter.setFont(QFont("Arial", 7, QFont.Bold))
            if clip_flag:
                painter.setPen(QPen(QColor("#fff"), 1))
            painter.drawText(x, top_margin - clip_h - 2, ch_w, clip_h,
                             Qt.AlignCenter, "CLIP")

        # dB目盛ラベル（詳細）
        painter.setFont(QFont("Arial", 8))
        for db_mark in [0, -3, -6, -9, -12, -18, -24, -36, -48, -60]:
            ratio = self._db_to_ratio(db_mark, self.DB_MIN, self.DB_MAX)
            y = int(meter_bot - ratio * meter_h)
            painter.setPen(QPen(QColor("#555555"), 1))
            painter.drawText(mx, y - 6, db_label_w - 4, 12,
                             Qt.AlignRight | Qt.AlignVCenter, str(db_mark))
            painter.setPen(QPen(QColor("#2a2a2a"), 1, Qt.DotLine))
            painter.drawLine(lx, y, rx + ch_w, y)

        # L / R ラベル
        painter.setFont(QFont("Arial", 10, QFont.Bold))
        painter.setPen(QPen(QColor("#aaaaaa"), 1))
        painter.drawText(lx, meter_bot + 4, ch_w, 18, Qt.AlignCenter, "L")
        painter.drawText(rx, meter_bot + 4, ch_w, 18, Qt.AlignCenter, "R")

        # 現在値表示（dB）
        db_l = self._lin_to_db(self._rms_l)
        db_r = self._lin_to_db(self._rms_r)
        db_pl = self._lin_to_db(self._peak_l)
        db_pr = self._lin_to_db(self._peak_r)
        painter.setFont(QFont("Arial", 8))
        painter.setPen(QPen(QColor("#888888"), 1))
        info_y = top_margin - clip_h - 18
        painter.drawText(lx, info_y, ch_w * 2 + ch_gap, 14, Qt.AlignCenter,
                         f"L: {db_l:+.1f}dB  R: {db_r:+.1f}dB  "
                         f"Peak L: {db_pl:+.1f}  R: {db_pr:+.1f}")

        painter.end()


# ===========================================================================
# アナログ针式VUメーターウィジェット
# ===========================================================================
class AnalogVUMeterWidget(QWidget):
    """
    アナログ针式VUメーター。
    小型インライン表示用（2ch L/R并列）。
    ダブルクリックで拡大ウィンドウを開く。
    """
    # VUメーターのdBスケール（実障のVU視覆角に対応）
    # -20 VU 〜 +3 VU、角度範囲: -60度 〜 +60度
    DB_MIN = -20.0
    DB_MAX =  3.0
    ANGLE_MIN = -65.0   # -20VU 側の角度（度）
    ANGLE_MAX =  65.0   # +3VU 側の角度（度）
    # ピークホールドティック数（50ms単位）
    PEAK_HOLD_TICKS = 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rms_l: float = 0.0
        self._rms_r: float = 0.0
        self._needle_l: float = self.ANGLE_MIN  # 現在の针角度（Lch）
        self._needle_r: float = self.ANGLE_MIN  # 現在の针角度（Rch）
        self._peak_l: float = 0.0
        self._peak_r: float = 0.0
        self._peak_hold_l: int = 0
        self._peak_hold_r: int = 0
        self._clip_l: bool = False
        self._clip_r: bool = False
        self._clip_blink: int = 0
        self._expanded_win = None
        self.setMinimumSize(120, 80)
        self.setToolTip("ダブルクリックで拡大表示")

    @staticmethod
    def _lin_to_db(lin: float) -> float:
        if lin <= 0.0:
            return -120.0
        return 20.0 * math.log10(lin)

    @staticmethod
    def _db_to_angle(db: float, db_min: float, db_max: float,
                     angle_min: float, angle_max: float) -> float:
        ratio = max(0.0, min(1.0, (db - db_min) / (db_max - db_min)))
        return angle_min + ratio * (angle_max - angle_min)

    def update_vu(self, rms_l: float, rms_r: float,
                  peak_l: float, peak_r: float,
                  clip_l: bool, clip_r: bool):
        """VU値を更新し针をアニメーションする。"""
        self._rms_l = rms_l
        self._rms_r = rms_r

        # 针角度はスムージング（慣性の遅いVU针の展漫を表現）
        db_l = self._lin_to_db(rms_l)
        db_r = self._lin_to_db(rms_r)
        target_l = self._db_to_angle(db_l, self.DB_MIN, self.DB_MAX,
                                     self.ANGLE_MIN, self.ANGLE_MAX)
        target_r = self._db_to_angle(db_r, self.DB_MIN, self.DB_MAX,
                                     self.ANGLE_MIN, self.ANGLE_MAX)
        # VU针の慣性: 上昇は速く、下降は遅い
        if target_l > self._needle_l:
            self._needle_l += (target_l - self._needle_l) * 0.35
        else:
            self._needle_l += (target_l - self._needle_l) * 0.12
        if target_r > self._needle_r:
            self._needle_r += (target_r - self._needle_r) * 0.35
        else:
            self._needle_r += (target_r - self._needle_r) * 0.12

        # ピークホールド
        if peak_l >= self._peak_l:
            self._peak_l = peak_l
            self._peak_hold_l = self.PEAK_HOLD_TICKS
        else:
            if self._peak_hold_l > 0:
                self._peak_hold_l -= 1
            else:
                self._peak_l = max(0.0, self._peak_l - 0.015)

        if peak_r >= self._peak_r:
            self._peak_r = peak_r
            self._peak_hold_r = self.PEAK_HOLD_TICKS
        else:
            if self._peak_hold_r > 0:
                self._peak_hold_r -= 1
            else:
                self._peak_r = max(0.0, self._peak_r - 0.015)

        if clip_l:
            self._clip_l = True
            self._clip_blink = 0
        if clip_r:
            self._clip_r = True
            self._clip_blink = 0
        if self._clip_l or self._clip_r:
            self._clip_blink += 1

        self.update()
        if self._expanded_win is not None and not self._expanded_win.isHidden():
            self._expanded_win.update_vu(
                rms_l, rms_r, peak_l, peak_r, clip_l, clip_r
            )

    def reset_clip(self):
        self._clip_l = False
        self._clip_r = False
        self._clip_blink = 0
        self.update()
        if self._expanded_win is not None and not self._expanded_win.isHidden():
            self._expanded_win.reset_clip()

    def mouseDoubleClickEvent(self, event):
        if self._expanded_win is not None and not self._expanded_win.isHidden():
            self._expanded_win.raise_()
            self._expanded_win.activateWindow()
            return
        win = ExpandedAnalogVUWindow(title="VU Meter - MASTER (Analog)")
        win.update_vu(self._rms_l, self._rms_r,
                      self._peak_l, self._peak_r,
                      self._clip_l, self._clip_r)
        win.destroyed.connect(self._on_expanded_closed)
        self._expanded_win = win
        win.show()

    def _on_expanded_closed(self):
        self._expanded_win = None

    def _draw_vu_face(self, painter, cx, cy, radius, needle_angle,
                      peak_angle, clip_flag, label, compact=True):
        """アナログVUメーターの顔を描画する共通メソッド。"""
        import math as _math

        # 文字盤背景（クリーム色）
        face_color = QColor("#f0e8c8")
        painter.setBrush(QBrush(face_color))
        painter.setPen(QPen(QColor("#333333"), 1.5))
        painter.drawEllipse(int(cx - radius), int(cy - radius),
                            int(radius * 2), int(radius * 2))

        # 目盛の定義
        # VUメーターの目盛: -20, -10, -7, -5, -3, 0, +3 (VU)
        marks = [
            (-20.0, "-20", False), (-10.0, "10", False), (-7.0, "7", False),
            (-5.0, "5", False),   (-3.0, "3", False),
            (0.0,  "0",  True),   (3.0,  "+3", True),
        ]
        tick_lengths_major = [
            (-20.0, True), (-10.0, True), (-7.0, False), (-5.0, True),
            (-3.0, True), (0.0, True), (3.0, True)
        ]
        # 小刻目位置
        minor_marks_db = [-15.0, -12.0, -9.0, -6.0, -4.0, -2.0, -1.0, 1.0, 2.0]

        font_size = max(5, int(radius * (0.13 if compact else 0.16)))
        painter.setFont(QFont("Arial", font_size, QFont.Bold))

        for db_val, label_str, is_red in marks:
            angle_deg = self._db_to_angle(db_val, self.DB_MIN, self.DB_MAX,
                                          self.ANGLE_MIN, self.ANGLE_MAX)
            # 角度は下から時計回り方向、中心から上向きが0度
            rad = _math.radians(angle_deg - 90)
            tick_outer = radius * 0.92
            tick_inner = radius * 0.75
            x1 = cx + tick_outer * _math.cos(rad)
            y1 = cy + tick_outer * _math.sin(rad)
            x2 = cx + tick_inner * _math.cos(rad)
            y2 = cy + tick_inner * _math.sin(rad)
            color = QColor("#cc0000") if is_red else QColor("#222222")
            painter.setPen(QPen(color, 1.5 if compact else 2.0))
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))

            # ラベル位置
            label_r = radius * 0.60
            lx = cx + label_r * _math.cos(rad)
            ly = cy + label_r * _math.sin(rad)
            lw = int(radius * 0.35)
            lh = int(radius * 0.22)
            painter.setPen(QPen(color, 1))
            painter.drawText(int(lx - lw / 2), int(ly - lh / 2), lw, lh,
                             Qt.AlignCenter, label_str)

        # 小刻目
        painter.setPen(QPen(QColor("#555555"), 1.0))
        for db_val in minor_marks_db:
            angle_deg = self._db_to_angle(db_val, self.DB_MIN, self.DB_MAX,
                                          self.ANGLE_MIN, self.ANGLE_MAX)
            rad = _math.radians(angle_deg - 90)
            tick_outer = radius * 0.92
            tick_inner = radius * 0.83
            x1 = cx + tick_outer * _math.cos(rad)
            y1 = cy + tick_outer * _math.sin(rad)
            x2 = cx + tick_inner * _math.cos(rad)
            y2 = cy + tick_inner * _math.sin(rad)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        # 赤帯域（0dB〜+3dB）
        red_start_db = 0.0
        red_end_db = 3.0
        a_start = self._db_to_angle(red_start_db, self.DB_MIN, self.DB_MAX,
                                    self.ANGLE_MIN, self.ANGLE_MAX)
        a_end = self._db_to_angle(red_end_db, self.DB_MIN, self.DB_MAX,
                                  self.ANGLE_MIN, self.ANGLE_MAX)
        arc_r = radius * 0.88
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)
        # QtのdrawArcは16分の1度単位、開始角度は3時から反時計回り
        qt_start = int((90 - a_end) * 16)
        qt_span  = int((a_end - a_start) * 16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#cc0000")))
        arc_thick = max(3, int(radius * 0.10))
        for r_off in range(arc_thick):
            r_cur = arc_r - r_off
            if r_cur <= 0:
                break
            arc_rect_cur = QRectF(cx - r_cur, cy - r_cur, r_cur * 2, r_cur * 2)
            painter.setPen(QPen(QColor("#cc0000"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(arc_rect_cur, qt_start, qt_span)

        # ピークホールドマーカー（青線）
        if peak_angle > self.ANGLE_MIN:
            peak_rad = _math.radians(peak_angle - 90)
            pk_outer = radius * 0.93
            pk_inner = radius * 0.70
            pkx1 = cx + pk_outer * _math.cos(peak_rad)
            pky1 = cy + pk_outer * _math.sin(peak_rad)
            pkx2 = cx + pk_inner * _math.cos(peak_rad)
            pky2 = cy + pk_inner * _math.sin(peak_rad)
            painter.setPen(QPen(QColor("#0066cc"), 2))
            painter.drawLine(int(pkx1), int(pky1), int(pkx2), int(pky2))

        # 针
        needle_rad = _math.radians(needle_angle - 90)
        needle_len = radius * 0.85
        nx = cx + needle_len * _math.cos(needle_rad)
        ny = cy + needle_len * _math.sin(needle_rad)
        painter.setPen(QPen(QColor("#111111"), max(1, int(radius * 0.04))))
        painter.drawLine(int(cx), int(cy), int(nx), int(ny))
        # 针の中心点
        pivot_r = max(3, int(radius * 0.07))
        painter.setBrush(QBrush(QColor("#333333")))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(int(cx - pivot_r), int(cy - pivot_r),
                            pivot_r * 2, pivot_r * 2)

        # PEAK LED（右上）— クリップ時のみ表示
        led_x = int(cx + radius * 0.62)
        led_y = int(cy - radius * 0.30)
        led_r = max(4, int(radius * 0.10))
        if clip_flag:
            blink_on = (self._clip_blink // 5) % 2 == 0
            led_color = QColor("#ff2222") if blink_on else QColor("#880000")
            painter.setBrush(QBrush(led_color))
            painter.setPen(QPen(QColor("#222222"), 1))
            painter.drawEllipse(led_x - led_r, led_y - led_r, led_r * 2, led_r * 2)
            # PEAKラベル
            painter.setFont(QFont("Arial", max(4, int(radius * 0.09))))
            painter.setPen(QPen(QColor("#cc0000"), 1))
            painter.drawText(led_x - led_r * 2, led_y + led_r + 1,
                             led_r * 4, 10, Qt.AlignCenter, "PEAK")

        # チャンネルラベル（VU文字の代わりに）
        painter.setFont(QFont("Arial", max(6, int(radius * 0.14)), QFont.Bold))
        painter.setPen(QPen(QColor("#333333"), 1))
        painter.drawText(int(cx - radius * 0.2), int(cy + radius * 0.05),
                         int(radius * 0.4), int(radius * 0.25),
                         Qt.AlignCenter, label)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # 背景
        painter.fillRect(0, 0, w, h, QColor("#1a1a1a"))

        # 2つのVU表示（L/R带平列）
        margin = 4
        half_w = (w - margin * 3) // 2
        radius_l = min(half_w, h - margin * 2) * 0.48
        radius_r = radius_l
        cx_l = margin + half_w // 2
        cx_r = margin * 2 + half_w + half_w // 2
        cy = int(h * 0.55)

        # Lチャンネル
        peak_angle_l = self._db_to_angle(
            self._lin_to_db(self._peak_l), self.DB_MIN, self.DB_MAX,
            self.ANGLE_MIN, self.ANGLE_MAX
        )
        self._draw_vu_face(painter, cx_l, cy, radius_l,
                           self._needle_l, peak_angle_l, self._clip_l, "L")

        # Rチャンネル
        peak_angle_r = self._db_to_angle(
            self._lin_to_db(self._peak_r), self.DB_MIN, self.DB_MAX,
            self.ANGLE_MIN, self.ANGLE_MAX
        )
        self._draw_vu_face(painter, cx_r, cy, radius_r,
                           self._needle_r, peak_angle_r, self._clip_r, "R")

        painter.end()


# ===========================================================================
# 拡大アナログVUメーターウィンドウ
# ===========================================================================
class ExpandedAnalogVUWindow(QWidget):
    """
    AnalogVUMeterWidgetをダブルクリックしたときに開く拡大表示ウィンドウ。
    大型アナログVUメーターをリアルタイム表示。
    """
    def __init__(self, title: str = "VU Meter", parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(title)
        self.resize(600, 320)
        self.setMinimumSize(400, 220)
        self.setStyleSheet("background-color: #1a1a1a;")
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._meter = AnalogVUMeterWidget(self)
        self._meter.setGeometry(0, 40, 600, 260)

        # ピンボタン
        pin_btn = QPushButton("ピン", self)
        pin_btn.setGeometry(10, 8, 44, 22)
        pin_btn.setCheckable(True)
        pin_btn.setStyleSheet("""
            QPushButton { background-color: #222; color: #aaa;
                border: 1px solid #444; border-radius: 3px; font-size: 9px; }
            QPushButton:checked { background-color: #2c3e50; color: #4a90d9;
                border: 1px solid #4a90d9; }
        """)
        pin_btn.toggled.connect(lambda on: self.setWindowFlag(
            Qt.WindowStaysOnTopHint, on) or self.show())

    def update_vu(self, rms_l, rms_r, peak_l, peak_r, clip_l, clip_r):
        self._meter.update_vu(rms_l, rms_r, peak_l, peak_r, clip_l, clip_r)

    def reset_clip(self):
        self._meter.reset_clip()

    def resizeEvent(self, event):
        self._meter.setGeometry(0, 40, self.width(), self.height() - 40)


# ===========================================================================
# 縦フェーダーウィジェット
# ===========================================================================
class VerticalFader(QWidget):
    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None, max_value: int = 100, default_value: int = 80):
        super().__init__(parent)
        self._max_value = max_value
        self._default_value = default_value
        self._value = default_value
        self._dragging = False
        self._drag_start_y = 0
        self._drag_start_val = 0
        self.setMinimumSize(30, 160)
        self.setMaximumWidth(40)
        self.setCursor(Qt.SizeVerCursor)

    def get_value(self) -> int:
        return self._value

    def set_value(self, value: int, emit: bool = True):
        value = max(0, min(self._max_value, value))
        if value != self._value:
            self._value = value
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def set_range(self, min_value: int, max_value: int):
        """フェーダーの範囲を変更する（GEQモード切り替え用）。"""
        self._max_value = max_value
        self._value = max(min_value, min(max_value, self._value))
        self.update()

    def _value_to_y(self, value: int) -> int:
        h, handle_h = self.height(), 24
        return int((1.0 - value / self._max_value) * (h - handle_h)) + handle_h // 2

    def _y_to_value(self, y: int) -> int:
        h, handle_h = self.height(), 24
        ratio = 1.0 - (y - handle_h // 2) / (h - handle_h)
        return int(max(0.0, min(1.0, ratio)) * self._max_value)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_start_y = event.y()
            self._drag_start_val = self._value

    def mouseMoveEvent(self, event):
        if self._dragging:
            dy = event.y() - self._drag_start_y
            h, handle_h = self.height(), 24
            delta = int(-dy / (h - handle_h) * self._max_value)
            self.set_value(self._drag_start_val + delta)

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def set_geq_mode(self, enabled: bool):
        """GEQモードフラグ。Trueのときダブルクリックでセンター(50=±0dB)に戻る。"""
        self._geq_mode = enabled

    def mouseDoubleClickEvent(self, event):
        if getattr(self, '_geq_mode', False):
            # GEQモード中はセンター(50=±0dB)にリセット
            self.set_value(50)
        else:
            # 通常モードは従来通り
            self.set_value(self._default_value)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        handle_h, handle_w = 24, w - 8
        groove_x, groove_w = w // 2 - 2, 4
        painter.fillRect(groove_x, handle_h // 2, groove_w, h - handle_h, QColor(Colors.FADER_GROOVE))
        painter.setPen(QPen(QColor("#333333"), 1))
        for i in range(0, self._max_value + 1, self._max_value // 10):
            y = self._value_to_y(i)
            tick_w = 6 if i % (self._max_value // 2) == 0 else 4
            painter.drawLine(groove_x - tick_w // 2, y, groove_x + groove_w + tick_w // 2, y)
        handle_y = self._value_to_y(self._value) - handle_h // 2
        handle_color = QColor(Colors.FADER_ACTIVE if self._dragging else Colors.FADER_HANDLE)
        rect_x = (w - handle_w) // 2
        painter.setBrush(QBrush(handle_color))
        painter.setPen(QPen(QColor("#555555"), 1))
        painter.drawRoundedRect(rect_x, handle_y, handle_w, handle_h, 3, 3)
        painter.setPen(QPen(QColor("#888888"), 1))
        mid_y = handle_y + handle_h // 2
        painter.drawLine(rect_x + 4, mid_y, rect_x + handle_w - 4, mid_y)
        painter.end()


# ===========================================================================
# 1トラック分のウィジェット
# ===========================================================================
class TrackWidget(QFrame):
    file_load_requested = pyqtSignal(int)
    volume_changed      = pyqtSignal(int, float)
    pan_changed         = pyqtSignal(int, float)
    gain_changed        = pyqtSignal(int, float)  # (track_id, gain_db)
    mute_toggled        = pyqtSignal(int)
    solo_toggled        = pyqtSignal(int)
    eq_changed          = pyqtSignal(int, object)  # (track_id, EQParams)
    effect_changed      = pyqtSignal(int, str, bool)  # (track_id, preset_name, enabled)
    geq_band_changed    = pyqtSignal(int, float, float)  # (track_id, band_freq, gain_db)
    mic_toggled         = pyqtSignal(int)  # (track_id) - MICボタンクリック時
    aux_toggled         = pyqtSignal(int)  # (track_id) - AUXボタンクリック時
    xfade_assign_changed = pyqtSignal(int, str)  # (track_id, A/B/THRU)

    # キーボードショートカット（バンク内インデックス 0～7）
    KEY_LABELS = ["A / Z", "S / X", "D / C", "F / V", "G / B", "H / N", "J / M", "K / ,"]

    def __init__(self, track: TrackModel, bank: int = 0, parent=None):
        super().__init__(parent)
        self._track = track
        self._bank = bank
        # バンクに応じたアクセントカラー
        palette = Colors.ACCENT_COLORS_A if bank == 0 else Colors.ACCENT_COLORS_B
        # バンク内インデックス（0～7）でカラーを選択
        bank_index = track.track_id % 8
        self._accent = palette[bank_index % len(palette)]
        # GEQモード状態
        self._geq_band_freq = None
        self._geq_band_name = None
        self._setup_ui()

    def _setup_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(140)
        self.setMaximumWidth(180)
        self.setStyleSheet(f"""
            TrackWidget {{
                background-color: {Colors.BG_TRACK};
                border: 1px solid {Colors.BORDER};
                border-top: 3px solid {self._accent};
                border-radius: 4px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # トラック番号（グローバル番号 = track_id + 1）
        self._track_label = QLabel(f"TRACK {self._track.track_id + 1}")
        self._track_label.setAlignment(Qt.AlignCenter)
        self._track_label.setStyleSheet(f"color: {self._accent}; font-size: 11px; font-weight: bold; letter-spacing: 2px;")
        layout.addWidget(self._track_label)

        # ファイル名
        self._file_label = QLabel(self._track.get_file_display_name())
        self._file_label.setAlignment(Qt.AlignCenter)
        self._file_label.setWordWrap(False)
        self._file_label.setFixedHeight(28)
        self._file_label.setMaximumWidth(140)
        self._file_label.setTextInteractionFlags(Qt.NoTextInteraction)
        self._file_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 9px; padding: 2px;")
        self._file_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout.addWidget(self._file_label)

        # 読み込みボタン
        self._load_btn = QPushButton("LOAD")
        self._load_btn.setFixedHeight(24)
        self._load_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.BTN_LOAD}; color: {Colors.TEXT_PRIMARY};
                border: 1px solid #3a5068; border-radius: 3px;
                font-size: 10px; font-weight: bold; letter-spacing: 1px;
            }}
            QPushButton:hover {{ background-color: {Colors.BTN_LOAD_HOV}; }}
            QPushButton:pressed {{ background-color: #1a2530; }}
        """)
        self._load_btn.clicked.connect(lambda: self.file_load_requested.emit(self._track.track_id))
        layout.addWidget(self._load_btn)

        # ゲインコントロール（LOADボタンとEQの間）
        gain_layout = QVBoxLayout()
        gain_layout.setSpacing(2)
        gain_title_row = QHBoxLayout()
        gain_title = QLabel("GAIN")
        gain_title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        gain_title.setStyleSheet(f"color: {Colors.TEXT_LABEL}; font-size: 9px; letter-spacing: 1px;")
        self._gain_label = QLabel(self._format_gain(self._track.gain_db))
        self._gain_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._gain_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: 9px; font-weight: bold;")
        self._gain_reset_btn = QPushButton("0")
        self._gain_reset_btn.setFixedSize(18, 14)
        self._gain_reset_btn.setToolTip("Gainを0dBにリセット")
        self._gain_reset_btn.setStyleSheet("""
            QPushButton { background-color: #333; color: #aaa; border: 1px solid #555;
                          border-radius: 2px; font-size: 8px; }
            QPushButton:hover { background-color: #555; color: #fff; }
        """)
        self._gain_reset_btn.clicked.connect(self._on_gain_reset)
        gain_title_row.addWidget(gain_title)
        gain_title_row.addStretch()
        gain_title_row.addWidget(self._gain_label)
        gain_title_row.addWidget(self._gain_reset_btn)
        gain_layout.addLayout(gain_title_row)
        self._gain_slider = QSlider(Qt.Horizontal)
        self._gain_slider.setRange(-24, 24)
        self._gain_slider.setValue(int(round(self._track.gain_db)))
        self._gain_slider.setFixedHeight(18)
        self._gain_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{ height: 4px; background: {Colors.FADER_GROOVE}; border-radius: 2px; }}
            QSlider::handle:horizontal {{
                width: 12px; height: 12px; margin: -4px 0;
                background: #f0a030; border-radius: 6px; border: 1px solid #c07010;
            }}
            QSlider::sub-page:horizontal {{ background: #f0a030; border-radius: 2px; }}
            QSlider::add-page:horizontal {{ background: {Colors.FADER_GROOVE}; border-radius: 2px; }}
        """)
        self._gain_slider.valueChanged.connect(self._on_gain_changed)
        gain_layout.addWidget(self._gain_slider)
        layout.addLayout(gain_layout)

        # EQWidget（ゲインの下）
        eq_params = EQParams(
            low_gain_db=self._track.eq_low_gain,
            mid_gain_db=self._track.eq_mid_gain,
            mid_freq_hz=self._track.eq_mid_freq,
            mid_q=self._track.eq_mid_q,
            high_gain_db=self._track.eq_high_gain,
        )
        self._eq_widget = EQWidget(
            track_id=self._track.track_id,
            params=eq_params,
            accent=self._accent
        )
        self._eq_widget.eq_changed.connect(self.eq_changed)
        layout.addWidget(self._eq_widget)

        # ミュート / ソロ / MIC / AUX
        ms_layout = QHBoxLayout()
        ms_layout.setSpacing(3)
        self._mute_btn = QPushButton("M")
        self._mute_btn.setFixedSize(26, 22)
        self._solo_btn = QPushButton("S")
        self._solo_btn.setFixedSize(26, 22)
        self._mic_btn = QPushButton("MIC")
        self._mic_btn.setFixedSize(30, 22)
        self._mic_btn.setToolTip("マイク入力を割り当てる")
        self._aux_btn = QPushButton("AUX")
        self._aux_btn.setFixedSize(30, 22)
        self._aux_btn.setToolTip("AUX ON: このトラックにのみFXを適用")
        self._mute_btn.setStyleSheet(self._btn_style(False, is_mute=True))
        self._solo_btn.setStyleSheet(self._btn_style(False, is_mute=False))
        self._mic_btn.setStyleSheet(self._mic_btn_style(False))
        self._aux_btn.setStyleSheet(self._aux_btn_style(False))
        self._mute_btn.clicked.connect(lambda: self.mute_toggled.emit(self._track.track_id))
        self._solo_btn.clicked.connect(lambda: self.solo_toggled.emit(self._track.track_id))
        self._mic_btn.clicked.connect(lambda: self.mic_toggled.emit(self._track.track_id))
        self._aux_btn.clicked.connect(lambda: self.aux_toggled.emit(self._track.track_id))
        ms_layout.addStretch()
        ms_layout.addWidget(self._mute_btn)
        ms_layout.addWidget(self._solo_btn)
        ms_layout.addWidget(self._mic_btn)
        ms_layout.addWidget(self._aux_btn)
        ms_layout.addStretch()
        layout.addLayout(ms_layout)

        # Phase 25: X-FADER割当（A / B / THRU）
        xfade_assign_layout = QHBoxLayout()
        xfade_assign_layout.setSpacing(3)
        xfade_assign_label = QLabel("XF")
        xfade_assign_label.setFixedWidth(18)
        xfade_assign_label.setStyleSheet("color:#a99dff; font-size:8px; font-weight:bold;")
        self._xfade_assign_combo = QComboBox()
        self._xfade_assign_combo.addItem("THRU", "THRU")
        self._xfade_assign_combo.addItem("A", "A")
        self._xfade_assign_combo.addItem("B", "B")
        self._xfade_assign_combo.setCurrentIndex(0)
        self._xfade_assign_combo.setFixedHeight(19)
        self._xfade_assign_combo.setToolTip("X-FADER割当: THRU / A / B")
        self._xfade_assign_combo.setStyleSheet("""
            QComboBox { background:#242039; color:#dcd8ff; border:1px solid #5a4f92;
                        border-radius:2px; font-size:8px; font-weight:bold; padding:0 3px; }
            QComboBox::drop-down { border:none; width:14px; }
            QComboBox QAbstractItemView { background:#242039; color:#dcd8ff; font-size:8px; }
        """)
        self._xfade_assign_combo.currentTextChanged.connect(self._on_xfade_assign_changed)
        xfade_assign_layout.addStretch()
        xfade_assign_layout.addWidget(xfade_assign_label)
        xfade_assign_layout.addWidget(self._xfade_assign_combo)
        xfade_assign_layout.addStretch()
        layout.addLayout(xfade_assign_layout)

        # フェーダー + レベルメーター
        fader_meter_layout = QHBoxLayout()
        fader_meter_layout.setSpacing(4)
        self._fader = VerticalFader(max_value=100, default_value=80)
        self._fader.set_value(int(self._track.volume * 100), emit=False)
        self._fader.valueChanged.connect(self._on_fader_changed)
        self._meter = LevelMeter()
        fader_meter_layout.addStretch()
        fader_meter_layout.addWidget(self._fader)
        fader_meter_layout.addWidget(self._meter)
        fader_meter_layout.addStretch()
        layout.addLayout(fader_meter_layout)

        # 音量値表示
        self._vol_label = QLabel(f"{self._track.get_volume_percent()}%")
        self._vol_label.setAlignment(Qt.AlignCenter)
        self._vol_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: 12px; font-weight: bold;")
        layout.addWidget(self._vol_label)

        self._db_label = QLabel(self._track.get_volume_db())
        self._db_label.setAlignment(Qt.AlignCenter)
        self._db_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 9px;")
        layout.addWidget(self._db_label)

        # パンスライダー
        pan_layout = QVBoxLayout()
        pan_layout.setSpacing(2)
        pan_title = QLabel("PAN")
        pan_title.setAlignment(Qt.AlignCenter)
        pan_title.setStyleSheet(f"color: {Colors.TEXT_LABEL}; font-size: 9px; letter-spacing: 1px;")
        pan_layout.addWidget(pan_title)
        self._pan_slider = QSlider(Qt.Horizontal)
        self._pan_slider.setRange(-100, 100)
        self._pan_slider.setValue(int(self._track.pan * 100))
        self._pan_slider.setFixedHeight(20)
        self._pan_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{ height: 4px; background: {Colors.FADER_GROOVE}; border-radius: 2px; }}
            QSlider::handle:horizontal {{
                width: 14px; height: 14px; margin: -5px 0;
                background: {Colors.FADER_HANDLE}; border-radius: 7px; border: 1px solid #555;
            }}
            QSlider::sub-page:horizontal {{ background: {self._accent}; border-radius: 2px; }}
        """)
        self._pan_slider.valueChanged.connect(self._on_pan_changed)
        pan_layout.addWidget(self._pan_slider)
        self._pan_label = QLabel(self._track.get_pan_display())
        self._pan_label.setAlignment(Qt.AlignCenter)
        self._pan_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 9px;")
        pan_layout.addWidget(self._pan_label)
        layout.addLayout(pan_layout)

        # キーボードショートカット表示（バンク内インデックス 0〜7）
        bank_idx = self._track.track_id % 8
        key_label_text = self.KEY_LABELS[bank_idx] if bank_idx < len(self.KEY_LABELS) else ""
        key_label = QLabel(f"KEY: {key_label_text}")
        key_label.setAlignment(Qt.AlignCenter)
        key_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 8px; background-color: #1a1a1a; border-radius: 2px; padding: 2px;")
        layout.addWidget(key_label)

        # EQカーブ表示ウィジェット（KEYラベルの下）
        self._eq_curve = EQCurveView(accent_color=self._accent)
        self._eq_curve.set_track_label(f"Track {self._track.track_id}")
        layout.addWidget(self._eq_curve)

        # シークバー（EQカーブの下）
        self._seek_bar = TrackSeekBar(accent_color=self._accent)
        self._seek_bar.seeked.connect(self._on_seeked)
        layout.addWidget(self._seek_bar)
        layout.addStretch()

    def _btn_style(self, active: bool, is_mute: bool) -> str:
        if is_mute:
            bg = Colors.BTN_MUTE_ON if active else Colors.BTN_MUTE_OFF
            border = "#e67e22" if active else "#444"
            hover = "#d35400"
        else:
            bg = Colors.BTN_SOLO_ON if active else Colors.BTN_SOLO_OFF
            border = "#27ae60" if active else "#444"
            hover = "#1e8449"
        return f"""
            QPushButton {{
                background-color: {bg}; color: {Colors.TEXT_PRIMARY};
                border: 1px solid {border}; border-radius: 3px;
                font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {hover}; }}
        """

    def _mic_btn_style(self, active: bool) -> str:
        """MICボタンのスタイルを返す。active=Trueのとき点灯（青色）。"""
        bg = "#1a6fa8" if active else "#333333"
        border = "#4a90d9" if active else "#444"
        hover = "#1e8bc3" if active else "#555"
        return f"""
            QPushButton {{
                background-color: {bg}; color: {Colors.TEXT_PRIMARY};
                border: 1px solid {border}; border-radius: 3px;
                font-size: 9px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {hover}; }}
        """

    def _aux_btn_style(self, active: bool) -> str:
        """AUXボタンのスタイルを返す。active=Trueのとき点灯（パープル）。"""
        bg = "#6c3483" if active else "#333333"
        border = "#9b59b6" if active else "#444"
        hover = "#8e44ad" if active else "#555"
        return f"""
            QPushButton {{
                background-color: {bg}; color: {Colors.TEXT_PRIMARY};
                border: 1px solid {border}; border-radius: 3px;
                font-size: 9px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {hover}; }}
        """

    def update_aux_state(self, active: bool):
        """AUXボタンの点灯状態を更新する。"""
        self._aux_btn.setStyleSheet(self._aux_btn_style(active))
        self._aux_btn.setToolTip(
            "AUX ON: このトラックにのみFXを適用中" if active
            else "AUX ON: このトラックにのみFXを適用"
        )

    def _on_xfade_assign_changed(self, assign: str):
        assign = str(assign).upper()
        self._track.xfade_assign = assign
        self.xfade_assign_changed.emit(self._track.track_id, assign)

    def update_xfade_assign(self, assign: str):
        assign = str(assign).upper()
        if assign not in ("A", "B", "THRU"):
            assign = "THRU"
        index = self._xfade_assign_combo.findData(assign)
        self._xfade_assign_combo.blockSignals(True)
        self._xfade_assign_combo.setCurrentIndex(max(0, index))
        self._xfade_assign_combo.blockSignals(False)
        self._track.xfade_assign = assign

    def update_mic_state(self, active: bool, device_name: str = ""):
        """MICボタンの点灯状態を更新する。"""
        self._mic_btn.setStyleSheet(self._mic_btn_style(active))
        if active and device_name:
            self._mic_btn.setToolTip(f"マイク: {device_name}")
        else:
            self._mic_btn.setToolTip("マイク入力を割り当てる")

    def _on_fader_changed(self, value: int):
        # GEQモード中はフェーダーをGEQコントローラーとして使用
        if hasattr(self, '_geq_band_freq') and self._geq_band_freq is not None:
            # GEQモード: センター(50) = 0dB、範囲 0-100 → -15dB〜+15dB
            gain_db = (value - 50) / 50.0 * 15.0
            self._vol_label.setText(f"{gain_db:+.1f}dB")
            self._db_label.setText(self._geq_band_name)
            self.geq_band_changed.emit(self._track.track_id, self._geq_band_freq, gain_db)
            return
        vol = value / 100.0
        self._track.volume = vol
        self._vol_label.setText(f"{value}%")
        self._db_label.setText(self._track.get_volume_db())
        self.volume_changed.emit(self._track.track_id, vol)

    def enter_geq_mode(self, band_name: str, band_freq: float, current_gain_db: float):
        """フェーダーをGEQコントローラーに切り替える。"""
        self._geq_band_freq = band_freq
        self._geq_band_name = band_name
        # フェーダーの範囲を変更（センター50=0dB）
        self._fader.set_geq_mode(True)  # ダブルクリックで±0dBに戻るよう設定
        self._fader.blockSignals(True)
        self._fader.set_range(0, 100)
        # gain_db → フェーダー値に変換
        fader_val = int(current_gain_db / 15.0 * 50.0 + 50)
        fader_val = max(0, min(100, fader_val))
        self._fader.set_value(fader_val, emit=False)
        self._fader.blockSignals(False)
        # ラベルを更新
        self._vol_label.setText(f"{current_gain_db:+.1f}dB")
        self._db_label.setText(band_name)
        # ヘッダーのトラック名をバンド名に変更
        self._track_label.setText(band_name)
        self._track_label.setStyleSheet("color: #D4A017; font-size: 9px; font-weight: bold; letter-spacing: 1px;")

    def exit_geq_mode(self):
        """フェーダーを通常の音量コントローラーに戻す。"""
        self._geq_band_freq = None
        self._geq_band_name = None
        # GEQモードを解除（ダブルクリックを通常のデフォルト値に戻す）
        self._fader.set_geq_mode(False)
        # フェーダーの範囲を元に戻す
        self._fader.blockSignals(True)
        self._fader.set_range(0, 150)
        vol_val = int(self._track.volume * 100)
        self._fader.set_value(vol_val, emit=False)
        self._fader.blockSignals(False)
        # ラベルを元に戻す
        self._vol_label.setText(f"{vol_val}%")
        self._db_label.setText(self._track.get_volume_db())
        # ヘッダーのトラック名を元に戻す
        track_num = self._track.track_id + 1
        self._track_label.setText(f"TRACK {track_num}")
        self._track_label.setStyleSheet(f"color: {self._accent}; font-size: 10px; font-weight: bold; letter-spacing: 1px;")

    def _on_pan_changed(self, value: int):
        pan = value / 100.0
        self._track.pan = pan
        self._pan_label.setText(self._track.get_pan_display())
        self.pan_changed.emit(self._track.track_id, pan)

    @staticmethod
    def _format_gain(gain_db: float) -> str:
        """gain_dbを表示用文字列に変換する。"""
        if abs(gain_db) < 0.05:
            return "0 dB"
        return f"{gain_db:+.0f} dB"

    def _on_gain_changed(self, value: int):
        """GAINスライダ変更時のハンドラ。"""
        gain_db = float(value)
        self._track.gain_db = gain_db
        # クリッピング警告：+12dB以上はオレンジ、安全圆内は通常色
        if gain_db >= 12.0:
            self._gain_label.setStyleSheet("color: #ff6600; font-size: 9px; font-weight: bold;")
        else:
            self._gain_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: 9px; font-weight: bold;")
        self._gain_label.setText(self._format_gain(gain_db))
        self.gain_changed.emit(self._track.track_id, gain_db)

    def _on_gain_reset(self):
        """GAINを0dBにリセットする。"""
        self._gain_slider.setValue(0)  # valueChangedシグナルが発火される

    def set_loading(self, loading: bool):
        """ロード中はLOADボタンを無効化し、テキストを変更する。"""
        self._load_btn.setEnabled(not loading)
        self._load_btn.setText("Loading..." if loading else "LOAD")

    def update_file_label(self, name: str):
        self._file_label.setText(name)

    def update_mute_state(self, muted: bool):
        self._mute_btn.setStyleSheet(self._btn_style(muted, is_mute=True))

    def update_solo_state(self, solo: bool):
        self._solo_btn.setStyleSheet(self._btn_style(solo, is_mute=False))

    def update_level(self, level: float):
        self._meter.set_level(level)

    def adjust_volume(self, delta: int):
        """キーボードショートカットで音量を増減する。"""
        self._fader.set_value(self._fader.get_value() + delta)

    def update_eq_curve(self, params):
        """EQParamsを受け取ってEQカーブを再描画する。"""
        self._eq_curve.update_curve(params)

    def clear_eq_curve(self):
        """EQカーブをフラットに戻す。"""
        self._eq_curve.clear()

    def update_seek_bar(self, pos_sec: float, duration_sec: float):
        """シークバーの位置と総時間を更新する。"""
        self._seek_bar.set_position(pos_sec)
        if self._seek_bar._duration_sec != duration_sec:
            self._seek_bar.set_duration(duration_sec)

    def set_seek_bar_peaks(self, peaks: list):
        """波形サムネイルデータをシークバーに設定する。"""
        self._seek_bar.set_peaks(peaks)

    def _on_seeked(self, pos_sec: float):
        """シークバー操作時のコールバック。親ウィンドウのエンジンにシークを依頼するため、
        シグナルではなく親ウィンドウを直接呼び出す方式を使う。"""
        # 親ウィンドウを探してシークを依頼する
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, '_engine') and hasattr(parent, '_tracks'):
                parent._engine.seek_track(self._track.track_id, pos_sec)
                break
            parent = parent.parent() if hasattr(parent, 'parent') else None

    def restore_state(self, track: TrackModel):
        """プロジェクト読み込み時にUIを復元する。"""
        self._track = track
        self._fader.set_value(int(track.volume * 100), emit=False)
        self._vol_label.setText(f"{int(track.volume * 100)}%")
        self._db_label.setText(track.get_volume_db())
        self._pan_slider.setValue(int(track.pan * 100))
        self._pan_label.setText(track.get_pan_display())
        self._mute_btn.setStyleSheet(self._btn_style(track.muted, is_mute=True))
        self._solo_btn.setStyleSheet(self._btn_style(track.solo, is_mute=False))
        self.update_aux_state(track.aux_enabled)
        self.update_xfade_assign(track.xfade_assign)
        self._file_label.setText(track.get_file_display_name())
        # ゲインを復元
        self._gain_slider.blockSignals(True)
        self._gain_slider.setValue(int(round(track.gain_db)))
        self._gain_slider.blockSignals(False)
        self._gain_label.setText(self._format_gain(track.gain_db))
        # EQパラメータを復元
        eq_params = EQParams(
            low_gain_db=track.eq_low_gain,
            mid_gain_db=track.eq_mid_gain,
            mid_freq_hz=track.eq_mid_freq,
            mid_q=track.eq_mid_q,
            high_gain_db=track.eq_high_gain,
        )
        self._eq_widget.restore_params(eq_params)
        # EQカーブを復元
        self._eq_curve.update_curve(eq_params)

    def set_volume_silent(self, volume: float):
        """シグナルを発火せずにフェーダーとラベルを更新（UNDO/REDO用）。"""
        self._fader.blockSignals(True)
        self._fader.set_value(int(volume * 100), emit=False)
        self._fader.blockSignals(False)
        self._vol_label.setText(f"{int(volume * 100)}%")
        import math
        if volume <= 0:
            self._db_label.setText("-inf dB")
        else:
            db = 20 * math.log10(volume)
            self._db_label.setText(f"{db:+.1f} dB")

    def set_pan_silent(self, pan: float):
        """シグナルを発火せずにPANスライダーとラベルを更新（UNDO/REDO用）。"""
        self._pan_slider.blockSignals(True)
        self._pan_slider.setValue(int(pan * 100))
        self._pan_slider.blockSignals(False)
        self._track.pan = pan
        self._pan_label.setText(self._track.get_pan_display())

    def set_gain_silent(self, gain_db: float):
        """シグナルを発火せずにGAINスライダーとラベルを更新（UNDO/REDO用）。"""
        self._gain_slider.blockSignals(True)
        self._gain_slider.setValue(int(round(gain_db)))
        self._gain_slider.blockSignals(False)
        self._gain_label.setText(self._format_gain(gain_db))


# ===========================================================================
# MASTERトラックウィジェット（TrackWidgetと同形式）
# ===========================================================================
class MasterTrackWidget(QFrame):
    volumeChanged = pyqtSignal(float)
    # (track_id=-1, preset_name, enabled) → マスターエフェクト変更通知
    effectChanged = pyqtSignal(int, str, bool)
    # GEQモード変更通知: 'low' / 'hi' / 'off'
    geqModeChanged = pyqtSignal(str)
    # REC START / STOP ボタンシグナル
    rec_start_clicked = pyqtSignal()
    rec_stop_clicked = pyqtSignal()
    # マスターリミッター変更通知: (enabled, ceiling_db)
    limiterChanged = pyqtSignal(bool, float)
    # X-FADER変更通知: (position, curve, cut_a, cut_b)
    xfadeChanged = pyqtSignal(float, str, bool, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accent = Colors.MASTER_ACCENT
        self._clip_active = False
        self._setup_ui()

    def _setup_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(140)
        self.setMaximumWidth(180)
        self.setStyleSheet(f"""
            MasterTrackWidget {{
                background-color: {Colors.BG_TRACK};
                border: 1px solid {Colors.BORDER};
                border-top: 3px solid {self._accent};
                border-radius: 4px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # MASTER ラベル
        master_lbl = QLabel("MASTER")
        master_lbl.setAlignment(Qt.AlignCenter)
        master_lbl.setStyleSheet(f"color: {self._accent}; font-size: 11px; font-weight: bold; letter-spacing: 2px;")
        layout.addWidget(master_lbl)

        # クリッピング警告ラベル
        self._clip_label = QLabel("")
        self._clip_label.setAlignment(Qt.AlignCenter)
        self._clip_label.setMaximumHeight(24)
        self._clip_label.setStyleSheet(f"color: {Colors.CLIP_WARNING}; font-size: 9px; font-weight: bold; padding: 2px;")
        layout.addWidget(self._clip_label)

        # EXPORT WAV ボタン（初期状態は無効）
        self._export_btn = QPushButton("EXPORT WAV")
        self._export_btn.setFixedHeight(28)
        self._export_btn.setEnabled(False)
        self._export_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.BTN_EXPORT}; color: {Colors.TEXT_PRIMARY};
                border: 1px solid #8e44ad; border-radius: 3px;
                font-size: 9px; font-weight: bold; letter-spacing: 1px;
            }}
            QPushButton:hover {{ background-color: {Colors.BTN_EXPORT_HOV}; }}
            QPushButton:pressed {{ background-color: #4a235a; }}
            QPushButton:disabled {{ background-color: #333; color: #666; border: 1px solid #555; }}
        """)
        layout.addWidget(self._export_btn)

        # REC START ボタン（初期有効）
        self._rec_start_btn = QPushButton("REC START")
        self._rec_start_btn.setFixedHeight(28)
        self._rec_start_btn.setStyleSheet("""
            QPushButton {
                background-color: #1a5c2a; color: #e8e8e8;
                border: 1px solid #27ae60; border-radius: 3px;
                font-size: 9px; font-weight: bold; letter-spacing: 1px;
            }
            QPushButton:hover { background-color: #27ae60; color: #fff; }
            QPushButton:pressed { background-color: #145a20; }
            QPushButton:disabled { background-color: #333; color: #666; border: 1px solid #555; }
        """)
        self._rec_start_btn.clicked.connect(self.rec_start_clicked)
        layout.addWidget(self._rec_start_btn)

        # REC STOP ボタン（初期無効）
        self._rec_stop_btn = QPushButton("REC STOP")
        self._rec_stop_btn.setFixedHeight(28)
        self._rec_stop_btn.setEnabled(False)
        self._rec_stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #5c1a1a; color: #e8e8e8;
                border: 1px solid #c0392b; border-radius: 3px;
                font-size: 9px; font-weight: bold; letter-spacing: 1px;
            }
            QPushButton:hover { background-color: #c0392b; color: #fff; }
            QPushButton:pressed { background-color: #4a0f0f; }
            QPushButton:disabled { background-color: #333; color: #666; border: 1px solid #555; }
        """)
        self._rec_stop_btn.clicked.connect(self.rec_stop_clicked)
        layout.addWidget(self._rec_stop_btn)

        # =============================================
        # Phase 9: GEQパネル（GEQ Low / GEQ Hi）
        # =============================================
        geq_frame = QFrame()
        geq_frame.setFrameShape(QFrame.StyledPanel)
        geq_frame.setStyleSheet("""
            QFrame {
                background-color: #1a1a1a;
                border: 1px solid #444;
                border-radius: 3px;
            }
        """)
        geq_layout = QVBoxLayout(geq_frame)
        geq_layout.setContentsMargins(4, 4, 4, 4)
        geq_layout.setSpacing(4)

        # GEQヘッダー
        geq_header = QHBoxLayout()
        geq_header.setSpacing(4)
        geq_lbl = QLabel("GEQ")
        geq_lbl.setStyleSheet("color: #D4A017; font-size: 9px; font-weight: bold; letter-spacing: 1px;")
        geq_header.addWidget(geq_lbl)
        geq_header.addStretch()
        geq_layout.addLayout(geq_header)

        # GEQ Low / GEQ Hi ボタン行
        geq_btn_row = QHBoxLayout()
        geq_btn_row.setSpacing(4)
        self._geq_low_btn = QPushButton("GEQ Low")
        self._geq_low_btn.setFixedHeight(20)
        self._geq_low_btn.setCheckable(True)
        self._geq_low_btn.setChecked(False)
        self._geq_low_btn.setStyleSheet(self._geq_btn_style(False))
        self._geq_low_btn.clicked.connect(lambda: self._on_geq_btn_clicked("low"))
        geq_btn_row.addWidget(self._geq_low_btn)

        self._geq_hi_btn = QPushButton("GEQ Hi")
        self._geq_hi_btn.setFixedHeight(20)
        self._geq_hi_btn.setCheckable(True)
        self._geq_hi_btn.setChecked(False)
        self._geq_hi_btn.setStyleSheet(self._geq_btn_style(False))
        self._geq_hi_btn.clicked.connect(lambda: self._on_geq_btn_clicked("hi"))
        geq_btn_row.addWidget(self._geq_hi_btn)
        geq_layout.addLayout(geq_btn_row)

        # GEQステータスラベル
        self._geq_status_lbl = QLabel("GEQ: OFF")
        self._geq_status_lbl.setAlignment(Qt.AlignCenter)
        self._geq_status_lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 8px;")
        geq_layout.addWidget(self._geq_status_lbl)

        layout.addWidget(geq_frame)

        # =============================================
        # Phase 6: エフェクターパネル
        # =============================================
        fx_frame = QFrame()
        fx_frame.setFrameShape(QFrame.StyledPanel)
        fx_frame.setStyleSheet("""
            QFrame {
                background-color: #1a1a1a;
                border: 1px solid #333;
                border-radius: 3px;
            }
        """)
        fx_layout = QVBoxLayout(fx_frame)
        fx_layout.setContentsMargins(4, 4, 4, 4)
        fx_layout.setSpacing(4)

        # FXヘッダー行: FXラベル + ON/OFFトグル
        fx_header = QHBoxLayout()
        fx_header.setSpacing(4)
        fx_lbl = QLabel("FX")
        fx_lbl.setStyleSheet(f"color: {self._accent}; font-size: 9px; font-weight: bold; letter-spacing: 1px;")
        fx_header.addWidget(fx_lbl)
        fx_header.addStretch()
        self._fx_on_btn = QPushButton("ON")
        self._fx_on_btn.setFixedSize(32, 18)
        self._fx_on_btn.setCheckable(True)
        self._fx_on_btn.setChecked(False)
        self._fx_on_btn.clicked.connect(self._on_fx_toggle)
        self._fx_on_btn.setStyleSheet(self._fx_btn_style(False))
        fx_header.addWidget(self._fx_on_btn)
        fx_layout.addLayout(fx_header)

        # プリセットコンボボックス
        self._fx_combo = QComboBox()
        self._fx_combo.setStyleSheet("""
            QComboBox {
                background-color: #2a2a2a; color: #ddd;
                border: 1px solid #555; border-radius: 3px;
                font-size: 10px; font-weight: bold; padding: 2px 4px;
                min-height: 20px;
            }
            QComboBox::drop-down { border: none; width: 16px; }
            QComboBox QAbstractItemView {
                background-color: #2a2a2a; color: #ddd;
                selection-background-color: #3a5068;
                font-size: 10px; font-weight: bold;
                padding: 2px;
            }
        """)
        # カテゴリ別にアイテムを追加
        for category, presets in EFFECT_CATEGORIES.items():
            for p in presets:
                self._fx_combo.addItem(p)
        self._fx_combo.setCurrentText("None")
        self._fx_combo.currentTextChanged.connect(self._on_fx_preset_changed)
        fx_layout.addWidget(self._fx_combo)

        # 現在のエフェクトタイプ表示
        self._fx_type_lbl = QLabel("Type: -")
        self._fx_type_lbl.setAlignment(Qt.AlignCenter)
        self._fx_type_lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 8px;")
        fx_layout.addWidget(self._fx_type_lbl)

        layout.addWidget(fx_frame)

        # =============================================
        # Phase 23: マスター・リミッター
        # =============================================
        limiter_frame = QFrame()
        limiter_frame.setFrameShape(QFrame.StyledPanel)
        limiter_frame.setStyleSheet("""
            QFrame {
                background-color: #101b20;
                border: 1px solid #275464;
                border-radius: 3px;
            }
        """)
        limiter_layout = QVBoxLayout(limiter_frame)
        limiter_layout.setContentsMargins(4, 4, 4, 4)
        limiter_layout.setSpacing(3)

        limiter_header = QHBoxLayout()
        limiter_header.setSpacing(4)
        limiter_lbl = QLabel("LIMITER")
        limiter_lbl.setStyleSheet("color: #42d9e8; font-size: 9px; font-weight: bold; letter-spacing: 1px;")
        limiter_header.addWidget(limiter_lbl)
        limiter_header.addStretch()
        self._limiter_on_btn = QPushButton("ON")
        self._limiter_on_btn.setFixedSize(32, 18)
        self._limiter_on_btn.setCheckable(True)
        self._limiter_on_btn.setChecked(True)
        self._limiter_on_btn.setStyleSheet(self._limiter_btn_style(True))
        self._limiter_on_btn.clicked.connect(self._on_limiter_toggle)
        limiter_header.addWidget(self._limiter_on_btn)
        limiter_layout.addLayout(limiter_header)

        self._limiter_combo = QComboBox()
        self._limiter_combo.addItem("-0.1 dB", -0.1)
        self._limiter_combo.addItem("-1.0 dB", -1.0)
        self._limiter_combo.addItem("-3.0 dB", -3.0)
        self._limiter_combo.addItem("-6.0 dB", -6.0)
        self._limiter_combo.setCurrentIndex(1)
        self._limiter_combo.setStyleSheet("""
            QComboBox {
                background-color: #162d35; color: #d7faff;
                border: 1px solid #347887; border-radius: 2px;
                font-size: 9px; font-weight: bold; padding: 1px 4px; min-height: 18px;
            }
            QComboBox QAbstractItemView {
                background-color: #162d35; color: #d7faff; selection-background-color: #275464;
                font-size: 9px;
            }
        """)
        self._limiter_combo.currentIndexChanged.connect(self._on_limiter_ceiling_changed)
        limiter_layout.addWidget(self._limiter_combo)

        self._limiter_gr_label = QLabel("GR: 0.0 dB")
        self._limiter_gr_label.setAlignment(Qt.AlignCenter)
        self._limiter_gr_label.setStyleSheet("color: #76c9d2; font-size: 8px; font-family: Consolas;")
        limiter_layout.addWidget(self._limiter_gr_label)
        layout.addWidget(limiter_frame)

        # =============================================
        # Phase 25: X-FADER
        # =============================================
        xfade_frame = QFrame()
        xfade_frame.setFrameShape(QFrame.StyledPanel)
        xfade_frame.setStyleSheet("""
            QFrame { background-color: #161521; border: 1px solid #5a4f92; border-radius: 3px; }
        """)
        xfade_layout = QVBoxLayout(xfade_frame)
        xfade_layout.setContentsMargins(4, 3, 4, 3)
        xfade_layout.setSpacing(2)
        xfade_header = QHBoxLayout()
        xfade_lbl = QLabel("X-FADER")
        xfade_lbl.setStyleSheet("color: #b9adff; font-size: 8px; font-weight: bold; letter-spacing: 1px;")
        xfade_header.addWidget(xfade_lbl)
        xfade_header.addStretch()
        self._xfade_curve_combo = QComboBox()
        self._xfade_curve_combo.addItem("E.PWR", "equal_power")
        self._xfade_curve_combo.addItem("LIN", "linear")
        self._xfade_curve_combo.setFixedSize(48, 18)
        self._xfade_curve_combo.setToolTip("X-FADERカーブ: Equal Power / Linear")
        self._xfade_curve_combo.setStyleSheet("QComboBox { background:#29254a; color:#e8e5ff; border:1px solid #6d60aa; font-size:7px; padding:0 2px; }")
        self._xfade_curve_combo.currentIndexChanged.connect(self._on_xfade_changed)
        xfade_header.addWidget(self._xfade_curve_combo)
        xfade_layout.addLayout(xfade_header)

        self._xfade_slider = QSlider(Qt.Horizontal)
        self._xfade_slider.setRange(0, 100)
        self._xfade_slider.setValue(50)
        self._xfade_slider.setFixedHeight(16)
        self._xfade_slider.setToolTip("A ← X-FADER → B")
        self._xfade_slider.setStyleSheet("""
            QSlider::groove:horizontal { height:4px; background:#343052; border-radius:2px; }
            QSlider::sub-page:horizontal { background:#8275df; border-radius:2px; }
            QSlider::handle:horizontal { width:10px; margin:-4px 0; background:#f1edff; border:1px solid #a79bff; border-radius:5px; }
        """)
        self._xfade_slider.valueChanged.connect(self._on_xfade_changed)
        xfade_layout.addWidget(self._xfade_slider)

        xfade_bottom = QHBoxLayout()
        self._xfade_cut_a_btn = QPushButton("A CUT")
        self._xfade_cut_b_btn = QPushButton("B CUT")
        for button in (self._xfade_cut_a_btn, self._xfade_cut_b_btn):
            button.setFixedHeight(17)
            button.setCheckable(True)
            button.setStyleSheet(self._xfade_cut_style(False))
            button.clicked.connect(self._on_xfade_changed)
            xfade_bottom.addWidget(button)
        xfade_layout.addLayout(xfade_bottom)
        layout.addWidget(xfade_frame)

        # VUメーターウィジェット（MASTERトラックの空白部分に配置）
        vu_frame = QFrame()
        vu_frame.setFrameShape(QFrame.StyledPanel)
        vu_frame.setStyleSheet("""
            QFrame {
                background-color: #0d1117;
                border: 1px solid #2a2a2a;
                border-radius: 3px;
            }
        """)
        vu_layout = QVBoxLayout(vu_frame)
        vu_layout.setContentsMargins(4, 4, 4, 4)
        vu_layout.setSpacing(2)

        # VUメーターヘッダー
        vu_header = QHBoxLayout()
        vu_lbl = QLabel("VU METER")
        vu_lbl.setStyleSheet(f"color: {Colors.MASTER_ACCENT}; font-size: 8px; font-weight: bold; letter-spacing: 1px;")
        vu_header.addWidget(vu_lbl)
        vu_header.addStretch()
        self._clip_reset_btn = QPushButton("CLIP")
        self._clip_reset_btn.setFixedSize(36, 16)
        self._clip_reset_btn.setStyleSheet("""
            QPushButton {
                background-color: #3a1010; color: #e74c3c;
                border: 1px solid #c0392b; border-radius: 2px;
                font-size: 8px; font-weight: bold;
            }
            QPushButton:hover { background-color: #c0392b; color: #fff; }
        """)
        self._clip_reset_btn.setToolTip("クリップ警告をリセット")
        self._clip_reset_btn.clicked.connect(self._on_clip_reset)
        vu_header.addWidget(self._clip_reset_btn)
        vu_layout.addLayout(vu_header)

        # アナログ针式VUメーター
        self._analog_vu_meter = AnalogVUMeterWidget()
        self._analog_vu_meter.setMinimumHeight(90)
        self._analog_vu_meter.setMaximumHeight(110)
        vu_layout.addWidget(self._analog_vu_meter)

        # セグメントVUメーター（高さを小さく調整）
        self._vu_meter = VUMeterWidget()
        self._vu_meter.setMinimumHeight(150)
        self._vu_meter.setMaximumHeight(180)
        vu_layout.addWidget(self._vu_meter)

        # dB値テキスト表示ラベル
        self._vu_db_label = QLabel("L: --- dB  R: --- dB")
        self._vu_db_label.setAlignment(Qt.AlignCenter)
        self._vu_db_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 8px;")
        vu_layout.addWidget(self._vu_db_label)

        layout.addWidget(vu_frame)

        # フェーダー + ステレオ2chメーター
        fader_meter_layout = QHBoxLayout()
        fader_meter_layout.setSpacing(4)
        self._fader = VerticalFader(max_value=150, default_value=100)
        self._fader.set_value(100, emit=False)
        self._fader.valueChanged.connect(self._on_fader_changed)
        self._meter_l = LevelMeter()
        self._meter_r = LevelMeter()
        fader_meter_layout.addStretch()
        fader_meter_layout.addWidget(self._fader)
        fader_meter_layout.addWidget(self._meter_l)
        fader_meter_layout.addWidget(self._meter_r)
        fader_meter_layout.addStretch()
        layout.addLayout(fader_meter_layout)

        # 音量値表示
        self._vol_label = QLabel("100%")
        self._vol_label.setAlignment(Qt.AlignCenter)
        self._vol_label.setMaximumHeight(18)
        self._vol_label.setStyleSheet(f"color: {self._accent}; font-size: 12px; font-weight: bold;")
        layout.addWidget(self._vol_label)

        self._db_label = QLabel("+0.0 dB")
        self._db_label.setAlignment(Qt.AlignCenter)
        self._db_label.setMaximumHeight(14)
        self._db_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 9px;")
        layout.addWidget(self._db_label)

        # RANGE / ヒント表示
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        range_lbl = QLabel("RANGE: 0% ~ 150%")
        range_lbl.setAlignment(Qt.AlignCenter)
        range_lbl.setMaximumHeight(12)
        range_lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 9px;")
        info_layout.addWidget(range_lbl)
        hint_lbl = QLabel("DBL-CLK: 100%")
        hint_lbl.setAlignment(Qt.AlignCenter)
        hint_lbl.setMaximumHeight(12)
        hint_lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 9px;")
        info_layout.addWidget(hint_lbl)
        layout.addLayout(info_layout)

        bottom_lbl = QLabel("MASTER VOL")
        bottom_lbl.setAlignment(Qt.AlignCenter)
        bottom_lbl.setMaximumHeight(16)
        bottom_lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 8px; background-color: #1a1a1a; border-radius: 2px; padding: 2px;")
        layout.addWidget(bottom_lbl)

        # GEQカーブ表示（MASTERトラック下部）
        # TrackWidgetのEQカーブと同じ高さに揃えるためスペーサーを挿入
        layout.addItem(QSpacerItem(0, 22, QSizePolicy.Minimum, QSizePolicy.Fixed))
        self._geq_curve = GEQCurveView()
        layout.addWidget(self._geq_curve)
        layout.addStretch()

    def _geq_btn_style(self, active: bool) -> str:
        if active:
            return """
                QPushButton {
                    background-color: #D4A017; color: #1a1a1a;
                    border: 1px solid #F0C040; border-radius: 3px;
                    font-size: 8px; font-weight: bold;
                }
                QPushButton:hover { background-color: #F0C040; }
            """
        else:
            return """
                QPushButton {
                    background-color: #333; color: #888;
                    border: 1px solid #444; border-radius: 3px;
                    font-size: 8px; font-weight: bold;
                }
                QPushButton:hover { background-color: #444; color: #aaa; }
            """

    def _on_geq_btn_clicked(self, mode: str):
        """「GEQ Low」または「GEQ Hi」ボタンが押されたときの処理。"""
        if mode == "low":
            if self._geq_low_btn.isChecked():
                # GEQ Lowを有効化（GEQ Hiを解除）
                self._geq_hi_btn.setChecked(False)
                self._geq_hi_btn.setStyleSheet(self._geq_btn_style(False))
                self._geq_low_btn.setStyleSheet(self._geq_btn_style(True))
                self._geq_status_lbl.setText("GEQ Low: ACTIVE")
                self.geqModeChanged.emit("low")
            else:
                # GEQ Lowを解除
                self._geq_low_btn.setStyleSheet(self._geq_btn_style(False))
                self._geq_status_lbl.setText("GEQ: OFF")
                self.geqModeChanged.emit("off")
        else:  # hi
            if self._geq_hi_btn.isChecked():
                # GEQ Hiを有効化（GEQ Lowを解除）
                self._geq_low_btn.setChecked(False)
                self._geq_low_btn.setStyleSheet(self._geq_btn_style(False))
                self._geq_hi_btn.setStyleSheet(self._geq_btn_style(True))
                self._geq_status_lbl.setText("GEQ Hi: ACTIVE")
                self.geqModeChanged.emit("hi")
            else:
                # GEQ Hiを解除
                self._geq_hi_btn.setStyleSheet(self._geq_btn_style(False))
                self._geq_status_lbl.setText("GEQ: OFF")
                self.geqModeChanged.emit("off")

    def get_geq_mode(self) -> str:
        """現在のGEQモードを返す（'low'/'hi'/'off'）。"""
        if self._geq_low_btn.isChecked():
            return "low"
        if self._geq_hi_btn.isChecked():
            return "hi"
        return "off"

    def update_geq_curve(self, params: GEQParams):
        """GEQカーブを更新する。"""
        self._geq_curve.update_curve(params)

    def _fx_btn_style(self, active: bool) -> str:
        if active:
            return f"""
                QPushButton {{
                    background-color: #27ae60; color: white;
                    border: 1px solid #2ecc71; border-radius: 3px;
                    font-size: 8px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: #2ecc71; }}
            """
        else:
            return f"""
                QPushButton {{
                    background-color: #333; color: #888;
                    border: 1px solid #444; border-radius: 3px;
                    font-size: 8px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: #444; color: #aaa; }}
            """

    def _limiter_btn_style(self, active: bool) -> str:
        if active:
            return """
                QPushButton {
                    background-color: #12606c; color: #e8ffff;
                    border: 1px solid #42d9e8; border-radius: 3px;
                    font-size: 8px; font-weight: bold;
                }
                QPushButton:hover { background-color: #168090; }
            """
        return """
            QPushButton {
                background-color: #333; color: #888;
                border: 1px solid #444; border-radius: 3px;
                font-size: 8px; font-weight: bold;
            }
            QPushButton:hover { background-color: #444; color: #aaa; }
        """

    def _xfade_cut_style(self, active: bool) -> str:
        if active:
            return """
                QPushButton { background:#8b2945; color:#fff1f4; border:1px solid #f07192;
                              border-radius:2px; font-size:7px; font-weight:bold; }
            """
        return """
            QPushButton { background:#29254a; color:#b9adff; border:1px solid #5a4f92;
                          border-radius:2px; font-size:7px; font-weight:bold; }
            QPushButton:hover { background:#3a3565; color:#fff; }
        """

    def _on_xfade_changed(self, *_args):
        cut_a = self._xfade_cut_a_btn.isChecked()
        cut_b = self._xfade_cut_b_btn.isChecked()
        self._xfade_cut_a_btn.setStyleSheet(self._xfade_cut_style(cut_a))
        self._xfade_cut_b_btn.setStyleSheet(self._xfade_cut_style(cut_b))
        curve = self._xfade_curve_combo.currentData() or "equal_power"
        self.xfadeChanged.emit(self._xfade_slider.value() / 100.0, curve, cut_a, cut_b)

    def get_xfade_state(self) -> dict:
        return {
            "position": self._xfade_slider.value() / 100.0,
            "curve": self._xfade_curve_combo.currentData() or "equal_power",
            "cut_a": self._xfade_cut_a_btn.isChecked(),
            "cut_b": self._xfade_cut_b_btn.isChecked(),
        }

    def restore_xfade_state(self, state: dict):
        position = max(0.0, min(1.0, float(state.get("position", 0.5))))
        curve = state.get("curve", "equal_power")
        index = self._xfade_curve_combo.findData(curve)
        widgets = (self._xfade_slider, self._xfade_curve_combo,
                   self._xfade_cut_a_btn, self._xfade_cut_b_btn)
        for widget in widgets:
            widget.blockSignals(True)
        self._xfade_slider.setValue(round(position * 100))
        self._xfade_curve_combo.setCurrentIndex(max(0, index))
        self._xfade_cut_a_btn.setChecked(bool(state.get("cut_a", False)))
        self._xfade_cut_b_btn.setChecked(bool(state.get("cut_b", False)))
        for widget in widgets:
            widget.blockSignals(False)
        self._xfade_cut_a_btn.setStyleSheet(self._xfade_cut_style(self._xfade_cut_a_btn.isChecked()))
        self._xfade_cut_b_btn.setStyleSheet(self._xfade_cut_style(self._xfade_cut_b_btn.isChecked()))

    def _on_limiter_toggle(self):
        enabled = self._limiter_on_btn.isChecked()
        self._limiter_on_btn.setStyleSheet(self._limiter_btn_style(enabled))
        self._limiter_combo.setEnabled(enabled)
        if not enabled:
            self._limiter_gr_label.setText("GR: BYPASS")
        self.limiterChanged.emit(enabled, self.get_limiter_ceiling_db())

    def _on_limiter_ceiling_changed(self, _index: int):
        if self._limiter_on_btn.isChecked():
            self.limiterChanged.emit(True, self.get_limiter_ceiling_db())

    def get_limiter_enabled(self) -> bool:
        return self._limiter_on_btn.isChecked()

    def get_limiter_ceiling_db(self) -> float:
        value = self._limiter_combo.currentData()
        return float(value) if value is not None else -1.0

    def update_limiter_reduction(self, reduction_db: float):
        """リミッターの直近ゲインリダクションを表示する。"""
        if not self._limiter_on_btn.isChecked():
            self._limiter_gr_label.setText("GR: BYPASS")
            return
        reduction_db = max(0.0, float(reduction_db))
        self._limiter_gr_label.setText(f"GR: -{reduction_db:.1f} dB")
        if reduction_db >= 6.0:
            color = "#ff8d8d"
        elif reduction_db >= 2.0:
            color = "#f7ce78"
        else:
            color = "#76c9d2"
        self._limiter_gr_label.setStyleSheet(
            f"color: {color}; font-size: 8px; font-family: Consolas; font-weight: bold;"
        )

    def restore_limiter_state(self, enabled: bool, ceiling_db: float):
        """リミッターのUI状態を外部設定から復元する。"""
        self._limiter_on_btn.blockSignals(True)
        self._limiter_combo.blockSignals(True)
        self._limiter_on_btn.setChecked(bool(enabled))
        self._limiter_on_btn.setStyleSheet(self._limiter_btn_style(bool(enabled)))
        target = min(
            range(self._limiter_combo.count()),
            key=lambda i: abs(float(self._limiter_combo.itemData(i)) - float(ceiling_db))
        )
        self._limiter_combo.setCurrentIndex(target)
        self._limiter_combo.setEnabled(bool(enabled))
        self._limiter_on_btn.blockSignals(False)
        self._limiter_combo.blockSignals(False)
        self.update_limiter_reduction(0.0)

    def _on_fx_toggle(self):
        enabled = self._fx_on_btn.isChecked()
        self._fx_on_btn.setStyleSheet(self._fx_btn_style(enabled))
        preset = self._fx_combo.currentText()
        self._update_fx_type_label(preset, enabled)
        self.effectChanged.emit(-1, preset, enabled)

    def _on_fx_preset_changed(self, name: str):
        enabled = self._fx_on_btn.isChecked()
        self._update_fx_type_label(name, enabled)
        if enabled:
            self.effectChanged.emit(-1, name, enabled)

    def _update_fx_type_label(self, preset_name: str, enabled: bool):
        if preset_name in FX_PRESETS and preset_name != "None":
            fx_type = FX_PRESETS[preset_name].effect_type.capitalize()
            status = "ON" if enabled else "OFF"
            self._fx_type_lbl.setText(f"Type: {fx_type} [{status}]")
        else:
            self._fx_type_lbl.setText("Type: -")

    def get_fx_preset(self) -> str:
        return self._fx_combo.currentText()

    def get_fx_enabled(self) -> bool:
        return self._fx_on_btn.isChecked()

    def restore_fx_state(self, preset_name: str, enabled: bool):
        """プロジェクト読み込み時にFX状態を復元する。"""
        self._fx_combo.blockSignals(True)
        self._fx_combo.setCurrentText(preset_name if preset_name in FX_PRESETS else "None")
        self._fx_combo.blockSignals(False)
        self._fx_on_btn.setChecked(enabled)
        self._fx_on_btn.setStyleSheet(self._fx_btn_style(enabled))
        self._update_fx_type_label(self._fx_combo.currentText(), enabled)

    def _on_fader_changed(self, value: int):
        vol_ratio = value / 100.0
        self._vol_label.setText(f"{value}%")
        if value == 0:
            db_str = "-inf dB"
        else:
            db = 20 * math.log10(vol_ratio)
            db_str = f"{db:+.1f} dB"
        self._db_label.setText(db_str)
        self.volumeChanged.emit(vol_ratio)

    def get_value(self) -> float:
        return self._fader.get_value() / 100.0

    def update_level(self, level: float):
        self._meter_l.set_level(level)
        self._meter_r.set_level(level)

    def update_vu_meter(self, rms_l: float, rms_r: float,
                        peak_l: float, peak_r: float,
                        clip_l: bool, clip_r: bool):
        """リアルタイムVUメーター値を更新する。"""
        self._vu_meter.update_vu(rms_l, rms_r, peak_l, peak_r, clip_l, clip_r)
        self._analog_vu_meter.update_vu(rms_l, rms_r, peak_l, peak_r, clip_l, clip_r)
        # dB値テキスト更新
        def to_db(v):
            return f"{20 * math.log10(max(v, 1e-9)):+.1f}" if v > 0.001 else "-inf"
        self._vu_db_label.setText(
            f"L: {to_db(rms_l)} dB  R: {to_db(rms_r)} dB"
        )

    def _on_clip_reset(self):
        """クリップ警告をリセットする。"""
        self._vu_meter.reset_clip()
        self._analog_vu_meter.reset_clip()

    def show_clip_warning(self, show: bool, ratio: float = 0.0):
        if show:
            self._clip_active = True
            pct = f"{ratio * 100:.1f}%"
            self._clip_label.setText(f"CLIP\n{pct}")
            self.setStyleSheet(f"""
                MasterTrackWidget {{
                    background-color: {Colors.BG_TRACK};
                    border: 1px solid {Colors.CLIP_WARNING};
                    border-top: 3px solid {Colors.CLIP_WARNING};
                    border-radius: 4px;
                }}
            """)
        else:
            self._clip_active = False
            self._clip_label.setText("")
            self.setStyleSheet(f"""
                MasterTrackWidget {{
                    background-color: {Colors.BG_TRACK};
                    border: 1px solid {Colors.BORDER};
                    border-top: 3px solid {self._accent};
                    border-radius: 4px;
                }}
            """)

    def restore_state(self, master_volume: float):
        val = int(master_volume * 100)
        self._fader.set_value(val, emit=False)
        self._vol_label.setText(f"{val}%")
        if val == 0:
            self._db_label.setText("-inf dB")
        else:
            db = 20 * math.log10(master_volume)
            self._db_label.setText(f"{db:+.1f} dB")


# ===========================================================================
# メインウィンドウ
# ===========================================================================
class MixerMainWindow(QMainWindow):
    NUM_TRACKS      = 16   # 総トラック数
    TRACKS_PER_BANK = 8    # 1バンクあたりのトラック数

    def __init__(self):
        super().__init__()
        # 16トラック分のモデルを生成
        self._tracks: List[TrackModel] = [
            TrackModel(track_id=i) for i in range(self.NUM_TRACKS)
        ]
        self._engine = AudioEngine(num_tracks=self.NUM_TRACKS)
        self._track_widgets: List[TrackWidget] = []
        self._master_widget: Optional[MasterTrackWidget] = None
        self._export_worker: Optional[ExportWorker] = None
        self._load_workers: List[LoadWorker] = []   # GC防止用
        self._eq_workers: List[EQWorker] = []       # GC防止用
        self._effect_workers: List[EffectWorker] = []  # GC防止用
        self._project_store = ProjectStore()
        self._current_project_path: Optional[str] = None
        self._current_bank: int = 0   # 0=Bank A (1-8), 1=Bank B (9-16)

        # Phase 20: マーカーマネージャー
        from project_store import MarkerManager
        self._marker_manager = MarkerManager()
        self._marker_bar: Optional["MarkerBar"] = None

        # Phase 21: UNDO/REDOコマンド履歴
        from project_store import CommandHistory
        self._history = CommandHistory()
        self._undo_btn = None   # トランスポートバーに追加後に参照
        self._redo_btn = None

        # Phase 22: ループ範囲と操作ボタン
        self._loop_in_sec: Optional[float] = None
        self._loop_out_sec: Optional[float] = None
        self._loop_btn = None
        self._loop_in_btn = None
        self._loop_out_btn = None
        self._loop_all_btn = None

        # GEQモード状態
        self._geq_mode: str = "off"  # 'low' / 'hi' / 'off'
        self._geq_params: GEQParams = GEQParams()

        self._setup_ui()
        self._setup_timer()
        self.setFocusPolicy(Qt.StrongFocus)

    # ------------------------------------------------------------------
    # UI 構築
    # ------------------------------------------------------------------

    def _setup_ui(self):
        self.setWindowTitle("Mixer4Track — 16 Track Mixer  [Phase 25]")
        self.setMinimumSize(1280, 1000)
        self.resize(1440, 1200)

        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(Colors.BG_MAIN))
        palette.setColor(QPalette.WindowText, QColor(Colors.TEXT_PRIMARY))
        palette.setColor(QPalette.Base, QColor(Colors.BG_TRACK_DARK))
        palette.setColor(QPalette.Text, QColor(Colors.TEXT_PRIMARY))
        palette.setColor(QPalette.Button, QColor("#2a2a2a"))
        palette.setColor(QPalette.ButtonText, QColor(Colors.TEXT_PRIMARY))
        self.setPalette(palette)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        main_layout.addWidget(self._build_header())

        # ---- バンク切り替えバー ----
        main_layout.addWidget(self._build_bank_bar())

        # ---- トラックエリア ----
        # track_area_container: バンクA/Bのウィジェットを重ねて表示切替
        self._track_area_container = QWidget()
        container_layout = QHBoxLayout(self._track_area_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # バンクA（Track 1〜8）
        self._bank_a_widget = self._build_bank_widget(bank=0)
        # バンクB（Track 9〜16）
        self._bank_b_widget = self._build_bank_widget(bank=1)

        # 区切り線
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {Colors.BORDER};")

        # MASTERウィジェット（常時表示）
        self._master_widget = MasterTrackWidget()
        self._master_widget.volumeChanged.connect(self._on_master_volume_changed)
        self._master_widget._export_btn.clicked.connect(self._on_export)
        self._master_widget.effectChanged.connect(self._on_master_effect_changed)
        self._master_widget.limiterChanged.connect(self._on_master_limiter_changed)
        self._master_widget.xfadeChanged.connect(self._on_master_xfade_changed)
        self._master_widget.geqModeChanged.connect(self._on_geq_mode_changed)
        self._master_widget.rec_start_clicked.connect(self._on_rec_start)
        self._master_widget.rec_stop_clicked.connect(self._on_rec_stop)

        # 表示中バンクが余白を吸収するようストレッチを与える。
        container_layout.addWidget(self._bank_a_widget, 1)
        container_layout.addWidget(self._bank_b_widget, 1)
        container_layout.addWidget(sep)
        container_layout.addWidget(self._master_widget)

        # 初期表示: バンクAのみ表示
        self._bank_b_widget.setVisible(False)

        main_layout.addWidget(self._track_area_container, stretch=1)
        main_layout.addWidget(self._build_transport())

        # Phase 20: マーカーバー
        self._marker_bar = MarkerBar()
        self._marker_bar.marker_clicked.connect(self._on_marker_jump)
        self._marker_bar.marker_delete_requested.connect(self._on_marker_delete)
        self._marker_bar.marker_rename_requested.connect(self._on_marker_rename)
        main_layout.addWidget(self._marker_bar)

        # ステータスバー
        self._status_label = QLabel("Load files and press PLAY  |  [BANK A: 1-8] active")
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setStyleSheet(f"""
            color: {Colors.TEXT_SECONDARY}; font-size: 10px;
            padding: 4px; background-color: #111; border-radius: 3px;
        """)
        main_layout.addWidget(self._status_label)

    def _build_bank_widget(self, bank: int) -> QWidget:
        """バンク（0=A, 1=B）のトラックウィジェット群をまとめたウィジェットを生成する。"""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        start = bank * self.TRACKS_PER_BANK
        end   = start + self.TRACKS_PER_BANK

        for i in range(start, end):
            track = self._tracks[i]
            tw = TrackWidget(track, bank=bank)
            tw.file_load_requested.connect(self._on_load_file)
            tw.volume_changed.connect(self._on_volume_changed)
            tw.pan_changed.connect(self._on_pan_changed)
            tw.gain_changed.connect(self._on_gain_changed)
            tw.mute_toggled.connect(self._on_mute_toggled)
            tw.solo_toggled.connect(self._on_solo_toggled)
            tw.eq_changed.connect(self._on_eq_changed)
            tw.mic_toggled.connect(self._on_mic_toggled)
            tw.aux_toggled.connect(self._on_aux_toggled)
            tw.xfade_assign_changed.connect(self._on_xfade_assign_changed)
            self._track_widgets.append(tw)
            # 8本のチャンネルストリップで利用可能な横幅を均等に使う。
            layout.addWidget(tw, 1)

        return container

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(48)
        header.setStyleSheet(f"background-color: #111; border-bottom: 1px solid {Colors.BORDER}; border-radius: 4px;")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 0, 16, 0)

        title = QLabel("MIXER 4TRACK")
        title.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: 18px; font-weight: bold; letter-spacing: 4px;")
        layout.addWidget(title)

        phase_badge = QLabel("Phase 25")
        phase_badge.setStyleSheet(f"""
            color: #111; background-color: {Colors.MASTER_ACCENT};
            font-size: 10px; font-weight: bold;
            padding: 2px 8px; border-radius: 3px;
        """)
        layout.addWidget(phase_badge)
        layout.addStretch()

        # プロジェクト名表示
        self._project_label = QLabel("Unsaved Project")
        self._project_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 10px;")
        layout.addWidget(self._project_label)

        layout.addStretch()

        subtitle = QLabel("v25.0")
        subtitle.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 10px;")
        layout.addWidget(subtitle)
        return header

    def _build_bank_bar(self) -> QWidget:
        """バンク切り替えバーを構築する。"""
        bar = QWidget()
        bar.setFixedHeight(44)
        bar.setStyleSheet(f"background-color: #111; border: 1px solid {Colors.BORDER}; border-radius: 4px;")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(12)

        bank_lbl = QLabel("BANK:")
        bank_lbl.setStyleSheet(f"color: {Colors.TEXT_LABEL}; font-size: 11px; font-weight: bold; letter-spacing: 2px;")
        layout.addWidget(bank_lbl)

        self._bank_a_btn = QPushButton("BANK A  [ 1 - 8 ]")
        self._bank_a_btn.setFixedSize(160, 32)
        self._bank_a_btn.clicked.connect(lambda: self._switch_bank(0))

        self._bank_b_btn = QPushButton("BANK B  [ 9 - 16 ]")
        self._bank_b_btn.setFixedSize(160, 32)
        self._bank_b_btn.clicked.connect(lambda: self._switch_bank(1))

        layout.addWidget(self._bank_a_btn)
        layout.addWidget(self._bank_b_btn)

        # 自動保存インジケーター
        self._autosave_label = QLabel("")
        self._autosave_label.setStyleSheet(f"color: {Colors.METER_LOW}; font-size: 9px;")
        layout.addWidget(self._autosave_label)

        layout.addStretch()

        # バンク説明
        self._bank_info_label = QLabel("Bank A: Tracks 1-8  |  Bank B: Tracks 9-16  |  Auto-save on bank switch")
        self._bank_info_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 9px;")
        layout.addWidget(self._bank_info_label)

        self._update_bank_buttons()
        return bar

    def _build_transport(self) -> QWidget:
        transport = QWidget()
        transport.setStyleSheet(f"background-color: #111; border: 1px solid {Colors.BORDER}; border-radius: 4px;")
        outer = QHBoxLayout(transport)
        outer.setContentsMargins(10, 7, 10, 7)
        outer.setSpacing(6)
        transport.setFixedHeight(50)

        # 一段に収めるため、操作頻度に合わせて小型化したトランスポート操作。
        self._undo_btn = QPushButton("↶ UNDO")
        self._undo_btn.setFixedSize(68, 30)
        self._undo_btn.setEnabled(False)
        self._undo_btn.setStyleSheet(self._transport_btn_style("#2c3e50", "#34495e", font_size=9))
        self._undo_btn.setToolTip("UNDO (履歴なし)")
        self._undo_btn.clicked.connect(self._on_undo)
        outer.addWidget(self._undo_btn)

        self._redo_btn = QPushButton("REDO ↷")
        self._redo_btn.setFixedSize(68, 30)
        self._redo_btn.setEnabled(False)
        self._redo_btn.setStyleSheet(self._transport_btn_style("#2c3e50", "#34495e", font_size=9))
        self._redo_btn.setToolTip("REDO (履歴なし)")
        self._redo_btn.clicked.connect(self._on_redo)
        outer.addWidget(self._redo_btn)
        outer.addSpacing(8)

        self._play_btn = QPushButton("▶  PLAY")
        self._play_btn.setFixedSize(96, 34)
        self._play_btn.setStyleSheet(self._transport_btn_style(Colors.BTN_PLAY, Colors.BTN_PLAY_HOV, font_size=10))
        self._play_btn.clicked.connect(self._on_play)
        outer.addWidget(self._play_btn)

        self._pause_btn = QPushButton("⏸  PAUSE")
        self._pause_btn.setFixedSize(96, 34)
        self._pause_btn.setEnabled(False)  # 初期は無効（再生中のみ有効）
        self._pause_btn.setStyleSheet(self._transport_btn_style(Colors.BTN_PAUSE, Colors.BTN_PAUSE_HOV, font_size=10))
        self._pause_btn.clicked.connect(self._on_pause)
        outer.addWidget(self._pause_btn)

        self._stop_btn = QPushButton("■  STOP")
        self._stop_btn.setFixedSize(90, 34)
        self._stop_btn.setStyleSheet(self._transport_btn_style(Colors.BTN_STOP, Colors.BTN_STOP_HOV, font_size=10))
        self._stop_btn.clicked.connect(self._on_stop)
        outer.addWidget(self._stop_btn)

        self._loop_btn = QPushButton("↻  LOOP")
        self._loop_btn.setFixedSize(78, 34)
        self._loop_btn.setStyleSheet(self._transport_btn_style("#34495e", "#46637d", font_size=9))
        self._loop_btn.setToolTip("ループ再生をON/OFF（範囲未指定時は全体をループ）")
        self._loop_btn.clicked.connect(self._on_loop_toggled)
        outer.addWidget(self._loop_btn)

        self._loop_in_btn = QPushButton("IN")
        self._loop_in_btn.setFixedSize(38, 30)
        self._loop_in_btn.setStyleSheet(self._transport_btn_style("#164b56", "#1b6978", font_size=9))
        self._loop_in_btn.setToolTip("現在位置をループ開始点に設定")
        self._loop_in_btn.clicked.connect(self._on_loop_in)
        outer.addWidget(self._loop_in_btn)

        self._loop_out_btn = QPushButton("OUT")
        self._loop_out_btn.setFixedSize(42, 30)
        self._loop_out_btn.setEnabled(False)
        self._loop_out_btn.setStyleSheet(self._transport_btn_style("#164b56", "#1b6978", font_size=9))
        self._loop_out_btn.setToolTip("INより後の現在位置をループ終了点に設定")
        self._loop_out_btn.clicked.connect(self._on_loop_out)
        outer.addWidget(self._loop_out_btn)

        self._loop_all_btn = QPushButton("ALL")
        self._loop_all_btn.setFixedSize(42, 30)
        self._loop_all_btn.setStyleSheet(self._transport_btn_style("#164b56", "#1b6978", font_size=9))
        self._loop_all_btn.setToolTip("最長トラックの全体範囲をループ")
        self._loop_all_btn.clicked.connect(self._on_loop_all)
        outer.addWidget(self._loop_all_btn)

        self._playing_indicator = QLabel("●")
        self._playing_indicator.setStyleSheet(f"color: #333; font-size: 14px;")
        outer.addWidget(self._playing_indicator)
        outer.addSpacing(8)

        self._save_btn = QPushButton("SAVE")
        self._save_btn.setFixedSize(64, 30)
        self._save_btn.setStyleSheet(self._transport_btn_style(Colors.BTN_SAVE, Colors.BTN_SAVE_HOV, font_size=9))
        self._save_btn.clicked.connect(self._on_save_project)
        outer.addWidget(self._save_btn)

        self._save_as_btn = QPushButton("SAVE AS")
        self._save_as_btn.setFixedSize(74, 30)
        self._save_as_btn.setStyleSheet(self._transport_btn_style(Colors.BTN_SAVE, Colors.BTN_SAVE_HOV, font_size=9))
        self._save_as_btn.clicked.connect(self._on_save_project_as)
        outer.addWidget(self._save_as_btn)

        self._open_btn = QPushButton("OPEN")
        self._open_btn.setFixedSize(64, 30)
        self._open_btn.setStyleSheet(self._transport_btn_style(Colors.BTN_OPEN, Colors.BTN_OPEN_HOV, font_size=9))
        self._open_btn.clicked.connect(self._on_open_project)
        outer.addWidget(self._open_btn)
        outer.addSpacing(8)

        self._add_marker_btn = QPushButton("▼  ADD MARKER")
        self._add_marker_btn.setFixedSize(104, 30)
        self._add_marker_btn.setStyleSheet(self._transport_btn_style("#5d4037", "#795548", font_size=8))
        self._add_marker_btn.setToolTip("現在の再生位置にマーカーを追加")
        self._add_marker_btn.clicked.connect(self._on_add_marker)
        outer.addWidget(self._add_marker_btn)

        self._marker_combo = QComboBox()
        self._marker_combo.setFixedSize(132, 30)
        self._marker_combo.setStyleSheet(f"""
            QComboBox {{
                background: #222; color: #ffd700; border: 1px solid #5d4037;
                border-radius: 4px; padding-left: 7px; font-size: 9px;
            }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{
                background: #222; color: #ffd700; border: 1px solid #5d4037;
                selection-background-color: #5d4037;
            }}
        """)
        self._marker_combo.addItem("▼ マーカーにジャンプ")
        self._marker_combo.currentIndexChanged.connect(self._on_marker_combo_jump)
        outer.addWidget(self._marker_combo)
        outer.addStretch(1)
        return transport

    @staticmethod
    def _transport_btn_style(bg: str, hover: str, font_size: int = 13) -> str:
        return f"""
            QPushButton {{
                background-color: {bg}; color: white; border: none; border-radius: 4px;
                font-size: {font_size}px; font-weight: bold; letter-spacing: 1px;
            }}
            QPushButton:hover {{ background-color: {hover}; }}
            QPushButton:pressed {{ background-color: #1a1a1a; }}
            QPushButton:disabled {{ background-color: #333; color: #666; }}
        """

    # ------------------------------------------------------------------
    # Phase 4: バンク切り替え
    # ------------------------------------------------------------------

    def _switch_bank(self, bank: int):
        """バンクを切り替える。切り替え前に自動保存を実行する。"""
        if bank == self._current_bank:
            return

        # 自動保存
        self._auto_save()

        self._current_bank = bank
        self._bank_a_widget.setVisible(bank == 0)
        self._bank_b_widget.setVisible(bank == 1)
        self._update_bank_buttons()

        bank_name = "A" if bank == 0 else "B"
        start = bank * self.TRACKS_PER_BANK + 1
        end   = start + self.TRACKS_PER_BANK - 1
        self._set_status(f"Switched to Bank {bank_name} (Tracks {start}-{end})  |  Auto-saved")

    def _update_bank_buttons(self):
        """バンクボタンのアクティブ/非アクティブ状態を更新する。"""
        a_active = self._current_bank == 0
        b_active = self._current_bank == 1

        a_bg  = Colors.BTN_BANK_A_ACT if a_active else Colors.BTN_BANK_A
        a_bdr = Colors.BTN_BANK_A_ACT if a_active else "#3a3a3a"
        b_bg  = Colors.BTN_BANK_B_ACT if b_active else Colors.BTN_BANK_B
        b_bdr = Colors.BTN_BANK_B_ACT if b_active else "#3a3a3a"

        self._bank_a_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {a_bg}; color: white;
                border: 2px solid {a_bdr}; border-radius: 4px;
                font-size: 11px; font-weight: bold; letter-spacing: 1px;
            }}
            QPushButton:hover {{ background-color: #5ba3e8; }}
        """)
        self._bank_b_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {b_bg}; color: white;
                border: 2px solid {b_bdr}; border-radius: 4px;
                font-size: 11px; font-weight: bold; letter-spacing: 1px;
            }}
            QPushButton:hover {{ background-color: #e74c3c; }}
        """)

    def _auto_save(self):
        """バンク切り替え時の自動保存。保存先がなければデフォルトパスに保存する。"""
        path = self._current_project_path
        if path is None:
            # 初回はデフォルトパスに保存
            import os
            default_dir = os.path.join(os.path.expanduser("~"), "Documents", "Mixer4Track")
            path = os.path.join(default_dir, "autosave.m4t")

        store = ProjectStore(project_path=path)
        master_vol = self._engine.get_master_volume()
        limiter_enabled, limiter_ceiling_db, limiter_release_ms = self._engine.get_master_limiter_state()
        master_xfade = self._engine.get_master_xfade_state()
        ok = store.save(self._tracks, master_volume=master_vol, current_bank=self._current_bank,
                        markers=self._marker_manager.get_all(), master_limiter={
                            "enabled": limiter_enabled,
                            "ceiling_db": limiter_ceiling_db,
                            "release_ms": limiter_release_ms,
                        }, master_xfade=master_xfade)
        if ok:
            if self._current_project_path is None:
                self._current_project_path = path
                self._update_project_label(path)
            # 自動保存インジケーターを一時的に表示
            self._autosave_label.setText("  Auto-saved")
            QTimer.singleShot(3000, lambda: self._autosave_label.setText(""))

    # ------------------------------------------------------------------
    # タイマー（レベルメーター更新）
    # ------------------------------------------------------------------

    def _setup_timer(self):
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._update_meters)
        self._timer.start()
        self._pseudo_levels = [0.0] * self.NUM_TRACKS
        self._master_pseudo_level = 0.0
        self._rec_blink_state = False   # REC STOPボタンの点滅用
        self._rec_blink_counter = 0     # 50msティックカウンター
        self._rec_duration_sec: float = 0.0  # 録音完了時の時間保持

    def _update_meters(self):
        any_solo = any(t.solo for t in self._tracks)
        is_playing = self._engine.is_playing()
        master_vol = self._engine.get_master_volume()

        for i, (track, tw) in enumerate(zip(self._tracks, self._track_widgets)):
            if is_playing:
                base = self._engine.get_level(i, track, any_solo)
                if base > 0:
                    target = base * (0.6 + random.random() * 0.4)
                    self._pseudo_levels[i] += (target - self._pseudo_levels[i]) * 0.3
                else:
                    self._pseudo_levels[i] *= 0.8
                tw.update_level(self._pseudo_levels[i])
            else:
                self._pseudo_levels[i] *= 0.85
                tw.update_level(self._pseudo_levels[i])

            # スペクトラムアナライザー更新
            try:
                bands = self._engine.get_spectrum_bands(i)
                tw._eq_curve.update_spectrum(bands)
            except Exception:
                pass

            # シークバー更新
            try:
                pos = self._engine.get_track_position_sec(track.track_id)
                dur = self._engine.get_sound_duration(track.track_id)
                tw.update_seek_bar(pos, dur)
                # 拡大ウィンドウにも同期
                if (tw._eq_curve._expanded_win is not None
                        and not tw._eq_curve._expanded_win.isHidden()):
                    tw._eq_curve._expanded_win.update_seek_bar(pos, dur)
            except Exception:
                pass

        # MASTERメーター
        if is_playing:
            avg = sum(self._pseudo_levels) / max(len(self._pseudo_levels), 1)
            target_m = avg * master_vol * (0.7 + random.random() * 0.3)
            self._master_pseudo_level += (target_m - self._master_pseudo_level) * 0.3
        else:
            self._master_pseudo_level *= 0.85
        if self._master_widget:
            self._master_widget.update_level(self._master_pseudo_level)
            # MASTERのGEQカーブにもスペクトル更新（全トラックの平均）
            try:
                import numpy as _np
                all_bands = [self._engine.get_spectrum_bands(i) for i in range(self.NUM_TRACKS)]
                valid = [b for b in all_bands if b is not None and len(b) > 0]
                if valid:
                    avg_bands = _np.mean(valid, axis=0)
                    self._master_widget._geq_curve.update_spectrum(avg_bands)
            except Exception:
                pass

            # VUメーター更新（リアルレベル取得）
            try:
                rms_l, rms_r, peak_l, peak_r, clip_l, clip_r = \
                    self._engine.get_vu_levels()
                # 非再生中は減衰
                if not is_playing:
                    rms_l = rms_r = 0.0
                    peak_l = peak_r = 0.0
                self._master_widget.update_vu_meter(
                    rms_l, rms_r, peak_l, peak_r, clip_l, clip_r
                )
                self._master_widget.update_limiter_reduction(
                    self._engine.get_master_limiter_reduction_db()
                )
            except Exception:
                pass

        if is_playing:
            self._playing_indicator.setStyleSheet(f"color: {Colors.METER_LOW}; font-size: 16px;")
        else:
            self._playing_indicator.setStyleSheet(f"color: #333; font-size: 16px;")
            # 再生終了時（トラックが最後まで再生し終わった場合）にボタンをリセット
            if self._pause_btn.isEnabled():
                self._play_btn.setEnabled(True)
                self._pause_btn.setEnabled(False)
                self._pause_btn.setText("⏸  PAUSE")
                self._pause_btn.setStyleSheet(
                    self._transport_btn_style(Colors.BTN_PAUSE, Colors.BTN_PAUSE_HOV))

        # REC STOPボタンのリアルタイム更新（録音中のみ）
        if self._master_widget and self._engine._rec_active:
            self._rec_blink_counter += 1
            dur = self._engine.get_rec_duration_sec()
            m = int(dur // 60)
            s = dur % 60
            # 10ティック（500ms）ごとに点滅（ドット表示切り替え）
            if self._rec_blink_counter % 10 < 5:
                dot = "\u25cf"
            else:
                dot = "\u25cb"
            self._master_widget._rec_stop_btn.setText(f"REC {dot} {m}:{s:04.1f}")
            self._master_widget._rec_stop_btn.setStyleSheet("""
                QPushButton {
                    background-color: #c0392b; color: #fff;
                    border: 2px solid #ff6b6b; border-radius: 3px;
                    font-size: 9px; font-weight: bold; letter-spacing: 1px;
                }
                QPushButton:hover { background-color: #e74c3c; }
                QPushButton:pressed { background-color: #4a0f0f; }
            """)

    # ------------------------------------------------------------------
    # スロット（トラック操作）
    # ------------------------------------------------------------------

    def _on_load_file(self, track_id: int):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Track {track_id + 1} - Select Audio File",
            os.path.expanduser("~"),
            "Audio Files (*.wav *.mp3 *.ogg *.flac);;All Files (*)"
        )
        if not path:
            return
        # バックグラウンドで読み込み（UIFreezeを防ぐ）
        self._track_widgets[track_id].set_loading(True)
        self._set_status(f"Track {track_id + 1}: Loading...")
        worker = LoadWorker(self._engine, track_id, path)
        worker.finished.connect(self._on_load_finished)
        self._load_workers.append(worker)
        worker.finished.connect(lambda *_: self._load_workers.remove(worker) if worker in self._load_workers else None)
        worker.start()

    @pyqtSlot(int, bool, str)
    def _on_load_finished(self, track_id: int, ok: bool, path: str):
        self._track_widgets[track_id].set_loading(False)
        if ok:
            self._tracks[track_id].file_path = path
            self._track_widgets[track_id].update_file_label(os.path.basename(path))
            # EQカーブを更新（現在のEQパラメータで再描画）
            track = self._tracks[track_id]
            from eq_engine import EQParams
            eq_params = EQParams(
                low_gain_db=track.eq_low_gain,
                mid_gain_db=track.eq_mid_gain,
                mid_freq_hz=track.eq_mid_freq,
                mid_q=track.eq_mid_q,
                high_gain_db=track.eq_high_gain,
            )
            self._track_widgets[track_id].update_eq_curve(eq_params)
            # 波形サムネイルをシークバーに設定
            try:
                peaks = self._engine.get_waveform_peaks(track_id, num_points=200)
                self._track_widgets[track_id].set_seek_bar_peaks(peaks)
                dur = self._engine.get_sound_duration(track_id)
                self._track_widgets[track_id].update_seek_bar(0.0, dur)
            except Exception:
                pass
            # 最長トラックに合わせてマーカー/ループ範囲のスケールを更新
            self._refresh_marker_ui()
            if self._engine.is_loop_enabled() and self._marker_bar:
                active, start, end = self._engine.get_loop_range()
                self._marker_bar.set_loop_range(active, start, end)
            self._set_status(f"Track {track_id + 1}: {os.path.basename(path)} loaded")
        else:
            QMessageBox.warning(self, "Load Error",
                f"Could not load file:\n{path}\n\nPlease select a WAV or MP3 file.")

    def _on_volume_changed(self, track_id: int, volume: float):
        from project_store import VolumeCommand
        old_vol = self._tracks[track_id].volume
        self._tracks[track_id].volume = volume
        any_solo = any(t.solo for t in self._tracks)
        self._engine.update_track(self._tracks[track_id], any_solo)
        # UNDO記録（値が変化した場合のみ）
        if abs(old_vol - volume) > 0.001:
            def _apply_vol(tid, v):
                self._tracks[tid].volume = v
                self._engine.update_track(self._tracks[tid], any(t.solo for t in self._tracks))
            def _label_vol(tid, v):
                self._track_widgets[tid].set_volume_silent(v)
            cmd = VolumeCommand(track_id, old_vol, volume, _apply_vol, _label_vol)
            self._history.push(cmd)
            self._update_undo_redo_buttons()

    def _on_pan_changed(self, track_id: int, pan: float):
        from project_store import PanCommand
        old_pan = self._tracks[track_id].pan
        self._tracks[track_id].pan = pan
        any_solo = any(t.solo for t in self._tracks)
        self._engine.update_track(self._tracks[track_id], any_solo)
        if abs(old_pan - pan) > 0.001:
            def _apply_pan(tid, p):
                self._tracks[tid].pan = p
                self._engine.update_track(self._tracks[tid], any(t.solo for t in self._tracks))
            def _label_pan(tid, p):
                self._track_widgets[tid].set_pan_silent(p)
            cmd = PanCommand(track_id, old_pan, pan, _apply_pan, _label_pan)
            self._history.push(cmd)
            self._update_undo_redo_buttons()

    def _on_gain_changed(self, track_id: int, gain_db: float):
        """
        ゲイン変更時のコールバック。
        TrackModelを更新し、AudioEngineに信号チェーン再生成を依頼する。
        """
        from project_store import GainCommand
        old_gain = self._tracks[track_id].gain_db
        self._tracks[track_id].gain_db = gain_db
        self._engine.update_gain(track_id, gain_db)
        if abs(old_gain - gain_db) > 0.001:
            def _apply_gain(tid, g):
                self._tracks[tid].gain_db = g
                self._engine.update_gain(tid, g)
            def _label_gain(tid, g):
                self._track_widgets[tid].set_gain_silent(g)
            cmd = GainCommand(track_id, old_gain, gain_db, _apply_gain, _label_gain)
            self._history.push(cmd)
            self._update_undo_redo_buttons()

    def _on_mute_toggled(self, track_id: int):
        from project_store import MuteCommand
        track = self._tracks[track_id]
        old_muted = track.muted
        new_muted = not track.muted
        def _apply_mute(tid, v):
            self._tracks[tid].muted = v
            self._track_widgets[tid].update_mute_state(v)
            self._engine.update_all_tracks(self._tracks)
        _apply_mute(track_id, new_muted)
        self._set_status(f"Track {track_id + 1}: {'Muted' if new_muted else 'Unmuted'}")
        cmd = MuteCommand(track_id, old_muted, new_muted, _apply_mute)
        self._history.push(cmd)
        self._update_undo_redo_buttons()

    def _on_solo_toggled(self, track_id: int):
        from project_store import SoloCommand
        track = self._tracks[track_id]
        old_solo = track.solo
        new_solo = not track.solo
        def _apply_solo(tid, v):
            self._tracks[tid].solo = v
            self._track_widgets[tid].update_solo_state(v)
            self._engine.update_all_tracks(self._tracks)
        _apply_solo(track_id, new_solo)
        self._set_status(f"Track {track_id + 1}: Solo {'ON' if new_solo else 'OFF'}")
        cmd = SoloCommand(track_id, old_solo, new_solo, _apply_solo)
        self._history.push(cmd)
        self._update_undo_redo_buttons()

    def _on_mic_toggled(self, track_id: int):
        """
        MICボタンクリック時のコールバック。
        - MICが未割り当ての場合: デバイス選択ダイアログを開き、割り当てる
        - MICが既に割り当て済みの場合: 解除する
        """
        if mic_engine.has_mic(track_id):
            # 既に割り当て済み → 解除
            mic_engine.release_mic(track_id)
            self._track_widgets[track_id].update_mic_state(False)
            self._set_status(f"Track {track_id + 1}: MIC 入力解除")
        else:
            # 未割り当て → デバイス選択ダイアログを開く
            dlg = MicDeviceDialog(self)
            if dlg.exec_() != QDialog.Accepted:
                return
            result = dlg.selected_device()
            if result is None:
                return
            dev_index, dev_name = result
            ok = mic_engine.assign_mic(track_id, dev_index, dev_name)
            if ok:
                self._track_widgets[track_id].update_mic_state(True, dev_name)
                # MICループを開始（ファイル再生中でも独立して動作）
                self._engine.start_mic_loop(self._tracks)
                self._set_status(f"Track {track_id + 1}: MIC 入力 「{dev_name}」 割り当て済み")
            else:
                QMessageBox.warning(self, "MIC エラー",
                    f"マイクデバイスの引き当てに失敗しました。\nデバイス: {dev_name}")

    def _on_aux_toggled(self, track_id: int):
        """
        AUXボタンクリック時のコールバック。
        AUX状態をトグルし、エンジンに即座反映する。
        """
        from project_store import AuxCommand
        track = self._tracks[track_id]
        old_aux = track.aux_enabled
        new_aux = not track.aux_enabled
        def _apply_aux(tid, v):
            self._tracks[tid].aux_enabled = v
            self._track_widgets[tid].update_aux_state(v)
            self._engine.set_aux_track(tid, v)
        _apply_aux(track_id, new_aux)
        state = "ON" if new_aux else "OFF"
        self._set_status(f"Track {track_id + 1}: AUX {state} — FXは{'AUX ONトラックのみ' if new_aux else '全トラックに適用'}に変更")
        cmd = AuxCommand(track_id, old_aux, new_aux, _apply_aux)
        self._history.push(cmd)
        self._update_undo_redo_buttons()

    def _on_xfade_assign_changed(self, track_id: int, assign: str):
        """Phase 25: トラックのA/B/THRU割当を音声Brokerへ反映する。"""
        self._tracks[track_id].xfade_assign = assign
        self._engine.set_track_xfade_assign(track_id, assign)
        self._set_status(f"Track {track_id + 1}: X-FADER {assign}")

    def _on_eq_changed(self, track_id: int, params):
        """
        EQノブまたはプリセット変更時のコールバック。
        TrackModelにEQパラメータを同期し、AudioEngineにリアルタイム適用を依頼する。
        EQ再生成はバックグラウンドで実行（UIFreeze防止）。
        """
        from project_store import EQCommand
        from eq_engine import EQParams
        track = self._tracks[track_id]
        # 変更前のパラメータを保存
        old_params = EQParams(
            low_gain_db=track.eq_low_gain,
            mid_gain_db=track.eq_mid_gain,
            mid_freq_hz=track.eq_mid_freq,
            mid_q=track.eq_mid_q,
            high_gain_db=track.eq_high_gain,
        )
        track.eq_low_gain  = params.low_gain_db
        track.eq_mid_gain  = params.mid_gain_db
        track.eq_mid_freq  = params.mid_freq_hz
        track.eq_mid_q     = params.mid_q
        track.eq_high_gain = params.high_gain_db
        # EQカーブを即時更新
        self._track_widgets[track_id].update_eq_curve(params)
        # リアルタイム反映（次チャンクから即適用）
        self._engine.update_eq(track_id, params)
        # UNDO記録
        def _apply_eq(tid, p):
            t = self._tracks[tid]
            t.eq_low_gain = p.low_gain_db
            t.eq_mid_gain = p.mid_gain_db
            t.eq_mid_freq = p.mid_freq_hz
            t.eq_mid_q    = p.mid_q
            t.eq_high_gain = p.high_gain_db
            self._track_widgets[tid].update_eq_curve(p)
            self._engine.update_eq(tid, p)
        cmd = EQCommand(track_id, old_params, params, _apply_eq)
        self._history.push(cmd)
        self._update_undo_redo_buttons()

    def _on_play(self):
        loaded = [t for t in self._tracks if t.file_path is not None]
        if not loaded:
            QMessageBox.information(self, "Cannot Play",
                "Please load at least one audio file.")
            return
        self._engine.play_all(self._tracks)
        self._set_status("Playing...")
        # ボタン状態遷移: 再生中
        self._play_btn.setEnabled(False)
        self._pause_btn.setEnabled(True)
        self._pause_btn.setText("⏸  PAUSE")
        self._pause_btn.setStyleSheet(
            self._transport_btn_style(Colors.BTN_PAUSE, Colors.BTN_PAUSE_HOV))

    def _on_pause(self):
        if self._engine.is_paused():
            # ポーズ中 → 再開
            self._engine.resume()
            self._set_status("Playing...")
            self._pause_btn.setText("⏸  PAUSE")
            self._pause_btn.setStyleSheet(
                self._transport_btn_style(Colors.BTN_PAUSE, Colors.BTN_PAUSE_HOV))
            self._play_btn.setEnabled(False)
        else:
            # 再生中 → ポーズ
            self._engine.pause()
            self._set_status("⏸ Paused")
            self._pause_btn.setText("▶  RESUME")
            self._pause_btn.setStyleSheet(
                self._transport_btn_style(Colors.BTN_RESUME, Colors.BTN_RESUME_HOV))
            self._play_btn.setEnabled(False)

    def _on_stop(self):
        self._engine.stop_all()
        self._set_status("Stopped")
        # ボタン状態遷移: 停止中
        self._play_btn.setEnabled(True)
        self._pause_btn.setEnabled(False)
        self._pause_btn.setText("⏸  PAUSE")
        self._pause_btn.setStyleSheet(
            self._transport_btn_style(Colors.BTN_PAUSE, Colors.BTN_PAUSE_HOV))

    def _on_master_volume_changed(self, volume: float):
        from project_store import MasterVolumeCommand
        old_vol = self._engine.get_master_volume()
        self._engine.set_master_volume(volume)
        if self._engine.is_playing():
            self._engine.update_all_tracks(self._tracks)
        if abs(old_vol - volume) > 0.001:
            def _apply_master_vol(v):
                self._engine.set_master_volume(v)
                if self._master_widget:
                    self._master_widget.restore_state(v)
            cmd = MasterVolumeCommand(old_vol, volume, _apply_master_vol)
            self._history.push(cmd)
            self._update_undo_redo_buttons()

    def _on_master_limiter_changed(self, enabled: bool, ceiling_db: float):
        """Phase 23: マスター・リミッター操作を最終出力へ即時反映する。"""
        self._engine.set_master_limiter(enabled, ceiling_db)
        state = "ON" if enabled else "BYPASS"
        self._set_status(f"MASTER LIMITER: {state} / Ceiling {ceiling_db:.1f} dB")

    def _on_master_xfade_changed(self, position: float, curve: str, cut_a: bool, cut_b: bool):
        """Phase 25: MASTER X-FADERをBrokerへ反映する。"""
        self._engine.set_master_xfade(position, curve, cut_a, cut_b)
        curve_label = curve.replace("_", " ").upper()
        self._set_status(f"X-FADER: {position * 100:.0f}% / {curve_label}")

    def _on_master_effect_changed(self, _track_id: int, preset_name: str, enabled: bool):
        """
        Phase 6: MASTERエフェクト変更コールバック。
        全トラックに同じエフェクトプリセットを適用する（マスターエフェクト）。
        バックグラウンドスレッドで処理してUIをブロックしない。
        """
        # TrackModelを先に更新（保存時に必要）
        for track in self._tracks:
            track.effect_preset  = preset_name
            track.effect_enabled = enabled

        # ファイルが読み込まれているトラックのみバックグラウンドでエフェクト適用
        loaded_tracks = [t for t in self._tracks if t.file_path is not None]
        if not loaded_tracks:
            self._set_status(f"FX: {preset_name} {'ON' if enabled else 'OFF'} (no files loaded)")
            return

        # リアルタイム反映（次チャンクから即適用）
        for track in loaded_tracks:
            self._engine.update_effect(track.track_id, preset_name, enabled)
        self._set_status(f"FX: {preset_name} {'ON' if enabled else 'OFF'} - Active")

    def _on_geq_mode_changed(self, mode: str):
        """
        GEQ Low/Hi ボタン切り替え時のコールバック。
        mode: 'low' / 'hi' / 'off'
        現在表示中のバンク（A or B）の8トラックのフェーダーをGEQコントローラーに切り替える。
        """
        self._geq_mode = mode

        # 現在表示中のバンクのトラックウィジェット（8本）を取得
        bank_start = self._current_bank * self.TRACKS_PER_BANK
        bank_widgets = self._track_widgets[bank_start:bank_start + self.TRACKS_PER_BANK]

        if mode == "off":
            # 全トラックをノーマルモードに戻す
            for tw in bank_widgets:
                tw.exit_geq_mode()
            self._set_status("GEQ: OFF")
        else:
            # GEQ Low または Hi のバンドリストを取得
            from geq_engine import GEQ_LOW_BANDS, GEQ_HI_BANDS
            bands = GEQ_LOW_BANDS if mode == "low" else GEQ_HI_BANDS
            for i, tw in enumerate(bank_widgets):
                if i < len(bands):
                    name, freq = bands[i]  # GEQ_LOW_BANDSは(name, freq)の順
                    current_gain = self._geq_params.get_gain(freq)
                    tw.enter_geq_mode(name, freq, current_gain)
                    # geq_band_changedシグナルを接続（まだ接続されていない場合）
                    try:
                        tw.geq_band_changed.disconnect(self._on_geq_band_changed)
                    except TypeError:
                        pass
                    tw.geq_band_changed.connect(self._on_geq_band_changed)
            mode_name = "GEQ Low (31Hz-630Hz)" if mode == "low" else "GEQ Hi (800Hz-16kHz)"
            self._set_status(f"GEQ: {mode_name} - フェーダーでバンドゲインを調整")

    def _on_geq_band_changed(self, _track_id: int, band_freq: float, gain_db: float):
        """
        GEQモード中にフェーダーが動いたときのコールバック。
        GEQパラメータを更新してAudioEngineにリアルタイム反映する。
        """
        from project_store import GEQCommand
        old_gain = self._geq_params.get_gain(band_freq)
        self._geq_params.set_gain(band_freq, gain_db)
        # MASTERカーブを更新
        if self._master_widget:
            self._master_widget.update_geq_curve(self._geq_params)
        # AudioEngineにリアルタイム反映
        self._engine.update_master_geq(self._geq_params)
        # UNDO記録
        if abs(old_gain - gain_db) > 0.001:
            def _apply_geq(freq, g):
                self._geq_params.set_gain(freq, g)
                if self._master_widget:
                    self._master_widget.update_geq_curve(self._geq_params)
                self._engine.update_master_geq(self._geq_params)
            cmd = GEQCommand(band_freq, old_gain, gain_db, _apply_geq)
            self._history.push(cmd)
            self._update_undo_redo_buttons()

    # ------------------------------------------------------------------
    # Phase 22: ループ再生
    # ------------------------------------------------------------------

    @staticmethod
    def _format_time_sec(sec: float) -> str:
        """秒を m:ss.s 形式に整形する。"""
        return f"{int(sec // 60)}:{sec % 60:04.1f}"

    def _get_timeline_position_sec(self) -> float:
        """読み込み済みトラックから共通タイムライン上の現在位置を取得する。"""
        for track in self._tracks:
            if track.file_path:
                return self._engine.get_track_position_sec(track.track_id)
        return 0.0

    def _on_loop_in(self):
        """現在位置をループ開始点（IN）に設定する。"""
        duration = self._engine.get_timeline_duration_sec()
        if duration <= 0.0:
            QMessageBox.information(self, "Loop", "先に少なくとも1つの音声ファイルを読み込んでください。")
            return
        pos = min(self._get_timeline_position_sec(),
                  max(0.0, duration - 1.0 / self._engine.SAMPLE_RATE))
        self._loop_in_sec = pos
        self._loop_out_sec = None
        # 既存のループは明示的に解除し、新しいOUT指定を待つ
        if self._engine.is_loop_enabled():
            self._engine.clear_loop_range()
            if self._marker_bar:
                self._marker_bar.set_loop_range(False)
        self._update_loop_controls()
        self._set_status(f"LOOP IN: {self._format_time_sec(pos)}  |  OUTを押して範囲を確定")

    def _on_loop_out(self):
        """現在位置をループ終了点（OUT）に設定し、範囲ループを有効化する。"""
        if self._loop_in_sec is None:
            QMessageBox.information(self, "Loop", "先にINを押してループ開始点を設定してください。")
            return
        duration = self._engine.get_timeline_duration_sec()
        pos = min(self._get_timeline_position_sec(), duration)
        # UI操作時の誤操作を避け、最低0.05秒の範囲を要求する
        if pos <= self._loop_in_sec + 0.05:
            QMessageBox.information(self, "Loop", "OUTはINより0.05秒以上後に設定してください。")
            return
        self._loop_out_sec = pos
        self._apply_loop_range(self._loop_in_sec, self._loop_out_sec)

    def _on_loop_all(self):
        """最長トラックの全体範囲をループ対象に設定する。"""
        duration = self._engine.get_timeline_duration_sec()
        if duration <= 0.0:
            QMessageBox.information(self, "Loop", "先に少なくとも1つの音声ファイルを読み込んでください。")
            return
        self._loop_in_sec = 0.0
        self._loop_out_sec = duration
        self._apply_loop_range(self._loop_in_sec, self._loop_out_sec)

    def _on_loop_toggled(self):
        """LOOPボタンでループ再生をON/OFFする。"""
        if self._engine.is_loop_enabled():
            self._clear_loop_range()
            return

        duration = self._engine.get_timeline_duration_sec()
        if duration <= 0.0:
            QMessageBox.information(self, "Loop", "先に少なくとも1つの音声ファイルを読み込んでください。")
            return
        # IN/OUTが確定済みなら選択範囲、なければトラック全体を採用
        if (self._loop_in_sec is not None and self._loop_out_sec is not None
                and self._loop_out_sec > self._loop_in_sec):
            self._apply_loop_range(self._loop_in_sec, self._loop_out_sec)
        else:
            self._loop_in_sec = 0.0
            self._loop_out_sec = duration
            self._apply_loop_range(0.0, duration)

    def _apply_loop_range(self, start_sec: float, end_sec: float):
        """エンジンとUIにループ範囲を反映する。"""
        if not self._engine.set_loop_range(start_sec, end_sec, enabled=True):
            QMessageBox.warning(self, "Loop", "ループ範囲を設定できませんでした。INとOUTを確認してください。")
            return
        _, start, end = self._engine.get_loop_range()
        self._loop_in_sec = start
        self._loop_out_sec = end
        if self._marker_bar:
            self._marker_bar.set_loop_range(True, start, end)
        self._update_loop_controls()
        self._set_status(f"LOOP ON: {self._format_time_sec(start)} – {self._format_time_sec(end)}")

    def _clear_loop_range(self):
        """ループをOFFにし、範囲表示を解除する。"""
        self._engine.clear_loop_range()
        if self._marker_bar:
            self._marker_bar.set_loop_range(False)
        self._update_loop_controls()
        self._set_status("LOOP OFF: 通常再生に戻りました")

    def _update_loop_controls(self):
        """ループの有効状態とIN/OUT状態に合わせてボタン表示を更新する。"""
        active, start, end = self._engine.get_loop_range()
        if self._loop_btn:
            self._loop_btn.setText("↻  LOOP ON" if active else "↻  LOOP")
            if active:
                self._loop_btn.setStyleSheet(self._transport_btn_style("#007a88", "#0099a9", font_size=10))
                self._loop_btn.setToolTip(
                    f"LOOP ON: {self._format_time_sec(start)} – {self._format_time_sec(end)}（クリックでOFF）")
            else:
                self._loop_btn.setStyleSheet(self._transport_btn_style("#34495e", "#46637d", font_size=11))
                self._loop_btn.setToolTip("ループ再生をON/OFF（範囲未指定時は全体をループ）")
        if self._loop_in_btn:
            self._loop_in_btn.setText("IN ●" if self._loop_in_sec is not None else "IN")
            self._loop_in_btn.setToolTip(
                f"LOOP IN: {self._format_time_sec(self._loop_in_sec)}" if self._loop_in_sec is not None
                else "現在位置をループ開始点に設定")
        if self._loop_out_btn:
            self._loop_out_btn.setEnabled(self._loop_in_sec is not None)
            self._loop_out_btn.setText("OUT ●" if self._loop_out_sec is not None else "OUT")
            self._loop_out_btn.setToolTip(
                f"LOOP OUT: {self._format_time_sec(self._loop_out_sec)}" if self._loop_out_sec is not None
                else "INより後の現在位置をループ終了点に設定")

    # ------------------------------------------------------------------
    # Phase 21: UNDO / REDO
    # ------------------------------------------------------------------

    def _on_undo(self):
        """UNDO操作を実行する。"""
        desc = self._history.undo()
        if desc:
            self._set_status(f"UNDO: {desc}")
        else:
            self._set_status("UNDO: 履歴なし")
        self._update_undo_redo_buttons()

    def _on_redo(self):
        """REDO操作を実行する。"""
        desc = self._history.redo()
        if desc:
            self._set_status(f"REDO: {desc}")
        else:
            self._set_status("REDO: 履歴なし")
        self._update_undo_redo_buttons()

    def _update_undo_redo_buttons(self):
        """ボタンの有効/無効とツールチップを更新する。"""
        if self._undo_btn:
            can = self._history.can_undo()
            self._undo_btn.setEnabled(can)
            desc = self._history.undo_description()
            self._undo_btn.setToolTip(f"UNDO: {desc}" if desc else "UNDO (履歴なし)")
        if self._redo_btn:
            can = self._history.can_redo()
            self._redo_btn.setEnabled(can)
            desc = self._history.redo_description()
            self._redo_btn.setToolTip(f"REDO: {desc}" if desc else "REDO (履歴なし)")

    # ------------------------------------------------------------------
    # Phase 14: REC START / REC STOP / EXPORT WAV
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_rec_start(self):
        """録音開始スロット。"""
        self._engine.start_rec()
        self._rec_blink_counter = 0
        self._rec_blink_state = False
        if self._master_widget:
            # ボタン状態: EXPORT無効 / REC START無効 / REC STOP有効
            self._master_widget._export_btn.setEnabled(False)
            self._master_widget._export_btn.setText("EXPORT WAV")
            self._master_widget._rec_start_btn.setEnabled(False)
            self._master_widget._rec_stop_btn.setEnabled(True)
            self._master_widget._rec_stop_btn.setText("REC \u25cf 0:00.0")
        self._set_status("REC: 録音中... REC STOPを押すと録音を停止します")

    @pyqtSlot()
    def _on_rec_stop(self):
        """録音停止スロット。"""
        dur = self._engine.stop_rec()
        self._rec_duration_sec = dur
        m = int(dur // 60)
        s = dur % 60
        dur_str = f"{m}:{s:04.1f}"
        if self._master_widget:
            # ボタン状態: EXPORT有効(時間表示) / REC START有効 / REC STOP無効
            self._master_widget._export_btn.setEnabled(True)
            self._master_widget._export_btn.setText(f"EXPORT WAV ({dur_str})")
            self._master_widget._export_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {Colors.BTN_EXPORT}; color: {Colors.TEXT_PRIMARY};
                    border: 2px solid #e056ef; border-radius: 3px;
                    font-size: 9px; font-weight: bold; letter-spacing: 1px;
                }}
                QPushButton:hover {{ background-color: {Colors.BTN_EXPORT_HOV}; }}
                QPushButton:pressed {{ background-color: #4a235a; }}
                QPushButton:disabled {{ background-color: #333; color: #666; border: 1px solid #555; }}
            """)
            self._master_widget._rec_start_btn.setEnabled(True)
            self._master_widget._rec_stop_btn.setEnabled(False)
            self._master_widget._rec_stop_btn.setText("REC STOP")
            self._master_widget._rec_stop_btn.setStyleSheet("""
                QPushButton {
                    background-color: #5c1a1a; color: #e8e8e8;
                    border: 1px solid #c0392b; border-radius: 3px;
                    font-size: 9px; font-weight: bold; letter-spacing: 1px;
                }
                QPushButton:hover { background-color: #c0392b; color: #fff; }
                QPushButton:pressed { background-color: #4a0f0f; }
                QPushButton:disabled { background-color: #333; color: #666; border: 1px solid #555; }
            """)
        self._set_status(f"REC: 録音完了 ({dur_str}) | EXPORT WAVで書き出せます")

    # ------------------------------------------------------------------
    # Phase 20: マーカー機能
    # ------------------------------------------------------------------

    def _on_add_marker(self):
        """現在の再生位置にマーカーを追加する。"""
        # 現在再生位置を全トラックから取得（再生中のトラックの位置を使用）
        pos_sec = 0.0
        for i in range(self.NUM_TRACKS):
            p = self._engine.get_track_position_sec(i)
            if p > 0.0:
                pos_sec = p
                break

        from PyQt5.QtWidgets import QInputDialog
        label, ok = QInputDialog.getText(
            self, "マーカーを追加",
            f"マーカー名（空白で時間表示）:",
            text=""
        )
        if not ok:
            return
        marker = self._marker_manager.add(pos_sec, label.strip())
        self._refresh_marker_ui()
        m = int(pos_sec // 60)
        s = pos_sec % 60
        self._set_status(f"マーカー追加: {marker.get_display_label()} @ {m}:{s:04.1f}")

    def _on_marker_jump(self, time_sec: float):
        """マーカーバークリックで指定位置に全トラックを同期シークする。"""
        self._engine.seek_all_tracks(time_sec)
        m = int(time_sec // 60)
        s = time_sec % 60
        self._set_status(f"マーカージャンプ: {m}:{s:04.1f}")

    def _on_marker_delete(self, marker_id: int):
        """マーカーを削除する。"""
        self._marker_manager.remove(marker_id)
        self._refresh_marker_ui()
        self._set_status("マーカーを削除しました")

    def _on_marker_rename(self, marker_id: int, new_label: str):
        """マーカー名を変更する。"""
        self._marker_manager.rename(marker_id, new_label)
        self._refresh_marker_ui()

    def _on_marker_combo_jump(self, index: int):
        """ドロップダウンからマーカーにジャンプする。"""
        if index <= 0:
            return
        markers = self._marker_manager.get_all()
        idx = index - 1  # 先頭の「マーカーにジャンプ」分を引く
        if 0 <= idx < len(markers):
            self._on_marker_jump(markers[idx].time_sec)
        # 選択を先頭に戻す
        self._marker_combo.blockSignals(True)
        self._marker_combo.setCurrentIndex(0)
        self._marker_combo.blockSignals(False)

    def _refresh_marker_ui(self):
        """マーカーバーとドロップダウンを更新する。"""
        markers = self._marker_manager.get_all()
        # 最長トラックの総時間を取得
        duration = 0.0
        for i in range(self.NUM_TRACKS):
            d = self._engine.get_track_duration_sec(i)
            if d > duration:
                duration = d
        if self._marker_bar:
            self._marker_bar.set_markers(markers, duration)
        # ドロップダウンを更新
        self._marker_combo.blockSignals(True)
        self._marker_combo.clear()
        self._marker_combo.addItem("▼ マーカーにジャンプ")
        for m in markers:
            mi = int(m.time_sec // 60)
            ms = m.time_sec % 60
            self._marker_combo.addItem(f"{m.get_display_label()}  [{mi}:{ms:04.1f}]")
        self._marker_combo.blockSignals(False)

    def _on_export(self):
        """録音バッファをWAVに書き出す。"""
        if not self._engine.has_rec_data():
            QMessageBox.information(self, "Cannot Export",
                "録音データがありません。\nREC START → REC STOP を実行してから書き出してください。")
            return

        m = int(self._rec_duration_sec // 60)
        s = self._rec_duration_sec % 60
        dur_str = f"{m}:{s:04.1f}"
        default_name = f"rec_{dur_str.replace(':', '-')}.wav"
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Export Recording as WAV",
            os.path.join(os.path.expanduser("~"), "Desktop", default_name),
            "WAV Files (*.wav)"
        )
        if not output_path:
            return
        if not output_path.lower().endswith(".wav"):
            output_path += ".wav"

        if self._master_widget:
            self._master_widget._export_btn.setEnabled(False)
            self._master_widget._export_btn.setText("Exporting...")
            self._master_widget._rec_start_btn.setEnabled(False)
            self._master_widget._rec_stop_btn.setEnabled(False)
        self._set_status("Exporting recording...")

        self._export_worker = ExportWorker(self._engine, output_path)
        self._export_worker.finished.connect(self._on_export_finished)
        self._export_worker.start()

    @pyqtSlot(object)
    def _on_export_finished(self, result: ExportResult):
        if self._master_widget:
            # 書き出完了後: EXPORT有効(時間表示) / REC START有効 / REC STOP無効
            m = int(self._rec_duration_sec // 60)
            s = self._rec_duration_sec % 60
            dur_str = f"{m}:{s:04.1f}"
            self._master_widget._export_btn.setEnabled(True)
            self._master_widget._export_btn.setText(f"EXPORT WAV ({dur_str})")
            self._master_widget._rec_start_btn.setEnabled(True)
            self._master_widget._rec_stop_btn.setEnabled(False)

        if not result.success:
            QMessageBox.critical(self, "Export Failed", result.error_message)
            self._set_status("Export failed.")
            return

        if result.clipping_detected:
            if self._master_widget:
                self._master_widget.show_clip_warning(True, result.clipping_ratio)
            clip_msg = (
                f"Clipping was detected and automatically corrected.\n\n"
                f"  Clipped samples : {result.clipping_count:,}\n"
                f"  Clipping ratio  : {result.clipping_ratio * 100:.2f}%\n"
                f"  Peak level      : {result.peak_level:.3f} ({20 * math.log10(max(result.peak_level, 1e-9)):+.1f} dB)\n\n"
                f"To avoid clipping, lower the Master volume or individual track volumes."
            )
            QMessageBox.warning(self, "Clipping Detected", clip_msg)
        else:
            if self._master_widget:
                self._master_widget.show_clip_warning(False)

        dur_str = f"{int(result.duration_sec // 60)}:{result.duration_sec % 60:04.1f}"
        peak_db = 20 * math.log10(max(result.peak_level, 1e-9))
        msg = (
            f"Export complete!\n\n"
            f"  File     : {os.path.basename(result.output_path)}\n"
            f"  Duration : {dur_str}\n"
            f"  Peak     : {result.peak_level:.3f} ({peak_db:+.1f} dB)\n"
            f"  Clipping : {'YES (auto-corrected)' if result.clipping_detected else 'None'}\n\n"
            f"Saved to:\n{result.output_path}"
        )
        QMessageBox.information(self, "Export Complete", msg)
        self._set_status(f"Exported: {os.path.basename(result.output_path)}")

    # ------------------------------------------------------------------
    # Phase 3: プロジェクト保存 / 読み込み
    # ------------------------------------------------------------------

    def _on_save_project(self):
        if self._current_project_path is None:
            self._on_save_project_as()
        else:
            self._save_to_path(self._current_project_path)

    def _on_save_project_as(self):
        default_dir = os.path.join(os.path.expanduser("~"), "Documents", "Mixer4Track")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project",
            os.path.join(default_dir, "project.m4t"),
            "Mixer4Track Project (*.m4t);;JSON Files (*.json)"
        )
        if not path:
            return
        if not (path.lower().endswith(".m4t") or path.lower().endswith(".json")):
            path += ".m4t"
        self._save_to_path(path)

    def _save_to_path(self, path: str):
        store = ProjectStore(project_path=path)
        master_vol = self._engine.get_master_volume()
        limiter_enabled, limiter_ceiling_db, limiter_release_ms = self._engine.get_master_limiter_state()
        master_xfade = self._engine.get_master_xfade_state()
        ok = store.save(self._tracks, master_volume=master_vol, current_bank=self._current_bank,
                        markers=self._marker_manager.get_all(), master_limiter={
                            "enabled": limiter_enabled,
                            "ceiling_db": limiter_ceiling_db,
                            "release_ms": limiter_release_ms,
                        }, master_xfade=master_xfade)
        if ok:
            self._current_project_path = path
            self._update_project_label(path)
            self._set_status(f"Project saved: {os.path.basename(path)}")
        else:
            QMessageBox.critical(self, "Save Error", f"Failed to save project:\n{path}")

    def _on_open_project(self):
        default_dir = os.path.join(os.path.expanduser("~"), "Documents", "Mixer4Track")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project",
            default_dir,
            "Mixer4Track Project (*.m4t *.json);;All Files (*)"
        )
        if not path:
            return
        self._load_from_path(path)

    def _load_from_path(self, path: str):
        store = ProjectStore(project_path=path)
        result = store.load()
        if result is None:
            QMessageBox.critical(self, "Open Error", f"Failed to load project:\n{path}")
            return

        tracks, master_vol, saved_bank, marker_data = result

        self._engine.stop_all()

        # トラックを復元（最大16トラック）
        for i, track in enumerate(tracks):
            if i >= self.NUM_TRACKS:
                break
            self._tracks[i] = track
            self._track_widgets[i].restore_state(track)
            if track.file_path and os.path.isfile(track.file_path):
                self._engine.load_file(i, track.file_path)
            else:
                self._engine.unload_file(i)

        # マスター音量を復元
        self._engine.set_master_volume(master_vol)
        if self._master_widget:
            self._master_widget.restore_state(master_vol)

        # Phase 23: プロジェクトに保存されたマスター・リミッターを復元
        limiter_state = store.get_master_limiter_state()
        self._engine.set_master_limiter(
            limiter_state["enabled"], limiter_state["ceiling_db"], limiter_state["release_ms"]
        )
        if self._master_widget:
            self._master_widget.restore_limiter_state(
                limiter_state["enabled"], limiter_state["ceiling_db"]
            )

        # Phase 25: MASTER X-FADERを復元
        xfade_state = store.get_master_xfade_state()
        self._engine.set_master_xfade(
            xfade_state["position"], xfade_state["curve"],
            xfade_state["cut_a"], xfade_state["cut_b"],
        )
        if self._master_widget:
            self._master_widget.restore_xfade_state(xfade_state)

        # EQカーブを復元（全トラック）
        from eq_engine import EQParams
        for i, track in enumerate(tracks):
            if i >= self.NUM_TRACKS:
                break
            eq_params = EQParams(
                low_gain_db=track.eq_low_gain,
                mid_gain_db=track.eq_mid_gain,
                mid_freq_hz=track.eq_mid_freq,
                mid_q=track.eq_mid_q,
                high_gain_db=track.eq_high_gain,
            )
            self._track_widgets[i].update_eq_curve(eq_params)

        # Phase 6: エフェクト状態を復元
        # 全トラックが同じプリセットを保持する前提で、代表値としてtrack[0]を使用
        if tracks:
            rep_preset  = tracks[0].effect_preset
            rep_enabled = tracks[0].effect_enabled
            if self._master_widget:
                self._master_widget.restore_fx_state(rep_preset, rep_enabled)
            # AudioEngineにも適用
            for i, track in enumerate(tracks):
                if i >= self.NUM_TRACKS:
                    break
                self._engine.update_effect(track.track_id, track.effect_preset, track.effect_enabled)

        # バンクを復元
        self._current_bank = saved_bank
        self._bank_a_widget.setVisible(saved_bank == 0)
        self._bank_b_widget.setVisible(saved_bank == 1)
        self._update_bank_buttons()

        # Phase 20: マーカーを復元
        self._marker_manager.clear()
        self._marker_manager.load_from_list(marker_data)
        self._refresh_marker_ui()

        self._current_project_path = path
        self._update_project_label(path)
        self._set_status(f"Project loaded: {os.path.basename(path)}")

    def _update_project_label(self, path: str):
        name = os.path.splitext(os.path.basename(path))[0]
        self._project_label.setText(name)

    # ------------------------------------------------------------------
    # キーボード操作（現在表示中のバンクの Track 1〜8 に対応）
    # ------------------------------------------------------------------

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        # Phase 21: Ctrl+Z = UNDO, Ctrl+Y / Ctrl+Shift+Z = REDO
        if mods & Qt.ControlModifier:
            if key == Qt.Key_Z:
                if mods & Qt.ShiftModifier:
                    self._on_redo()
                else:
                    self._on_undo()
                return
            elif key == Qt.Key_Y:
                self._on_redo()
                return

        # 現在のバンクの先頭トラックインデックス
        base = self._current_bank * self.TRACKS_PER_BANK
        # track_widgets のインデックスはグローバル track_id と一致
        tw = self._track_widgets

        if key == Qt.Key_A:
            tw[base + 0].adjust_volume(+3)
        elif key == Qt.Key_Z:
            tw[base + 0].adjust_volume(-3)
        elif key == Qt.Key_S:
            tw[base + 1].adjust_volume(+3)
        elif key == Qt.Key_X:
            tw[base + 1].adjust_volume(-3)
        elif key == Qt.Key_D:
            tw[base + 2].adjust_volume(+3)
        elif key == Qt.Key_C:
            tw[base + 2].adjust_volume(-3)
        elif key == Qt.Key_F:
            tw[base + 3].adjust_volume(+3)
        elif key == Qt.Key_V:
            tw[base + 3].adjust_volume(-3)
        elif key == Qt.Key_G:
            tw[base + 4].adjust_volume(+3)
        elif key == Qt.Key_B:
            tw[base + 4].adjust_volume(-3)
        elif key == Qt.Key_H:
            tw[base + 5].adjust_volume(+3)
        elif key == Qt.Key_N:
            tw[base + 5].adjust_volume(-3)
        elif key == Qt.Key_J:
            tw[base + 6].adjust_volume(+3)
        elif key == Qt.Key_M:
            tw[base + 6].adjust_volume(-3)
        elif key == Qt.Key_K:
            tw[base + 7].adjust_volume(+3)
        elif key == Qt.Key_Comma:
            tw[base + 7].adjust_volume(-3)
        elif key == Qt.Key_Space:
            if self._engine.is_playing():
                self._on_stop()
            else:
                self._on_play()
        # バンク切り替えショートカット（Tab キー）
        elif key == Qt.Key_Tab:
            self._switch_bank(1 - self._current_bank)
        else:
            super().keyPressEvent(event)

    def _set_status(self, msg: str):
        self._status_label.setText(msg)

    # ------------------------------------------------------------------
    # 終了処理
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        # タイマー停止
        self._timer.stop()
        # エクスポートワーカー待機
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.wait(3000)
        # オーディオエンジンクリーンアップ
        self._engine.cleanup()
        # 全サブウィンドウを閉じる（拡大表示ウィンドウ・トラックサブウィンドウなど）
        from PyQt5.QtWidgets import QApplication as _QApp
        for w in _QApp.topLevelWidgets():
            if w is not self:
                try:
                    w.close()
                except Exception:
                    pass
        event.accept()
        # アプリケーションを完全終了
        _QApp.quit()
