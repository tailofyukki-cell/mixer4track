"""
MASTERフェーダーのX位置を特定するスクリプト。
"""
from PIL import Image
import numpy as np

img = Image.open("/home/ubuntu/mixer_screenshot.png")
arr = np.array(img)

print(f"画像サイズ: {img.width} x {img.height}")

# Y=650-850の範囲で、X=980以降の明るいピクセルを探す
print("\n=== Y=630-850, X=980-1440 の明るいピクセル ===")
for x in range(980, 1440, 10):
    col = arr[630:850, x, :]
    bright = (col[:,0] > 180) & (col[:,1] > 180) & (col[:,2] > 180)
    bright_rows = np.where(bright)[0]
    if len(bright_rows) > 3:
        print(f"  X={x}: bright_rows={630+bright_rows[0]}~{630+bright_rows[-1]} (count={len(bright_rows)})")

# Y=630-850の範囲で、X=980以降の暗いピクセル（フェーダーグルーブ）を探す
print("\n=== Y=630-850, X=980-1440 の暗いピクセル（グルーブ）===")
for x in range(980, 1440, 5):
    col = arr[630:850, x, :]
    dark = (col[:,0] < 20) & (col[:,1] < 20) & (col[:,2] < 20)
    dark_rows = np.where(dark)[0]
    if len(dark_rows) > 50:
        print(f"  X={x}: dark_rows={630+dark_rows[0]}~{630+dark_rows[-1]} (count={len(dark_rows)})")
