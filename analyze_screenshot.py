"""
スクリーンショット画像からフェーダーハンドルのY位置を測定するスクリプト。
フェーダーハンドルは白い矩形なので、それを検出する。
"""
from PIL import Image
import numpy as np

img = Image.open("/home/ubuntu/mixer_screenshot.png")
arr = np.array(img)

print(f"画像サイズ: {img.width} x {img.height}")

# トラック1のフェーダー領域（X: 約30-60, Y: 600-800）
# MASTERのフェーダー領域（X: 約1030-1060, Y: 600-900）

# 白いピクセル（フェーダーハンドル）を検出
# フェーダーハンドルは明るいグレー/白色 (R>180, G>180, B>180)

def find_fader_handle_y(x_start, x_end, y_start, y_end):
    """指定範囲内でフェーダーハンドル（白い矩形）のY位置を検出"""
    region = arr[y_start:y_end, x_start:x_end]
    # 明るいピクセルを検出（R>180, G>180, B>180）
    bright = (region[:,:,0] > 180) & (region[:,:,1] > 180) & (region[:,:,2] > 180)
    rows_with_bright = np.where(bright.any(axis=1))[0]
    if len(rows_with_bright) > 0:
        # 連続した明るい行のグループを見つける
        groups = []
        current_group = [rows_with_bright[0]]
        for i in range(1, len(rows_with_bright)):
            if rows_with_bright[i] - rows_with_bright[i-1] <= 3:
                current_group.append(rows_with_bright[i])
            else:
                if len(current_group) >= 5:  # 5px以上の高さのグループ
                    groups.append(current_group)
                current_group = [rows_with_bright[i]]
        if len(current_group) >= 5:
            groups.append(current_group)
        
        if groups:
            # 最初のグループ（フェーダーハンドル）の中央Y
            first_group = groups[0]
            center_y = y_start + (first_group[0] + first_group[-1]) // 2
            return center_y, y_start + first_group[0], y_start + first_group[-1]
    return None, None, None

# トラック1のフェーダー（X: 30-80）
# ミキサーのヘッダーバー高さを考慮してY範囲を設定
print("\n=== フェーダーハンドル検出 ===")

# トラック1のフェーダー（最初のトラック）
# トラックウィジェットのX範囲を推定（ヘッダー幅 + トラック幅）
# バンクバー: ~90px, トラック幅: ~120px
track1_x_start = 15
track1_x_end = 130
y_center, y_top, y_bottom = find_fader_handle_y(track1_x_start, track1_x_end, 600, 850)
print(f"Track1 フェーダーハンドル: center_y={y_center}, top={y_top}, bottom={y_bottom}")

# 各トラックのX範囲を確認
for i, x_start in enumerate(range(15, 1000, 120)):
    y_center, y_top, y_bottom = find_fader_handle_y(x_start, x_start+110, 580, 850)
    if y_center:
        print(f"Track{i+1} (x={x_start}-{x_start+110}): fader_y={y_center}")

# MASTERトラック（右端）
print("\n=== MASTERトラック フェーダー検出 ===")
for x_start in range(980, 1440, 20):
    y_center, y_top, y_bottom = find_fader_handle_y(x_start, x_start+80, 580, 900)
    if y_center:
        print(f"MASTER (x={x_start}-{x_start+80}): fader_y={y_center}")
        break

# 画像の特定行のピクセルを確認
print("\n=== 特定行のピクセル確認（Y=640-700, X=30-80）===")
for y in range(630, 720, 5):
    row = arr[y, 30:80]
    bright_count = ((row[:,0] > 150) & (row[:,1] > 150) & (row[:,2] > 150)).sum()
    if bright_count > 5:
        print(f"  Y={y}: bright_pixels={bright_count}")
