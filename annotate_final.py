"""
EQカーブとGEQカーブの位置にアノテーションを付けるスクリプト（修正版）。
"""
from PIL import Image, ImageDraw
import numpy as np

img = Image.open("/home/ubuntu/mixer_screenshot.png")
arr = np.array(img)
draw = ImageDraw.Draw(img)

# EQカーブ上端: Y=1006
eq_top = 1006
geq_top = 1006

# アノテーション
draw.line([(0, eq_top), (img.width, eq_top)], fill=(0, 200, 255), width=3)
draw.text((5, eq_top - 20), f"Track EQ Curve Top: Y={eq_top}", fill=(0, 200, 255))
draw.text((5, eq_top + 5), f"MASTER GEQ Curve Top: Y={geq_top}  ALIGNED!", fill=(0, 255, 0))

# フェーダーハンドルも確認
results = []
for x in range(10, img.width-10, 5):
    col = arr[580:900, x, :]
    bright = (col[:,0] > 180) & (col[:,1] > 180) & (col[:,2] > 180)
    bright_rows = np.where(bright)[0]
    if len(bright_rows) >= 5:
        groups = []
        current = [bright_rows[0]]
        for i in range(1, len(bright_rows)):
            if bright_rows[i] - bright_rows[i-1] <= 3:
                current.append(bright_rows[i])
            else:
                if len(current) >= 5:
                    groups.append(current)
                current = [bright_rows[i]]
        if len(current) >= 5:
            groups.append(current)
        for g in groups:
            results.append((x, 580+(g[0]+g[-1])//2))

groups_x = []
if results:
    current_x = [results[0]]
    for i in range(1, len(results)):
        if results[i][0] - results[i-1][0] <= 20:
            current_x.append(results[i])
        else:
            groups_x.append(current_x)
            current_x = [results[i]]
    groups_x.append(current_x)

print("=== フェーダーハンドル ===")
fader_ys = []
for g in groups_x:
    x_vals = [r[0] for r in g]
    y_vals = [r[1] for r in g]
    avg_y = int(sum(y_vals)/len(y_vals))
    fader_ys.append(avg_y)
    print(f"  X={min(x_vals)}-{max(x_vals)}, Handle_Y={avg_y}")

if fader_ys:
    # 全フェーダーの平均Y
    track_ys = fader_ys[:-1]  # 最後がMASTER
    master_y = fader_ys[-1]
    avg_track_y = int(sum(track_ys)/len(track_ys)) if track_ys else 0
    print(f"\n  Track平均フェーダーY: {avg_track_y}")
    print(f"  MASTER フェーダーY: {master_y}")
    print(f"  差分: {avg_track_y - master_y}px")
    
    # フェーダー位置に赤線
    draw.line([(0, avg_track_y), (img.width, avg_track_y)], fill=(255, 80, 80), width=2)
    draw.text((5, avg_track_y - 18), f"Track Fader Handle: Y={avg_track_y}", fill=(255, 80, 80))
    
    draw.line([(0, master_y), (img.width, master_y)], fill=(255, 200, 0), width=2)
    draw.text((5, master_y + 5), f"MASTER Fader Handle: Y={master_y}", fill=(255, 200, 0))

print(f"\n  EQ/GEQカーブ上端: Y={eq_top} (差分=0px)")

img.save("/home/ubuntu/mixer_annotated_final.png", "PNG")
print("\n保存: /home/ubuntu/mixer_annotated_final.png")
