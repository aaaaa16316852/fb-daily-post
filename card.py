"""
card.py — 文章重點配圖（程式畫圖，不用 AI 生圖 → 中文字保證零錯字、不會有 AI 感）

輸出 1080×1350（FB 動態最佔版面的 4:5 直式），內容：
  頂部標籤＋日期 → 大標題（關鍵字上色）→ 副標 → 3～5 個編號重點卡 → 底部「建議收藏」＋署名

字型：Streamlit Cloud 靠 packages.txt 安裝 fonts-noto-cjk；本機找不到字型會丟出明確錯誤。
"""
from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1350
PAD = 72

FONT_DIRS = [
    "/usr/share/fonts/opentype/noto",           # Debian / Streamlit Cloud（fonts-noto-cjk）
    "/usr/share/fonts/noto-cjk",
    "/usr/share/fonts/truetype/noto",
    "/System/Library/Fonts",                    # macOS
    "C:/Windows/Fonts",                         # Windows
    os.path.join(os.path.dirname(__file__), "fonts"),  # 也可自己放字型到 repo 的 fonts/
]
FONT_FILES = {
    "black": ["NotoSansCJK-Black.ttc", "NotoSansTC-Black.otf", "msjhbd.ttc", "PingFang.ttc"],
    "bold": ["NotoSansCJK-Bold.ttc", "NotoSansTC-Bold.otf", "msjhbd.ttc", "PingFang.ttc"],
    "medium": ["NotoSansCJK-Medium.ttc", "NotoSansCJK-Regular.ttc", "NotoSansTC-Medium.otf",
               "msjh.ttc", "PingFang.ttc"],
}

# 配色主題：依標題自動挑一組，每天看起來不一樣但都是同一個品牌調性
THEMES = [
    dict(name="墨綠金", bg1=(14, 52, 42), bg2=(30, 92, 70), accent=(244, 190, 72),
         card=(255, 255, 255), card_text=(24, 40, 34), sub=(206, 226, 214), line=(255, 255, 255, 22)),
    dict(name="深藍橘", bg1=(13, 30, 64), bg2=(28, 66, 120), accent=(255, 146, 64),
         card=(255, 255, 255), card_text=(20, 30, 52), sub=(200, 214, 238), line=(255, 255, 255, 22)),
    dict(name="大地棕", bg1=(70, 40, 24), bg2=(128, 78, 44), accent=(255, 214, 120),
         card=(255, 250, 242), card_text=(60, 36, 22), sub=(240, 220, 196), line=(255, 255, 255, 20)),
    dict(name="酒紅金", bg1=(86, 18, 32), bg2=(140, 36, 54), accent=(255, 205, 110),
         card=(255, 252, 248), card_text=(70, 20, 30), sub=(244, 208, 214), line=(255, 255, 255, 20)),
]

NO_LINE_START = set("，。、；：！？）」』》〉%,.;:!?)]】…")


@dataclass
class CardData:
    headline: str                 # 大標題，14 字內最好
    points: list[str]             # 3～5 點，每點 20 字內
    subtitle: str = ""            # 副標（一句話痛點）
    tag: str = "土地知識"          # 左上角分類標籤
    highlight: str = ""           # 標題裡要上色的關鍵字（必須是標題的一部分）
    date: str = ""

    @classmethod
    def from_dict(cls, d: dict, date: str = "") -> "CardData":
        d = d or {}
        pts = [str(p).strip() for p in (d.get("points") or []) if str(p).strip()][:5]
        return cls(headline=str(d.get("headline", "")).strip(), points=pts,
                   subtitle=str(d.get("subtitle", "")).strip(),
                   tag=str(d.get("tag", "") or "土地知識").strip()[:6],
                   highlight=str(d.get("highlight", "")).strip(), date=date)

    def all_text(self) -> str:
        return "\n".join([self.tag, self.headline, self.subtitle, *self.points])


# ───────────────────────── 字型 ─────────────────────────
_font_cache: dict = {}


def _find_font_file(weight: str) -> str:
    for d in FONT_DIRS:
        for f in FONT_FILES[weight]:
            p = os.path.join(d, f)
            if os.path.exists(p):
                return p
    raise FileNotFoundError(
        "找不到中文字型。Streamlit Cloud 請確認 repo 裡有 packages.txt（內容 fonts-noto-cjk），"
        "或把 NotoSansTC-Bold.otf 等字型放到 repo 的 fonts/ 資料夾。")


def _tc_index(path: str) -> int:
    """.ttc 內含多國字型，挑繁體中文（TC）那一個"""
    if not path.lower().endswith(".ttc"):
        return 0
    for i in range(10):
        try:
            f = ImageFont.truetype(path, 20, index=i)
        except Exception:  # noqa: BLE001
            break
        if " TC" in " ".join(f.getname()):
            return i
    return 0


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key not in _font_cache:
        path = _find_font_file(weight)
        _font_cache[key] = ImageFont.truetype(path, size, index=_tc_index(path))
    return _font_cache[key]


# ───────────────────────── 排版工具 ─────────────────────────
def wrap(text: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """中文逐字換行，標點不放行首"""
    lines, cur = [], ""
    for ch in text:
        if f.getlength(cur + ch) <= max_w or not cur:
            cur += ch
        elif ch in NO_LINE_START:   # 標點黏在上一行尾巴
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    # 最後一行只剩 1 個字很難看 → 從上一行借一個字
    if len(lines) >= 2 and len(lines[-1]) == 1 and len(lines[-2]) > 2:
        lines[-1] = lines[-2][-1] + lines[-1]
        lines[-2] = lines[-2][:-1]
    return lines


def _wrap_segments(text: str, f, max_w: int) -> list[str]:
    """文字裡的 \\n 是指定斷行點（例如標題在語意處換行）"""
    out = []
    for seg in text.replace("\\n", "\n").split("\n"):
        if seg.strip():
            out += wrap(seg.strip(), f, max_w)
    return out


def fit_font(text: str, weight: str, max_w: int, max_lines: int, sizes: range) -> tuple:
    for s in sizes:
        f = font(weight, s)
        ls = _wrap_segments(text, f, max_w)
        if len(ls) <= max_lines:
            return f, ls
    f = font(weight, sizes[-1])
    return f, _wrap_segments(text, f, max_w)


def _gradient(t: dict) -> Image.Image:
    # 斜向漸層：旋轉內建線性漸層再裁切（比逐點計算快很多）
    big = Image.linear_gradient("L").resize((W * 2, H * 2)).rotate(18, resample=Image.BICUBIC)
    mask = big.crop((W // 2, H // 2, W // 2 + W, H // 2 + H))
    return Image.composite(Image.new("RGB", (W, H), t["bg2"]), Image.new("RGB", (W, H), t["bg1"]), mask)


def _decor(img: Image.Image, t: dict, seed: int):
    """地籍圖風格的淡淡分割線＋光暈，呼應「土地」又不搶字"""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    import random
    rnd = random.Random(seed)
    # 斜向地籍分割線（右上角區塊）
    for _ in range(8):
        x0 = rnd.randint(W // 2, W + 200)
        y0 = rnd.randint(-100, H // 3)
        x1 = x0 + rnd.randint(-420, -120)
        y1 = y0 + rnd.randint(160, 520)
        d.line([(x0, y0), (x1, y1)], fill=t["line"], width=3)
    for _ in range(4):
        y = rnd.randint(0, H // 3)
        d.line([(W // 2 + rnd.randint(0, 200), y), (W, y + rnd.randint(-60, 60))], fill=t["line"], width=2)
    img.paste(layer, (0, 0), layer)
    # 右上角暖色光暈
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W - 420, -260, W + 260, 420], fill=(*t["accent"], 60))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    img.paste(glow, (0, 0), glow)


def _rounded_shadow(img: Image.Image, box, radius: int, fill, shadow=True):
    if shadow:
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle(
            [box[0], box[1] + 10, box[2], box[3] + 10], radius, fill=(0, 0, 0, 70))
        sh = sh.filter(ImageFilter.GaussianBlur(14))
        img.paste(sh, (0, 0), sh)
    ImageDraw.Draw(img).rounded_rectangle(box, radius, fill=fill)


def _draw_headline(d: ImageDraw.ImageDraw, lines: list[str], f, x: int, y: int,
                   highlight: str, base_col, hl_col, line_h: int) -> int:
    """畫標題，highlight 關鍵字上強調色並加底線色塊"""
    full = "".join(lines)
    hl_start = full.find(highlight) if highlight else -1
    hl_range = range(hl_start, hl_start + len(highlight)) if hl_start >= 0 else range(0)
    idx = 0
    bottom = d.textbbox((0, 0), "國", font=f)[3]  # 字的底緣
    for ln in lines:
        cx = x
        for ch in ln:
            w = f.getlength(ch)
            if idx in hl_range:
                # 字下方的強調底線（不壓到字）
                d.rectangle([cx, y + bottom + 8, cx + w, y + bottom + 18], fill=hl_col)
                d.text((cx, y), ch, font=f, fill=hl_col)
            else:
                d.text((cx, y), ch, font=f, fill=base_col)
            cx += w
            idx += 1
        y += line_h
    return y


# ───────────────────────── 主函式 ─────────────────────────
def render(card: CardData, author: str = "歸仁土地仲介｜丘正其",
           search_hint: str = "Google搜尋「歸仁土地仲介」", theme: Optional[int] = None) -> bytes:
    if not card.headline or not card.points:
        raise ValueError("配圖缺少標題或重點")
    seed = int(hashlib.md5(card.headline.encode()).hexdigest(), 16)
    t = THEMES[(seed % len(THEMES)) if theme is None else theme % len(THEMES)]

    img = _gradient(t).convert("RGBA")
    _decor(img, t, seed)
    d = ImageDraw.Draw(img)
    inner = W - PAD * 2

    # 頂部：分類標籤膠囊 + 日期
    y = PAD
    ft = font("bold", 34)
    tw = ft.getlength(card.tag)
    d.rounded_rectangle([PAD, y, PAD + tw + 56, y + 62], 31, fill=t["accent"])
    d.text((PAD + 28, y + 9), card.tag, font=ft, fill=t["bg1"])
    if card.date:
        fd = font("medium", 28)
        d.text((W - PAD - fd.getlength(card.date), y + 16), card.date, font=fd, fill=t["sub"])
    y += 62 + 44

    # 大標題（自動縮字，最多 3 行）
    fh, hl = fit_font(card.headline, "black", inner, 3, range(104, 63, -4))
    lh = int(fh.size * 1.28)
    highlight = card.highlight if card.highlight and card.highlight in "".join(hl) else ""
    y = _draw_headline(d, hl, fh, PAD, y, highlight, (255, 255, 255), t["accent"], lh)
    y += 12

    # 副標
    if card.subtitle:
        fs, sl = fit_font(card.subtitle, "medium", inner, 2, range(40, 29, -2))
        for ln in sl:
            d.text((PAD, y), ln, font=fs, fill=t["sub"])
            y += int(fs.size * 1.45)
    y += 30

    # 重點卡：剩餘空間平均分配
    footer_h = 150
    n = len(card.points)
    gap = 22
    avail = H - footer_h - y - 10
    card_h = min(190, (avail - gap * (n - 1)) // n)
    num_d = min(92, card_h - 36)
    text_x0 = PAD + 32 + num_d + 30
    text_w = W - PAD - 36 - text_x0
    # 先決定一個所有卡片共用的字級，視覺比較整齊
    for size in range(46, 29, -2):
        fp = font("bold", size)
        if all(len(wrap(p, fp, text_w)) * size * 1.32 <= card_h - 28 for p in card.points):
            break
    for i, p in enumerate(card.points, 1):
        box = [PAD, y, W - PAD, y + card_h]
        _rounded_shadow(img, box, 26, t["card"])
        d = ImageDraw.Draw(img)
        # 左側強調色條
        d.rounded_rectangle([PAD, y, PAD + 12, y + card_h], 6, fill=t["accent"])
        # 編號圓
        cy = y + card_h // 2
        cx0 = PAD + 32
        d.ellipse([cx0, cy - num_d // 2, cx0 + num_d, cy + num_d // 2], fill=t["bg1"])
        fn = font("black", int(num_d * 0.52))
        s = str(i)
        d.text((cx0 + num_d / 2, cy), s, font=fn, fill=t["accent"], anchor="mm")
        # 重點文字（垂直置中）
        ls = wrap(p, fp, text_w)
        lh2 = int(fp.size * 1.32)
        ty = cy - (lh2 * len(ls)) // 2 + 2
        for ln in ls:
            d.text((text_x0, ty), ln, font=fp, fill=t["card_text"])
            ty += lh2
        y += card_h + gap

    # 底部：建議收藏 + 署名
    fy = H - footer_h + 30
    d.line([(PAD, fy - 18), (W - PAD, fy - 18)], fill=(*t["sub"], 120), width=2)
    fb = font("bold", 34)
    save = "建議收藏・分享給需要的人"
    d.text((PAD, fy), save, font=fb, fill=t["accent"])
    fa = font("bold", 32)
    d.text((PAD, fy + 52), author, font=fa, fill=(255, 255, 255))
    fsx = font("medium", 26)
    d.text((W - PAD - fsx.getlength(search_hint), fy + 58), search_hint, font=fsx, fill=t["sub"])

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def check(card: CardData) -> list[str]:
    """配圖文字的硬性檢查（不靠模型）"""
    issues = []
    head = card.headline.replace("\\n", "").replace("\n", "")
    if not head:
        issues.append("【配圖】缺少配圖大標題 card.headline。")
    elif len(head) > 18:
        issues.append(f"【配圖】配圖大標題「{head}」太長（{len(head)} 字），請縮到 14 字內。")
    if not 3 <= len(card.points) <= 5:
        issues.append(f"【配圖】配圖重點要 3～5 點，目前 {len(card.points)} 點。")
    for p in card.points:
        if len(p) > 24:
            issues.append(f"【配圖】配圖重點「{p}」太長（{len(p)} 字），請縮到 20 字內。")
    if card.highlight and card.highlight not in head:
        issues.append(f"【配圖】highlight「{card.highlight}」必須是大標題裡的字。")
    return issues
