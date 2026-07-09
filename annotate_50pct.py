"""
50%位置画像でフェーダー位置を検索・アノテーションするスクリプト。
"""
from PIL import Image, ImageDraw
import numpy as np

img = Image.open("/home/ubuntu/mixer_screenshot_50pct.png")
arr = np.array(img)
draw = ImageDraw.Draw(img)

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

print("=== 50%位置 フェーダーハンドル検出結果 ===")
fader_ys = []
for g in groups_x:
    x_vals = [r[0] for r in g]
    y_vals = [r[1] for r in g]
    avg_y = int(sum(y_vals)/len(y_vals))
    fader_ys.append((min(x_vals), max(x_vals), avg_y))
    print(f"  X={min(x_vals)}-{max(x_vals)}, Handle_Y={avg_y}")

if len(fader_ys) >= 2:
    track_y = fader_ys[0][2]
    master_y = fader_ys[-1][2]
    print(f"\nTrack1 Y={track_y}, MASTER Y={master_y}, 差分={track_y-master_y}px")
    
    # アノテーション
    draw.line([(0, track_y), (img.width, track_y)], fill=(255, 50, 50), width=3)
    draw.line([(0, master_y), (img.width, master_y)], fill=(255, 220, 0), width=3)
    draw.text((5, track_y - 18), f"Track Handle (50%): Y={track_y}", fill=(255, 50, 50))
    draw.text((5, master_y + 5), f"MASTER Handle (50%): Y={master_y}", fill=(255, 220, 0))
    diff = track_y - master_y
    if abs(diff) <= 3:
        draw.text((5, track_y + 20), f"ALIGNED! (diff={diff}px)", fill=(0, 255, 0))
    else:
        draw.text((5, (track_y+master_y)//2), f"Diff: {diff}px", fill=(255,255,255))

img.save("/home/ubuntu/mixer_annotated_50pct.png", "PNG")
print("保存: /home/ubuntu/mixer_annotated_50pct.png")
