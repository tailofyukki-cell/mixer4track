"""
全フェーダーのX・Y位置を検索するスクリプト。
"""
from PIL import Image
import numpy as np

img = Image.open("/home/ubuntu/mixer_screenshot.png")
arr = np.array(img)

print(f"画像サイズ: {img.width} x {img.height}")
print(f"ファイル更新時刻: {__import__('os').path.getmtime('/home/ubuntu/mixer_screenshot.png')}")

# Y=580-900の範囲で明るいピクセル（フェーダーハンドル）を全X範囲で検索
print("\n=== 全X範囲でフェーダーハンドル検索（Y=580-900）===")
results = []
for x in range(10, img.width-10, 5):
    col = arr[580:900, x, :]
    bright = (col[:,0] > 180) & (col[:,1] > 180) & (col[:,2] > 180)
    bright_rows = np.where(bright)[0]
    if len(bright_rows) >= 5:
        # 連続グループを検出
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
            center_y = 580 + (g[0] + g[-1]) // 2
            results.append((x, center_y, 580+g[0], 580+g[-1]))

# X位置でグループ化
if results:
    # 近いX位置をまとめる
    groups_x = []
    current_x = [results[0]]
    for i in range(1, len(results)):
        if results[i][0] - results[i-1][0] <= 20:
            current_x.append(results[i])
        else:
            groups_x.append(current_x)
            current_x = [results[i]]
    groups_x.append(current_x)
    
    for g in groups_x:
        x_vals = [r[0] for r in g]
        y_vals = [r[1] for r in g]
        print(f"  X={min(x_vals)}-{max(x_vals)}, Handle_Y={int(sum(y_vals)/len(y_vals))} (top={g[0][2]}, bottom={g[0][3]})")
