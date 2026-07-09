"""
MASTERフェーダーハンドルのY位置を正確に特定するスクリプト。
"""
from PIL import Image, ImageDraw
import numpy as np

img = Image.open("/home/ubuntu/mixer_screenshot.png")
arr = np.array(img)

# MASTERフェーダーのX位置は1090-1100付近（明るいピクセル検出）
# ハンドルY: 764~785（中央=774）
master_handle_y = (764 + 785) // 2  # = 774

# Track1のフェーダーハンドルY: 719（前回の検出結果）
track1_handle_y = 719

print(f"Track1 フェーダーハンドル中央Y: {track1_handle_y}")
print(f"MASTER フェーダーハンドル中央Y: {master_handle_y}")
print(f"差分: {track1_handle_y - master_handle_y}px (正=MASTERが上, 負=MASTERが下)")

# アノテーション付き画像を生成
img_draw = img.copy()
draw = ImageDraw.Draw(img_draw)

# Track1ハンドル（赤）
draw.line([(0, track1_handle_y), (img.width, track1_handle_y)], fill=(255, 50, 50), width=3)

# MASTERハンドル（黄）
draw.line([(0, master_handle_y), (img.width, master_handle_y)], fill=(255, 220, 0), width=3)

# ラベル
draw.text((5, track1_handle_y - 20), f"Track Fader Handle: Y={track1_handle_y}", fill=(255, 50, 50))
draw.text((5, master_handle_y + 5), f"MASTER Fader Handle: Y={master_handle_y}", fill=(255, 220, 0))
draw.text((5, (track1_handle_y + master_handle_y)//2), f"Diff: {track1_handle_y - master_handle_y}px", fill=(255, 255, 255))

img_draw.save("/home/ubuntu/mixer_annotated3.png", "PNG")
print(f"保存: /home/ubuntu/mixer_annotated3.png")

# 追加: 2番目の明るいピクセルグループ（X=1220-1230, Y=798-819）も確認
# これはMASTERのメーターかもしれない
print(f"\n2番目の明るいグループ: X=1220-1230, Y=798-819 (中央Y={int((798+819)/2)})")
