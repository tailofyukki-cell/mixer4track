"""
EQカーブとGEQカーブの位置を広い範囲で検索するスクリプト。
"""
from PIL import Image, ImageDraw
import numpy as np

img = Image.open("/home/ubuntu/mixer_screenshot.png")
arr = np.array(img)
draw = ImageDraw.Draw(img)

print(f"画像サイズ: {img.width} x {img.height}")

# Y=850-1100の範囲でTrack1(X=15-130)の暗い背景を検索
print("\n=== Track1 Y=850-1100 各行の暗いピクセル数 ===")
for y in range(850, 1100, 2):
    row = arr[y, 15:130]
    dark_count = ((row[:,0] < 25) & (row[:,1] < 25) & (row[:,2] < 25)).sum()
    if dark_count > 60:
        print(f"  Y={y}: dark_count={dark_count}")

# MASTER(X=985-1150)
print("\n=== MASTER Y=850-1100 各行の暗いピクセル数 ===")
for y in range(850, 1100, 2):
    row = arr[y, 985:1150]
    dark_count = ((row[:,0] < 25) & (row[:,1] < 25) & (row[:,2] < 25)).sum()
    if dark_count > 60:
        print(f"  Y={y}: dark_count={dark_count}")
