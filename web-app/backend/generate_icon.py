from PIL import Image, ImageDraw, ImageFont
import math, os

SIZE = 180
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# ============================================================
# Modern Fund Icon — gradient dark blue bg + golden candlestick chart
# ============================================================

# --- 1. Rounded rectangle base with gradient ---
r = 36  # corner radius: iOS style
cx = SIZE // 2

# Simulate gradient: dark navy (#0a1f3f) to slightly lighter (#1a3a6b)
# We'll draw horizontal bands
for y in range(SIZE):
    t = y / SIZE
    r_val = int(10 + t * 20)
    g_val = int(31 + t * 35)
    b_val = int(63 + t * 55)
    color = (r_val, g_val, b_val, 255)
    # Clip to rounded rect shape manually
    # Simple approach: use a mask later—but we can just draw full rect and mask
    draw.line([(0, y), (SIZE - 1, y)], fill=color)

# --- 2. Create rounded corner mask ---
mask = Image.new("L", (SIZE, SIZE), 0)
mask_draw = ImageDraw.Draw(mask)
mask_draw.rounded_rectangle([0, 0, SIZE, SIZE], radius=r, fill=255)

# Apply mask
img.putalpha(mask)

# Re-draw on a new image to get clean result
bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
bg_draw = ImageDraw.Draw(bg)
for y in range(SIZE):
    t = y / SIZE
    r_val = int(10 + t * 20)
    g_val = int(31 + t * 35)
    b_val = int(63 + t * 55)
    bg_draw.line([(0, y), (SIZE - 1, y)], fill=(r_val, g_val, b_val, 255))
bg.putalpha(mask)

draw = ImageDraw.Draw(bg)

# --- 3. Golden accent ring (subtle) ---
draw.rounded_rectangle([4, 4, SIZE - 4, SIZE - 4], radius=r-2,
                        outline=(0xFF, 0xD7, 0x00, 60), width=2)

# --- 4. Candlestick chart bars (white / gold mixed) ---
# Three bars: small loss, big gain, medium gain — classic fund chart
bar_data = [
    # (center_x, low_y, high_y, open_y, close_y, is_gain)
    # Bar 1: small red (loss)
    (50,  95, 55, 90, 65, False),
    # Bar 2: big green (gain)
    (90,  100, 30, 80, 40, True),
    # Bar 3: medium green (gain)
    (130, 90, 50, 75, 60, True),
]

for bx, low, high, open_y, close_y, is_gain in bar_data:
    # Wick (thin line from low to high)
    draw.line([(bx, low), (bx, high)], fill=(255, 255, 255, 200), width=2)
    # Body
    body_top = min(open_y, close_y)
    body_bot = max(open_y, close_y)
    if body_bot - body_top < 2:
        body_bot = body_top + 3  # ensure visible
    body_color = (0xFF, 0xD7, 0x00, 240) if is_gain else (0xFF, 0x6B, 0x6B, 240)
    draw.rectangle([bx - 8, body_top, bx + 8, body_bot], fill=body_color)

# --- 5. Upward trend line (white dashed curve) ---
# Simple polyline showing upward trend below bars
trend_pts = [(35, 115), (60, 108), (90, 98), (120, 88), (150, 75)]
for i in range(len(trend_pts) - 1):
    draw.line([trend_pts[i], trend_pts[i+1]], fill=(255, 255, 255, 160), width=3)

# Arrow at the end
tip = trend_pts[-1]
draw.polygon([
    (tip[0], tip[1] - 10),
    (tip[0] - 9, tip[1] + 3),
    (tip[0] + 9, tip[1] + 3),
], fill=(0xFF, 0xD7, 0x00, 240))

# --- 6. "$" symbol small at top-left ---
# Use simple rectangle-based $ for reliability (no font needed)
def draw_dollar(draw, x, y, size, color):
    s = size
    # S shape with two lines
    # Vertical bar
    draw.rectangle([x + s//2 - 1, y, x + s//2 + 1, y + s], fill=color)
    # Top curve
    draw.arc([x, y - s//3, x + s + 2, y + s//2], 180, 360, fill=color, width=2)
    # Bottom curve
    draw.arc([x, y + s//2, x + s + 2, y + s + s//3], 0, 180, fill=color, width=2)

draw_dollar(draw, 12, 12, 20, (0xFF, 0xD7, 0x00, 180))

out = os.path.join(os.path.dirname(__file__), "icon-180.png")
bg.save(out, "PNG")
print(f"Saved: {out}")
print(f"Size: {bg.size}")
