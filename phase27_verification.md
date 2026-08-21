# Phase 27 検証記録

## 実行日

2026-08-21

## 自動検証

`python3 -m py_compile *.py`、`test_headless.py`、`test_loop_ui_smoke.py`を、PyQt5 offscreen・SDL dummy環境で実行し、すべて合格した。

## 視覚確認

1280×1000pxのオフスクリーン実画面をキャプチャして確認した。トランスポートバーには`AUTO REC`、`AUTO PLAY`、`AUTO CLR`が表示され、既存の再生・ループ・保存・マーカー操作と同じ一段内で重複やクリッピングなく配置されている。

## 実機受入時の重点確認

音声を読み込んだWindows環境で、AUTO RECをONにして再生しながらフェーダー、PAN、MASTER X-FADERを動かす。STOP後にAUTO PLAYをONにして再生し、操作時刻・補間・音切れの有無をヘッドフォンで確認する。
