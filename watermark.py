# -*- coding: utf-8 -*-
"""EXIF 水印引擎：解析照片元数据并渲染水印。

模板:
  drone       — 底部半透明信息条(日期时间/GPS/海拔/机型/拍摄参数, 大疆风格)
  corner      — 简约角标(圆角半透明背景)
  custom      — 自定义文字 + 可选日期
  standard1   — 白卡片信息条: 机型/镜头/参数/时间 + 品牌 Logo (仿 semi-utils)
  standard2   — 社交卡片: 白色留白 + 圆角阴影照片 + 信息条
  blur        — 模糊背景: 照片模糊放大做背景, 居中圆角照片 + 白色信息
  center_logo — 品牌留白: 白边相框 + 底部居中品牌 Logo
"""
import os
import re
from fractions import Fraction

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

# 信息条高度倍率上限: 实测 1.9× 起 Logo 开始被压缩, 2.1~2.2× 完全消失, 故锁定 1.8
BAR_SCALE_MAX = 1.8
_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_DIR = os.path.join(_ASSETS, "logos")

_FONT_FILES = {
    "bold": [
        os.path.join(_ASSETS, "fonts", "AlibabaPuHuiTi-2-85-Bold.otf"),
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\arialbd.ttf",
    ],
    "med": [
        os.path.join(_ASSETS, "fonts", "Roboto-Medium.ttf"),
        os.path.join(_ASSETS, "fonts", "AlibabaPuHuiTi-2-85-Bold.otf"),
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\arial.ttf",
    ],
    "light": [
        os.path.join(_ASSETS, "fonts", "AlibabaPuHuiTi-2-45-Light.otf"),
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ],
}
_font_cache = {}


def _load_font(size, bold=False, weight=None):
    if weight is None:
        weight = "bold" if bold else "light"
    key = (int(size), weight)
    if key not in _font_cache:
        font = None
        for p in _FONT_FILES[weight]:
            if os.path.exists(p):
                try:
                    font = ImageFont.truetype(p, key[0])
                    break
                except OSError:
                    continue
        _font_cache[key] = font or ImageFont.load_default()
    return _font_cache[key]


def _num(v):
    """EXIF 有理数/字符串安全转 float。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return float(Fraction(str(v)))
        except Exception:
            return None


def _dms_str(coord, ref):
    if not coord or len(coord) < 3:
        return ""
    d, m, s = (_num(c) or 0 for c in coord[:3])
    return f"{ref} {int(d)}°{int(m):02d}′{s:05.2f}″"


# DJI 相机模组代号 → 机型名 (来源: Wikimedia Commons / Pix4D / mmt 项目整理)
_DJI_MODEL_MAP = {
    "fc1102": "Spark",
    "fc220": "Mavic Pro",
    "fc2204": "Mavic 2 Zoom",
    "l1d-20": "Mavic 2 Pro", "l1d-20c": "Mavic 2 Pro",
    "fc300c": "Phantom 3", "fc300s": "Phantom 3 Advanced",
    "fc300x": "Phantom 3 Professional",
    "fc330": "Phantom 4",
    "fc6310": "Phantom 4 Pro", "fc6310s": "Phantom 4 Pro V2.0",
    "fc6310r": "Phantom 4 RTK",
    "fc2103": "Mavic Air", "fc3170": "Mavic Air 2", "fc3411": "Air 2S",
    "fc7203": "Mavic Mini", "fc7303": "Mini 2", "fc3682": "Mini 3",
    "fc3582": "Mini 3 Pro", "fc8482": "Mini 4 Pro", "fc9313": "Mini 5 Pro",
    "fc8282": "Air 3", "fc8284": "Air 3",
    "fc9113": "Air 3S", "fc9184": "Air 3S",
    "fc8183": "Avata", "fc8485": "Avata 2", "fc8582": "Flip",
    "fc3305": "FPV",
    "l2d-20c": "Mavic 3", "l2s-20c": "Mavic 3 Pro",
    "m3e": "Mavic 3 Enterprise", "m3t": "Mavic 3T Thermal", "m30t": "Matrice 30T",
    "fc350": "Zenmuse X3", "fc550": "Zenmuse X5",
    "fc6510": "Zenmuse X4S", "fc6520": "Zenmuse X5S", "fc6540": "Zenmuse X7",
    "ot110": "Osmo Pocket", "hg310": "Osmo",
}


def _dji_friendly_model(img, model):
    """DJI 机型名还原: 优先 XMP 中的 AircraftModel, 其次 FC/L 代号映射表。"""
    xmp = img.info.get("xmp") or img.info.get("XML:com.adobe.xmp") or ""
    if isinstance(xmp, bytes):
        xmp = xmp.decode("utf-8", "ignore")
    if xmp:
        m = (re.search(r'drone-dji:(?:AircraftModel|AircraftName|DroneModel)="([^"]+)"', xmp)
             or re.search(r'<drone-dji:(?:AircraftModel|AircraftName|DroneModel)>'
                          r'([^<]+)<', xmp))
        if m and m.group(1).strip():
            return m.group(1).strip()
    return _DJI_MODEL_MAP.get(model.strip().lower(), model)


def parse_exif(img):
    """从 PIL Image 提取水印所需字段，异常时返回空值字典。"""
    info = {"date": "", "time": "", "make": "", "model": "", "lens": "",
            "gps": "", "altitude": "", "params": ""}
    try:
        exif = img.getexif()
        if not exif:
            return info
        sub = exif.get_ifd(0x8769)   # Exif IFD
        gps = exif.get_ifd(0x8825)   # GPS IFD

        dt = sub.get(36867) or sub.get(36868) or exif.get(306)
        if dt:
            dt = str(dt).replace("-", ":")
            date, _, t = dt.partition(" ")
            info["date"] = date.replace(":", "-")
            info["time"] = t[:5]

        info["make"] = str(exif.get(271) or "").strip()
        info["model"] = str(exif.get(272) or "").strip()
        # DJI: EXIF Model 是相机模组代号(FC3411 等), 还原为机型名
        if "dji" in info["make"].lower() or info["model"].lower() in _DJI_MODEL_MAP:
            info["model"] = _dji_friendly_model(img, info["model"])
        info["lens"] = str(sub.get(42036) or "").strip()  # LensModel 0xA434

        parts = []
        lat, lat_ref = gps.get(2), gps.get(1)
        lon, lon_ref = gps.get(4), gps.get(3)
        if lat and lat_ref:
            parts.append(_dms_str(lat, str(lat_ref)))
        if lon and lon_ref:
            parts.append(_dms_str(lon, str(lon_ref)))
        info["gps"] = " ".join(parts)

        alt = _num(gps.get(6)) if gps.get(6) is not None else None
        if alt is not None:
            info["altitude"] = f"海拔 {alt:.0f}m"

        seg = []
        focal = _num(sub.get(41989)) or _num(sub.get(37386))  # 优先 35mm 等效
        if focal:
            seg.append(f"{focal:g}mm")
        fn = _num(sub.get(33437))
        if fn:
            seg.append(f"f/{fn:.1f}")
        et = _num(sub.get(33434))
        if et:
            seg.append(f"{et:.6g}s" if et >= 1 else f"1/{round(1 / et)}s")
        iso = sub.get(34855)
        if iso:
            seg.append(f"ISO{iso}")
        info["params"] = " ".join(seg)
    except Exception:
        pass
    return info


# ---------------- 品牌 Logo ----------------

_LOGO_MAP = [
    ("dji", "DJI.jpg"), ("iphone", "apple.png"), ("apple", "apple.png"),
    ("canon", "canon.png"), ("fujifilm", "fujifilm.png"), ("fuji", "fujifilm.png"),
    ("hasselblad", "hasselblad.png"), ("leica", "leica_logo.png"),
    ("nikon", "nikon.png"), ("olympus", "olympus_blue_gold.png"),
    ("om system", "olympus_blue_gold.png"), ("panasonic", "panasonic.png"),
    ("lumix", "panasonic.png"), ("pentax", "pentax.png"), ("ricoh", "ricoh.png"),
    ("sony", "sony.png"), ("huawei", "xmage.png"), ("honor", "xmage.png"),
]


def load_brand_logo(make):
    """按品牌名匹配 Logo 图片, 未识别品牌用 default, 无品牌返回 None。"""
    m = (make or "").strip().lower()
    if not m:
        return None
    name = "default.png"
    for key, fn in _LOGO_MAP:
        if key in m:
            name = fn
            break
    p = os.path.join(LOGO_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        logo = Image.open(p)
        logo.load()
        return logo.convert("RGBA")
    except Exception:
        return None


def _fit_height(img, h):
    w = max(1, int(img.width * h / img.height))
    return img.resize((w, int(h)), Image.LANCZOS)


# ---------------- 通用绘制工具 ----------------

def _measure(d, text, font):
    box = d.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _rounded(img, radius):
    img = img.convert("RGBA")
    radius = int(radius)
    if radius <= 0:
        return img
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, img.width - 1, img.height - 1], radius=radius, fill=255)
    img.putalpha(mask)
    return img


def _cover(img, w, h):
    """等比缩放并居中裁剪到 w×h。"""
    sw, sh = img.size
    s = max(w / sw, h / sh)
    nw, nh = int(sw * s) + 1, int(sh * s) + 1
    tmp = img.resize((nw, nh), Image.LANCZOS)
    bx, by = (nw - w) // 2, (nh - h) // 2
    return tmp.crop((bx, by, bx + w, by + h))


def _paste_with_shadow(canvas, img, xy, blur=20, alpha=100, offset=(0, 10)):
    """照片带投影贴入画布。阴影在小尺寸上生成再放大(柔和渐变无需全分辨率)。"""
    x, y = int(xy[0]), int(xy[1])
    ox, oy = int(offset[0]), int(offset[1])
    m = int(blur) + 8
    w, h = img.width + 2 * m, img.height + 2 * m

    # 小尺寸 alpha 阴影 → 模糊 → 放大, 等效大半径模糊
    sw, sh = max(2, w // 8), max(2, h // 8)
    a_small = img.split()[3].resize(
        (max(1, img.width * sw // w), max(1, img.height * sh // h)), Image.BILINEAR)
    small = Image.new("L", (sw, sh), 0)
    small.paste(a_small, ((sw - a_small.width) // 2, (sh - a_small.height) // 2))
    small = small.filter(ImageFilter.GaussianBlur(max(1, blur // 8)))
    shadow = small.resize((w, h), Image.BILINEAR)

    patch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    patch.paste(Image.new("RGBA", (w, h), (0, 0, 0, alpha)), (0, 0), shadow)
    canvas.alpha_composite(patch, (x + ox - m, y + oy - m))
    canvas.alpha_composite(img, (x, y))


def _clean_make(m):
    """去掉厂商名中的公司后缀: NIKON CORPORATION → NIKON"""
    return re.sub(r"\s*(corporation|corp\.?|imaging corp\.?|company|ltd\.?|inc\.?)\s*$",
                  "", (m or "").strip(), flags=re.I)


def _nice_brand(s):
    """全大写品牌名规范化: NIKON → Nikon"""
    for a, b in [("NIKON", "Nikon"), ("CANON", "Canon"), ("SONY", "Sony"),
                 ("FUJIFILM", "Fujifilm"), ("RICOH", "Ricoh"), ("PENTAX", "Pentax"),
                 ("OLYMPUS", "Olympus"), ("PANASONIC", "Panasonic")]:
        s = re.sub(r"\b" + a + r"\b", b, s)
    return s


# 品牌强调色（信息条竖分隔线）
_BRAND_ACCENT = {
    "nikon": (255, 225, 0), "canon": (210, 10, 46), "sony": (85, 85, 85),
    "fujifilm": (0, 180, 237), "hasselblad": (51, 51, 51), "leica": (226, 6, 18),
    "dji": (43, 43, 43), "panasonic": (15, 91, 181), "pentax": (0, 169, 92),
    "olympus": (43, 111, 179), "huawei": (199, 0, 11), "honor": (0, 179, 227),
}


def _accent_color(make):
    m = (make or "").lower()
    for k, c in _BRAND_ACCENT.items():
        if k in m:
            return c
    return (216, 216, 216)


def _model_text(exif):
    make = _nice_brand(_clean_make(exif.get("make", "")))
    model = _nice_brand((exif.get("model") or "").strip())
    if make and model.upper().startswith(make.upper()):
        model = model[len(make):].strip()
    return " ".join(x for x in (make, model) if x)


# ---------------- 基础三模板(覆盖式水印) ----------------

def _render_drone(overlay, exif, opt):
    """底部信息条: 左侧日期+GPS/海拔, 右侧机型+拍摄参数。"""
    W, H = overlay.size
    d = ImageDraw.Draw(overlay)
    scale = float(opt.get("font_scale", 1.0))
    opacity = float(opt.get("opacity", 0.55))

    base = max(20, int(W / 55 * scale))
    f_main = _load_font(base, weight="bold")
    f_sub = _load_font(max(14, int(base * 0.62)))
    pad = int(base * 0.8)

    left1, left2, right1, right2 = "", "", "", ""
    if opt.get("show_date", True) and exif["date"]:
        left1 = f"{exif['date']}  {exif['time']}".strip()
    if opt.get("show_gps", True) and exif["gps"]:
        left2 = exif["gps"]
    if opt.get("show_altitude", True) and exif["altitude"]:
        left2 = (left2 + "   " + exif["altitude"]).strip()
    if opt.get("show_model", True):
        right1 = _model_text(exif)
    if opt.get("show_params", True) and exif["params"]:
        right2 = exif["params"]

    if not any((left1, left2, right1, right2)):
        left1 = (opt.get("custom_text") or "").strip() or "无 EXIF 信息"
        left2 = ""

    h1 = d.textbbox((0, 0), "Ag1", font=f_main)[3]
    h2 = d.textbbox((0, 0), "Ag1", font=f_sub)[3]
    gap = int(base * 0.35)
    bar_h = pad * 2 + h1 + (gap + h2 if (left2 or right2) else 0)
    y0 = H - bar_h

    d.rectangle([0, y0, W, H], fill=(0, 0, 0, int(255 * opacity)))
    d.rectangle([0, y0, W, y0 + max(2, base // 15)], fill=(255, 199, 44, 200))

    if left2 or right2:
        d.text((pad, y0 + pad), left1, font=f_main, fill=(255, 255, 255, 255))
        d.text((pad, y0 + pad + h1 + gap), left2, font=f_sub, fill=(255, 213, 79, 255))
        ry = y0 + pad
        if right1:
            rw = _measure(d, right1, f_main)[0]
            d.text((W - pad - rw, ry), right1, font=f_main, fill=(255, 255, 255, 255))
        if right2:
            rw = _measure(d, right2, f_sub)[0]
            d.text((W - pad - rw, ry + h1 + gap), right2, font=f_sub,
                   fill=(255, 255, 255, 230))
    else:
        d.text((pad, y0 + pad), left1, font=f_main, fill=(255, 255, 255, 255))
        if right1:
            rw = _measure(d, right1, f_main)[0]
            d.text((W - pad - rw, y0 + pad), right1, font=f_main,
                   fill=(255, 255, 255, 255))


def _render_corner(overlay, exif, opt, custom=False):
    """圆角半透明角标; custom=True 时渲染自定义文字(+可选日期)。"""
    W, H = overlay.size
    d = ImageDraw.Draw(overlay)
    scale = float(opt.get("font_scale", 1.0))
    opacity = float(opt.get("opacity", 0.55))
    pos = opt.get("position", "bottom-left")

    base = max(16, int(W / 70 * scale))
    f_main = _load_font(base, weight="bold")
    f_sub = _load_font(max(12, int(base * 0.7)))
    pad = int(base * 0.7)
    gap = int(base * 0.35)

    lines = []
    if custom:
        for ln in (opt.get("custom_text") or "").splitlines():
            if ln.strip():
                lines.append((ln.strip(), f_main, (255, 255, 255, 255)))
        if opt.get("show_date", False) and exif["date"]:
            lines.append((f"{exif['date']} {exif['time']}", f_sub, (255, 213, 79, 255)))
    else:
        if opt.get("show_date", True) and exif["date"]:
            lines.append((f"{exif['date']}  {exif['time']}", f_main, (255, 255, 255, 255)))
        if opt.get("show_gps", True) and exif["gps"]:
            lines.append((exif["gps"], f_sub, (255, 213, 79, 255)))
        if opt.get("show_altitude", True) and exif["altitude"]:
            lines.append((exif["altitude"], f_sub, (255, 255, 255, 240)))
    if not lines:
        lines = [(opt.get("custom_text") or "无 EXIF 信息", f_main, (255, 255, 255, 255))]

    sizes = [_measure(d, t, f) for t, f, _ in lines]
    bw = max(w for w, _ in sizes) + pad * 2
    bh = sum(h for _, h in sizes) + gap * (len(lines) - 1) + pad * 2

    margin = max(20, int(W / 60))
    y = margin if str(pos).endswith("top") else H - margin - bh
    x = margin if str(pos).endswith("left") else W - margin - bw

    d.rounded_rectangle([x, y, x + bw, y + bh], radius=int(base * 0.5),
                        fill=(0, 0, 0, int(255 * opacity)))
    cy = y + pad
    for (t, f, c), (_, h) in zip(lines, sizes):
        d.text((x + pad, cy), t, font=f, fill=c)
        cy += h + gap


# ---------------- 白卡片信息条(standard1 / standard2 共用) ----------------

def _draw_info_block(canvas, area, exif, opt, logo, cfg):
    """白色区域内绘制信息条（semi-utils WatermarkFilter 布局 + 宽度自适应）。

    cfg: dict(pad_x, pad_r, divider, right_align_left)
    左块: 机型(粗体黑)上 + 镜头(常规)下; 右块: 参数(粗体)上 + 日期(常规)下, 与左块底对齐。
    分隔线(浅灰) + Logo 紧贴其左侧。
    宽度自适应: 左右文本 + Logo 放不进画布时, 等比缩小字号(0.4× 下限), 仍不够则省略镜头行,
    避免窄幅(竖构图)照片出现文字重叠与 Logo 消失。
    """
    d = ImageDraw.Draw(canvas)
    x0, y0, x1, y1 = area
    h = y1 - y0
    W = x1 - x0
    common_spacing = int(0.02 * W)
    middle_spacing = int(h * 0.05)
    _PHT_LINE_RATIO = 1.4023
    t_norm_base = int(h * 0.3)
    t_bold_base = int(h * 0.3 * 1.13)
    pos = str(opt.get("logo_position", "right")).lower()

    model = _model_text(exif).replace("_", "") if opt.get("show_model", True) else ""
    lens = exif.get("lens", "") if opt.get("show_lens", True) else ""
    params = exif.get("params", "") if opt.get("show_params", True) else ""
    date = f"{exif.get('date', '')} {exif.get('time', '')}".strip() \
        if opt.get("show_date", True) else ""

    def build(scale, with_lens=True):
        """按字号缩放系数构建一次排版度量。"""
        s = max(0.2, min(1.0, scale))
        t_n = max(8, int(t_norm_base * s))
        t_b = max(8, int(t_bold_base * s))
        m = {
            "s": s, "t_norm": t_n, "t_bold": t_b,
            "f_model": _load_font(max(9, round(t_b / _PHT_LINE_RATIO)), weight="bold"),
            "f_param": _load_font(max(9, round(t_n / _PHT_LINE_RATIO)), weight="bold"),
            "f_norm": _load_font(max(9, round(t_n / _PHT_LINE_RATIO)), weight="light"),
            "lens": lens if with_lens else "",
        }
        m["mw"] = _measure(d, model, m["f_model"])[0] if model else 0
        m["lw"] = _measure(d, m["lens"], m["f_norm"])[0] if m["lens"] else 0
        m["pw"] = _measure(d, params, m["f_param"])[0] if params else 0
        m["dw"] = _measure(d, date, m["f_norm"])[0] if date else 0
        m["left_w"] = max(m["mw"], m["lw"])
        m["right_w"] = max(m["pw"], m["dw"])
        # 垂直: 行盒堆叠, 列高随字号缩放
        lt_h = t_b if model else 0
        lb_h = t_n if m["lens"] else 0
        rt_h = t_n if params else 0
        rb_h = t_n if date else 0
        mid = middle_spacing if (model and m["lens"]) else 0
        m["elem_h"] = max(lt_h + (mid + lb_h if m["lens"] else 0),
                          rt_h + (middle_spacing + rb_h if date else 0))
        m["lt_h"], m["lb_h"], m["rt_h"], m["rb_h"] = lt_h, lb_h, rt_h, rb_h
        m["lg"] = _fit_height(logo, m["elem_h"]) if logo is not None else None
        m["lg_w"] = m["lg"].width if m["lg"] is not None else 0
        return m

    def need_width(m):
        return (cfg["pad_x"] + cfg["pad_r"] + m["left_w"] + m["right_w"]
                + m["lg_w"] + 5 * common_spacing)

    # 选缩放: 逐档尝试, 找第一个放得下的
    chosen = None
    for cand in [1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, 0.45, 0.4, 0.35, 0.3]:
        m = build(cand)
        if need_width(m) <= W * 0.995:
            chosen = m
            break
    if chosen is None:      # 极小画布: 省略镜头行再试
        chosen = build(0.3, with_lens=False)
        for cand in [0.4, 0.35, 0.3, 0.25]:
            m = build(cand, with_lens=False)
            if need_width(m) <= W * 0.995:
                chosen = m
                break
        else:
            chosen = build(0.25, with_lens=False)

    m = chosen
    f_model, f_param, f_norm = m["f_model"], m["f_param"], m["f_norm"]
    lens = m["lens"]
    left_w, right_w = m["left_w"], m["right_w"]
    elem_h = min(m["elem_h"], h)
    elem_margin = max(0, (h - elem_h) // 2)
    lt_h, lb_h, rt_h, rb_h = m["lt_h"], m["lb_h"], m["rt_h"], m["rb_h"]
    mid_lb = middle_spacing if lens else 0
    lt_y = y0 + elem_margin
    lb_y = lt_y + lt_h + mid_lb
    rt_y = (lt_y + lt_h - rt_h) if model else (y0 + elem_margin)
    rb_y = (lb_y + lb_h - rb_h) if lens else (rt_y + rt_h + (middle_spacing if (params and date) else 0))

    # 左块 + 左侧 Logo
    lg = m["lg"]
    lx = x0 + cfg["pad_x"] + common_spacing
    if lg is not None and pos == "left":
        canvas.alpha_composite(lg, (lx, lt_y))
        lx += lg.width + common_spacing
    if model:
        d.text((lx, lt_y), model, font=f_model, fill=(0, 0, 0))
    if lens:
        d.text((lx, lb_y), lens, font=f_norm, fill=(36, 36, 36))
    left_end = lx + left_w

    # 右块
    r_base = x1 - cfg["pad_r"]
    rt_x = r_base - m["pw"] - common_spacing
    rb_x = r_base - m["dw"] - common_spacing
    if cfg["right_align_left"]:
        rt_x = rb_x = min(rt_x, rb_x)
    if params:
        d.text((rt_x, rt_y), params, font=f_param, fill=(36, 36, 36))
    if date:
        d.text((rb_x, rb_y), date, font=f_norm, fill=(36, 36, 36))

    # 分隔线 + 右侧/居中 Logo（放不下时逐级缩小, 下限按画布高度）
    if lg is not None and pos != "left":
        div_w = max(2, int(W * 0.003)) if cfg["divider"] else 0
        div_x = r_base - right_w - 2 * common_spacing - div_w
        if cfg["divider"]:
            d.rectangle([div_x, int(y0 + elem_margin - elem_h * 0.05),
                         div_x + div_w, int(y0 + elem_margin + elem_h * 1.05)],
                        fill=(216, 216, 214))
        min_h = max(8, int(h * 0.08))
        if pos == "right":
            logo_x = div_x - common_spacing - lg.width
            while logo_x < left_end + common_spacing and lg.height > min_h:
                lg = _fit_height(logo, max(min_h, int(lg.height * 0.85)))
                logo_x = div_x - common_spacing - lg.width
            if logo_x >= left_end + common_spacing:
                canvas.alpha_composite(lg, (logo_x, y0 + elem_margin))
        else:  # center
            logo_x = (x0 + x1 - lg.width) // 2
            while (not (logo_x > left_end + common_spacing
                        and logo_x + lg.width < div_x - common_spacing)
                   and lg.height > min_h):
                lg = _fit_height(logo, max(min_h, int(lg.height * 0.85)))
                logo_x = (x0 + x1 - lg.width) // 2
            if (logo_x > left_end + common_spacing
                    and logo_x + lg.width < div_x - common_spacing):
                canvas.alpha_composite(lg, (logo_x, y0 + (elem_h - lg.height) // 2))


def _render_standard1(img, exif, opt, logo):
    W, H = img.size
    bs = min(BAR_SCALE_MAX, max(0.3, float(opt.get("bar_scale", 1.0))))
    bar_h = int(H * 0.12 * bs)   # semi: bottom_margin = 12% 照片高
    canvas = Image.new("RGBA", (W, H + bar_h), (255, 255, 255, 255))
    canvas.paste(img.convert("RGBA"), (0, 0))
    _draw_info_block(
        canvas, (0, H, W, H + bar_h), exif, opt, logo,
        {"pad_x": 0, "pad_r": 0,
         "divider": True, "right_align_left": True})
    return canvas.convert("RGB")


def _glow_shadow(canvas, img, xy, radius=100, alpha=180):
    """对称光晕式阴影(仿 semi ShadowFilter): 剪影→模糊→gamma1.5衰减, 小图渲染再放大。radius<=0 时仅贴图。"""
    if radius <= 0:
        canvas.alpha_composite(img, (int(xy[0]), int(xy[1])))
        return
    x, y = int(xy[0]), int(xy[1])
    w, h = img.size
    margin = radius * 2
    tw, th = w + 2 * margin, h + 2 * margin
    scale = 8
    sw, sh = max(4, tw // scale), max(4, th // scale)
    iw, ih = max(2, w * sw // tw), max(2, h * sh // th)
    a_full = img.split()[3].point(lambda v: v * alpha // 255)
    a_small = Image.new("L", (sw, sh), 0)
    a_small.paste(a_full.resize((iw, ih), Image.BILINEAR),
                  ((sw - iw) // 2, (sh - ih) // 2))
    a_small = a_small.filter(ImageFilter.GaussianBlur(max(1, radius // scale)))
    lut = [int((i / 255.0) ** 1.5 * 255 + 0.5) for i in range(256)]
    shadow = a_small.point(lut).resize((tw, th), Image.BILINEAR)
    patch = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    patch.paste(Image.new("RGBA", (tw, th), (0, 0, 0, 255)), (0, 0), shadow)
    canvas.alpha_composite(patch, (x - margin, y - margin))
    canvas.alpha_composite(img, (x, y))


def _render_standard2(img, exif, opt, logo):
    """社交卡片: 按 semi 标准水印2 管道复刻。
    margin_scale 统一缩放白边: 1.0 = 每侧约170px(用户选定尺寸), 范围 0.3~3。
    """
    W, H = img.size
    ms = min(3.0, max(0.0, float(opt.get("margin_scale", 1.0))))
    m = max(0, int(57 * ms))             # 左/右/上白边
    sh_r = max(0, int(57 * ms))          # 阴影半径(随白边缩放, 0=无阴影)
    radius = int(H * 0.01)               # 圆角 1% 照片高
    pad = sh_r * 2                       # 阴影画布扩展
    bs = min(BAR_SCALE_MAX, max(0.3, float(opt.get("bar_scale", 1.0))))
    bar_h = int((H + 2 * pad) * 0.12 * bs)
    bottom_extra = int(H * 0.02)

    cw = W + 2 * pad + 2 * m
    ch = m + (H + 2 * pad) + bar_h + bottom_extra
    canvas = Image.new("RGBA", (cw, ch), (255, 255, 255, 255))

    px, py = m + pad, m + pad
    ph = _rounded(img, radius)
    _glow_shadow(canvas, ph, (px, py), radius=sh_r, alpha=180)

    footer_y = m + H + 2 * pad
    _draw_info_block(
        canvas, (0, footer_y, cw, footer_y + bar_h), exif, opt, logo,
        {"pad_x": m, "pad_r": m,
         "divider": False, "right_align_left": False})
    return canvas.convert("RGB")


def _render_blur(img, exif, opt):
    W, H = img.size
    pw = int(W * 0.72)
    ph = int(pw * H / W)
    cw, ch = W, int(H * 1.36)

    # 背景: 缩小→模糊→拉伸 (视觉等效大半径模糊, 快一个数量级)
    small_w = max(2, cw // 8)
    small_h = max(2, int(small_w * ch / cw))
    bg = _cover(img, small_w, small_h)
    bg = bg.filter(ImageFilter.GaussianBlur(3))
    bg = bg.resize((cw, ch), Image.BICUBIC)
    bg = ImageEnhance.Brightness(bg).enhance(0.72)
    canvas = bg.convert("RGBA")

    model = _model_text(exif) if opt.get("show_model", True) else ""
    params = exif.get("params", "") if opt.get("show_params", True) else ""
    f_model = _load_font(int(cw * 0.019), weight="bold")
    f_param = _load_font(int(cw * 0.015), weight="light")
    d = ImageDraw.Draw(canvas)
    hm = _measure(d, model or "Ag", f_model)[1] if model else 0
    hp = _measure(d, params or "Ag", f_param)[1] if params else 0
    pad_text = int(cw * 0.024)
    text_h = hm + (pad_text + hp if params else 0)

    content_h = ph + pad_text + text_h
    px = (cw - pw) // 2
    py = max(int(cw * 0.02), (ch - content_h) // 2)
    photo = img.resize((pw, ph), Image.BICUBIC)  # 照片须缩放到画布 72% 宽, 否则超出画布被裁
    _paste_with_shadow(canvas, _rounded(photo, int(pw * 0.015)), (px, py),
                       blur=max(8, int(cw * 0.01)), alpha=130,
                       offset=(0, max(4, int(cw * 0.008))))

    ty = py + ph + pad_text
    if model:
        tw = _measure(d, model, f_model)[0]
        d.text(((cw - tw) // 2, ty), model, font=f_model, fill=(255, 255, 255, 255))
    if params:
        tw = _measure(d, params, f_param)[0]
        d.text(((cw - tw) // 2, ty + hm + pad_text - int(cw * 0.006)),
               params, font=f_param, fill=(255, 255, 255, 235))
    return canvas.convert("RGB")


# ---------------- 品牌留白 ----------------

def _render_center_logo(img, exif, opt, logo):
    W, H = img.size
    ms = min(3.0, max(0.0, float(opt.get("margin_scale", 1.0))))
    mx = int(W * 0.024 * ms)
    top = int(W * 0.024 * ms)
    bottom = int(W * 0.088 * ms)
    canvas = Image.new("RGBA", (W + 2 * mx, top + H + bottom), (255, 255, 255, 255))
    canvas.paste(img.convert("RGBA"), (mx, top))
    d = ImageDraw.Draw(canvas)

    band_y0 = top + H
    custom = (opt.get("custom_text") or "").strip()
    pos = str(opt.get("logo_position", "center")).lower()
    if logo is not None:
        lh = int(W * 0.034)
        lg = _fit_height(logo, lh)
        if pos == "left":
            lx = mx
        elif pos == "right":
            lx = canvas.width - mx - lg.width
        else:
            lx = (canvas.width - lg.width) // 2
        canvas.alpha_composite(lg, (lx, band_y0 + (bottom - lh) // 2))
        # 自定义文字放对侧(Logo 居中时文字靠左)
        if custom:
            f = _load_font(int(W * 0.016), weight="light")
            text = custom.splitlines()[0]
            tw, th = _measure(d, text, f)
            ty = band_y0 + (bottom - th) // 2
            tx = canvas.width - mx - tw if pos == "left" else mx
            d.text((tx, ty), text, font=f, fill=(90, 90, 90))
    else:
        text = custom or exif.get("make", "") or ""
        if text:
            f = _load_font(int(W * 0.024), weight="bold")
            tw, th = _measure(d, text, f)
            d.text(((canvas.width - tw) // 2, band_y0 + (bottom - th) // 2),
                   text, font=f, fill=(20, 20, 20))
    return canvas.convert("RGB")


# ---------------- 入口 ----------------

_OVERLAY_TPL = ("drone", "corner", "custom")


def apply_watermark(img, exif, settings):
    """按 settings 渲染水印, 返回新图(不修改原图)。"""
    tpl = settings.get("template", "drone")

    if tpl in _OVERLAY_TPL:
        rgb = img.convert("RGB")
        overlay = Image.new("RGBA", rgb.size, (0, 0, 0, 0))
        if tpl == "corner":
            _render_corner(overlay, exif, settings)
        elif tpl == "custom":
            _render_corner(overlay, exif, settings, custom=True)
        else:
            _render_drone(overlay, exif, settings)
        return Image.alpha_composite(rgb.convert("RGBA"), overlay).convert("RGB")

    logo = settings.get("_logo")
    if logo is None:
        logo = load_brand_logo(exif.get("make", ""))
    if tpl == "standard1":
        return _render_standard1(img, exif, settings, logo)
    if tpl == "standard2":
        return _render_standard2(img, exif, settings, logo)
    if tpl == "blur":
        return _render_blur(img, exif, settings)
    if tpl == "center_logo":
        return _render_center_logo(img, exif, settings, logo)
    return img.convert("RGB")
