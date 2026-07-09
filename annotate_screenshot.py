"""
スクリーンショットにフェーダーY位置の水平線を引いて視覚化するスクリプト。
"""
from PIL import Image, ImageDraw

img = Image.open("/home/ubuntu/mixer_screenshot.png")
draw = ImageDraw.Draw(img)

# フェーダーハンドルのY位置（解析結果より）
fader_y = 719

# 赤い水平線を引く（フェーダーハンドル上端）
draw.line([(0, fader_y), (img.width, fader_y)], fill=(255, 0, 0), width=2)

# 保存
img.save("/home/ubuntu/mixer_annotated.png", "PNG")
print(f"アノテーション済み画像保存: /home/ubuntu/mixer_annotated.png")
print(f"赤線Y位置: {fader_y}px（全トラック＋MASTERのフェーダーハンドル位置）")
