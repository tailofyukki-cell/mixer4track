"""
スクリーンショットにフェーダー上端・中央・下端の水平線を引いて視覚化するスクリプト。
"""
from PIL import Image, ImageDraw, ImageFont
import numpy as np

img = Image.open("/home/ubuntu/mixer_screenshot.png")
arr = np.array(img)
draw = ImageDraw.Draw(img)

def find_fader_range(x_start, x_end, y_start, y_end):
    """指定範囲内でフェーダーグルーブ（暗い縦線）の上端・下端を検出"""
    region = arr[y_start:y_end, x_start:x_end]
    # フェーダーグルーブは非常に暗い（R<30, G<30, B<30）
    dark = (region[:,:,0] < 40) & (region[:,:,1] < 40) & (region[:,:,2] < 40)
    rows_with_dark = np.where(dark.any(axis=1))[0]
    if len(rows_with_dark) > 10:
        return y_start + rows_with_dark[0], y_start + rows_with_dark[-1]
    return None, None

def find_handle_y(x_start, x_end, y_start, y_end):
    """フェーダーハンドル（白い矩形）の中央Y位置を検出"""
    region = arr[y_start:y_end, x_start:x_end]
    bright = (region[:,:,0] > 180) & (region[:,:,1] > 180) & (region[:,:,2] > 180)
    rows_with_bright = np.where(bright.any(axis=1))[0]
    if len(rows_with_bright) > 0:
        groups = []
        current_group = [rows_with_bright[0]]
        for i in range(1, len(rows_with_bright)):
            if rows_with_bright[i] - rows_with_bright[i-1] <= 3:
                current_group.append(rows_with_bright[i])
            else:
                if len(current_group) >= 5:
                    groups.append(current_group)
                current_group = [rows_with_bright[i]]
        if len(current_group) >= 5:
            groups.append(current_group)
        if groups:
            first_group = groups[0]
            return y_start + (first_group[0] + first_group[-1]) // 2
    return None

# Track1のフェーダー範囲
track1_fader_top, track1_fader_bottom = find_fader_range(30, 80, 600, 870)
master_fader_top, master_fader_bottom = find_fader_range(1030, 1080, 600, 900)

print(f"Track1 フェーダーグルーブ: top={track1_fader_top}, bottom={track1_fader_bottom}")
print(f"MASTER フェーダーグルーブ: top={master_fader_top}, bottom={master_fader_bottom}")

# ハンドル位置
track1_handle = find_handle_y(30, 80, 600, 870)
master_handle = find_handle_y(1030, 1080, 600, 900)
print(f"Track1 ハンドルY: {track1_handle}")
print(f"MASTER ハンドルY: {master_handle}")

# アノテーション
# フェーダー上端（緑）
if track1_fader_top:
    draw.line([(0, track1_fader_top), (img.width, track1_fader_top)], fill=(0, 255, 0), width=2)
    draw.text((5, track1_fader_top - 15), f"Fader Top: Y={track1_fader_top}", fill=(0, 255, 0))

# フェーダー下端（青）
if track1_fader_bottom:
    draw.line([(0, track1_fader_bottom), (img.width, track1_fader_bottom)], fill=(0, 100, 255), width=2)
    draw.text((5, track1_fader_bottom + 3), f"Fader Bottom: Y={track1_fader_bottom}", fill=(0, 100, 255))

# ハンドル位置（赤）
if track1_handle:
    draw.line([(0, track1_handle), (img.width, track1_handle)], fill=(255, 50, 50), width=2)
    draw.text((5, track1_handle - 15), f"Track Handle: Y={track1_handle}", fill=(255, 50, 50))

if master_handle and master_handle != track1_handle:
    draw.line([(0, master_handle), (img.width, master_handle)], fill=(255, 150, 0), width=2)
    draw.text((5, master_handle + 3), f"Master Handle: Y={master_handle}", fill=(255, 150, 0))

img.save("/home/ubuntu/mixer_annotated2.png", "PNG")
print(f"保存: /home/ubuntu/mixer_annotated2.png")
