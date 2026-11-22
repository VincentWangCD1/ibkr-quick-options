"""Local PNG export of a single closing order. No account identifiers."""
from functools import lru_cache
from pathlib import Path
import os

from PIL import Image, ImageDraw, ImageFont, ImageOps
from quickoptions_version import APP_VERSION

BRAND = "VincentWangCD"
AVATAR = Path(__file__).with_name("assets") / "saint-tail.jpg"


@lru_cache(maxsize=64)
def font(size, bold=False):
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in (("msyhbd.ttc" if bold else "msyh.ttc"), "simhei.ttf", "arial.ttf"):
        if (fonts / name).exists():
            return ImageFont.truetype(str(fonts / name), size)
    return ImageFont.load_default(size=size)


def brand_avatar(size):
    with Image.open(AVATAR) as source:
        face = ImageOps.fit(source.convert("RGBA"), (size, size), centering=(.46, .40))
    mask = Image.new("L", face.size)
    ImageDraw.Draw(mask).ellipse((0, 0, size-1, size-1), fill=255)
    face.putalpha(mask)
    return face


def visibility_icon(hidden):
    icon = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(icon)
    ink = "#334c5d"
    draw.line([(3, 32), (13, 21), (23, 15), (32, 13), (41, 15), (51, 21), (61, 32),
               (51, 43), (41, 49), (32, 51), (23, 49), (13, 43), (3, 32)], fill=ink, width=4)
    draw.ellipse((24, 24, 40, 40), fill=ink)
    if hidden:
        draw.line((8, 5, 56, 59), fill=ink, width=6)
    return icon.resize((19, 19), Image.Resampling.LANCZOS)


def render_share_card(order, result, demo=False, private=False, language="zh", green_profit=False):
    from history_cards import amount, average_quote, contract_description

    def label(chinese, english):
        return english if language == "en" else chinese

    image = Image.new("RGB", (1080, 1920), "#f8f6f1")
    draw = ImageDraw.Draw(image)
    ink, muted, line = "#172d37", "#516670", "#cbd3ce"
    hero = "#18333d"
    profit = result["pnl"]
    from contract_display import pnl_color
    highlight = pnl_color(profit, green_profit, red="#ffafa1", green="#93e4c1", neutral="#f4f1e5")

    def text(x, y, value, size, color=ink, bold=False, width=920, anchor="lt"):
        value = str(value)
        while size > 12 and draw.textlength(value, font=font(size, bold)) > width:
            size -= 2
        draw.text((x, y), value, font=font(size, bold), fill=color, anchor=anchor)

    text(76, 81, f"Quick Options V{APP_VERSION}", 47, bold=True, width=650)
    text(1004, 93, label("离线演示" if demo else "交易复盘", "DEMO" if demo else "TRADE RECAP"),
         30, muted, anchor="rt")
    draw.line((76, 178, 1004, 178), fill=line, width=2)
    text(76, 233, order.get("symbol") or order.get("local_symbol") or "—", 90, bold=True)
    text(78, 362, contract_description(order, expiry_label=label("到期", "Expiry")), 46)
    qty = result["quantity"]
    quantity = label(f"{qty:g} 张", f"{qty:g} contract{'s' if qty != 1 else ''}")
    text(78, 444, label("平仓", "Position closed"), 32, muted)

    draw.rounded_rectangle((64, 558, 1016, 983), radius=48, fill=hero)
    caption = label("收益率", "Return")
    if result["before_fees"]:
        caption += label("（费前）", " (before fees)")
    text(114, 624, caption, 38, "#d8e5e2")
    pct = result["percent"]
    percent = f"{pct:+,.2f}%" if pct is not None else "—"
    text(108, 727, percent, 105, highlight, bold=True, width=850)
    text(115, 883, label("本次盈亏", "Trade P&L"), 34, "#f9faf6", width=300)
    detail = label("金额已隐藏", "Amount hidden") if private else amount(profit, order["currency"], signed=True)
    text(964, 883, detail, 34, "#f9faf6", width=590, anchor="rt")

    zone = result["timezone"]
    zone_label = {"America/New_York": label("纽约时间", "New York time"),
                  "Asia/Shanghai": label("北京时间", "Beijing time")}.get(zone, zone)
    text(78, 1054, zone_label, 30, muted)
    buy = label("买入", "Buy")
    sell = label("卖出", "Sell")
    for index, prefix in enumerate(("open", "close")):
        y = 1160 + index * 280
        buy_leg = (result["action"] == "SELL") == (prefix == "open")
        leg = buy if buy_leg else sell
        if not private and (prefix == "close" or result.get("open_first")):
            leg += "  " + quantity
        text(78, y, leg, 51, bold=True, width=530)
        if not private:
            price = average_quote(result.get(prefix + "_price"), order["currency"])
            text(1002, y, price, 57, bold=True, width=370, anchor="rt")
        first, last = result.get(prefix + "_first"), result.get(prefix + "_last")
        text(80, y + 88, first or label("缺少开仓记录", "Opening fill unavailable"), 37, muted)
        if first and first != last:
            text(80, y + 150, label("至  ", "to  ") + last, 37, muted)
        draw.line((78, y + 225, 1002, y + 225), fill=line, width=2)

    face = brand_avatar(96)
    image.paste(face, (78, 1744), face)
    text(204, 1750, label("作者", "Author"), 27, muted)
    text(202, 1792, BRAND, 37, bold=True)
    return image
