"""
EQカーブとGEQカーブの位置にアノテーションを付けるスクリプト。
"""
from PIL import Image, ImageDraw
import numpy as np

img = Image.open("/home/ubuntu/mixer_screenshot.png")
arr = np.array(img)
draw = ImageDraw.Draw(img)

# EQカーブとGEQカーブは暗い背景に青/黄色のライン
# Y=850-950の範囲で確認

# Track1のEQカーブ領域（X=15-130）の暗い背景を検出
# EQカーブは #0a0a0a 背景に青いライン
print("=== EQカーブ/GEQカーブ検出 ===")

# Y=850-960の範囲で、各トラックのX位置で暗い背景（#0a0a0a付近）を検出
def find_dark_bg_range(x_start, x_end, y_start, y_end):
    """暗い背景（EQ/GEQカーブウィジェット）の上端・下端を検出"""
    region = arr[y_start:y_end, x_start:x_end]
    # 非常に暗い背景 (R<20, G<20, B<20)
    dark = (region[:,:,0] < 20) & (region[:,:,1] < 20) & (region[:,:,2] < 20)
    # 各行の暗いピクセル数を計算
    dark_per_row = dark.sum(axis=1)
    # 幅の50%以上が暗い行を検出
    width = x_end - x_start
    dark_rows = np.where(dark_per_row > width * 0.5)[0]
    if len(dark_rows) > 5:
        return y_start + dark_rows[0], y_start + dark_rows[-1]
    return None, None

# Track1
top, bottom = find_dark_bg_range(15, 130, 850, 980)
print(f"Track1 EQカーブ: top={top}, bottom={bottom}")

# MASTER
master_top, master_bottom = find_dark_bg_range(985, 1150, 850, 980)
print(f"MASTER GEQカーブ: top={master_top}, bottom={master_bottom}")

# アノテーション
if top:
    draw.line([(0, top), (img.width, top)], fill=(0, 200, 255), width=2)
    draw.text((5, top - 18), f"Track EQ Curve: Y={top}", fill=(0, 200, 255))
if bottom:
    draw.line([(0, bottom), (img.width, bottom)], fill=(0, 200, 255), width=1)

if master_top:
    draw.line([(0, master_top), (img.width, master_top)], fill=(255, 200, 0), width=2)
    draw.text((5, master_top + 5), f"MASTER GEQ Curve: Y={master_top}", fill=(255, 200, 0))
if master_bottom:
    draw.line([(0, master_bottom), (img.width, master_bottom)], fill=(255, 200, 0), width=1)

if top and master_top:
    diff = top - master_top
    print(f"\n差分: {diff}px")
    if abs(diff) <= 5:
        draw.text((5, (top+master_top)//2 + 20), f"ALIGNED! (diff={diff}px)", fill=(0, 255, 0))
    else:
        draw.text((5, (top+master_top)//2), f"Diff: {diff}px", fill=(255, 255, 255))

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

print("\n=== フェーダーハンドル ===")
for g in groups_x:
    x_vals = [r[0] for r in g]
    y_vals = [r[1] for r in g]
    avg_y = int(sum(y_vals)/len(y_vals))
    print(f"  X={min(x_vals)}-{max(x_vals)}, Handle_Y={avg_y}")

img.save("/home/ubuntu/mixer_annotated_curves.png", "PNG")
print("\n保存: /home/ubuntu/mixer_annotated_curves.png")
