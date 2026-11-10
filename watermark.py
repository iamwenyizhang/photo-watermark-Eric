# -*- coding: utf-8 -*-
"""EXIF 水印引擎：解析照片元数据并渲染水印。

模板:
  standard1   — 白卡片信息条: 机型/镜头/参数/时间 + 品牌 Logo (仿 semi-utils)
  standard2   — 社交卡片: 白色留白 + 圆角阴影照片 + 信息条
  blur        — 模糊背景: 照片模糊放大做背景, 居中圆角照片 + 白色信息
  film        — 胶片边框: 上下深色片基 + 齿孔, 模拟 35mm 底片一格
  polaroid    — 拍立得: 照片缩进白色相纸, 底部宽边标注机型与参数
  center_logo — 品牌留白: 白边相框 + 底部居中品牌 Logo
  normal      — 极简右下角: 一行小字, 无底色
  custom      — 自定义文字 + 可选日期
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


# ---------------- 手机型号代号 → 通俗机型名 ----------------
# 手机照片 EXIF 的 Model 常是厂商内部代号（SM-S928B / ALN-AL00 / 23127PN0CC），
# 普通用户看不懂。此表把已知代号换成通俗机型名，且**不含品牌前缀**
# （品牌由中间 Logo 承担，如 "14" + 小米标）。
# 按品牌分组匹配，避免不同厂商型号代号撞车（如一加的 P 码体系与 OPPO 重叠）。
# 数据来源：三星 Knox 官方支持设备库、华为开发者官网支持设备清单、一加型号对照表、
# 小米官方型号、vivo/iQOO 型号库（MobileModels）。
# 未收录的代号保持原样显示，不做猜测（猜错比不猜更糟）。
_MODEL_CODE_MAP = {
    "samsung": {
        "SM-S938": "Galaxy S25 Ultra", "SM-S936": "Galaxy S25+", "SM-S931": "Galaxy S25",
        "SM-S937": "Galaxy S25 Edge", "SM-S731": "Galaxy S25 FE",
        "SM-S928": "Galaxy S24 Ultra", "SM-S926": "Galaxy S24+", "SM-S921": "Galaxy S24",
        "SM-S721": "Galaxy S24 FE",
        "SM-S918": "Galaxy S23 Ultra", "SM-S916": "Galaxy S23+", "SM-S911": "Galaxy S23",
        "SM-S711": "Galaxy S23 FE",
        "SM-S908": "Galaxy S22 Ultra", "SM-S906": "Galaxy S22+", "SM-S901": "Galaxy S22",
        "SM-G998": "Galaxy S21 Ultra", "SM-G996": "Galaxy S21+", "SM-G991": "Galaxy S21",
        "SM-G990": "Galaxy S21 FE", "SM-G781": "Galaxy S20 FE",
        "SM-F946": "Galaxy Z Fold5", "SM-F936": "Galaxy Z Fold4", "SM-F926": "Galaxy Z Fold3",
        "SM-F731": "Galaxy Z Flip5", "SM-F721": "Galaxy Z Flip4", "SM-F711": "Galaxy Z Flip3",
        "SM-A556": "Galaxy A55", "SM-A546": "Galaxy A54", "SM-A356": "Galaxy A35",
        "SM-A346": "Galaxy A34", "SM-A256": "Galaxy A25", "SM-A245": "Galaxy A24",
        "SM-A236": "Galaxy A23 5G", "SM-A235": "Galaxy A23", "SM-A155": "Galaxy A15",
        "SM-A156": "Galaxy A15 5G", "SM-A145": "Galaxy A14", "SM-A135": "Galaxy A13",
        "SM-M546": "Galaxy M54", "SM-M346": "Galaxy M34", "SM-M156": "Galaxy M15",
        "SM-M146": "Galaxy M14", "SM-M135": "Galaxy M13",
        "SM-N986": "Galaxy Note 20 Ultra",
        "SM-G556": "Galaxy XCover7", "SM-G736": "Galaxy XCover6 Pro",
    },
    "huawei": {
        "HLS-AL00": "Mate 90", "CMS-AL20": "Mate 90 Pro", "CMM-AL00": "Mate 90 Pro Max",
        "VYG-AL00": "Mate 80", "SGT-AL00": "Mate 80 Pro", "SGT-AL50": "Mate 80 Pro",
        "SGT-AL10": "Mate 80 Pro Max",
        "CLS-AL00": "Mate 70", "PLR-AL00": "Mate 70 Pro", "PLA-AL10": "Mate 70 Pro+",
        "PLU-AL10": "Mate 70 RS", "SUP-AL90": "Mate 70 Air",
        "BRA-AL00": "Mate 60", "ALN-AL00": "Mate 60 Pro", "ALN-AL80": "Mate 60 Pro",
        "ALN-AL10": "Mate 60 Pro+",
        "CET-AL00": "Mate 50", "DCO-AL00": "Mate 50 Pro",
        "KEE-AL00": "Pura 90", "MLN-AL00": "Pura 90 Pro", "SCA-AL00": "Pura 90 Pro Max",
        "HED-AL00": "Pura 80", "LMR-AL00": "Pura 80 Pro", "LMR-AL10": "Pura 80 Pro+",
        "LMU-AL10": "Pura 80 Ultra",
        "ADY-AL00": "Pura 70", "HBN-AL00": "Pura 70 Pro", "HBN-AL10": "Pura 70 Pro+",
        "LNA-AL00": "P60", "MNA-AL00": "P60 Pro",
        "DEL-AL10": "Mate X7", "ICL-AL10": "Mate X6", "ALT-AL10": "Mate X5",
        "GRL-AL10": "Mate XT", "LAP-AL10": "Mate XT 2",
    },
    "oneplus": {
        "PLK110": "15", "PJZ110": "13", "PKX110": "13T", "PJD110": "12", "PHB110": "11",
        "PJE110": "Ace 3", "PHK110": "Ace 2", "PKG110": "Ace 5",
        "CPH2747": "15", "CPH2649": "13", "CPH2653": "13", "CPH2655": "13",
        "CPH2723": "13T", "CPH2573": "12", "CPH2581": "12", "CPH2583": "12",
        "CPH2447": "11", "CPH2449": "11", "CPH2451": "11",
        "CPH2585": "Ace 3", "CPH2609": "Ace 3", "CPH2611": "Ace 3", "CPH2487": "Ace 2",
    },
    "vivo": {
        # X 系列
        "V2001A": "X50", "V2005A": "X50 Pro", "V2011A": "X50 Pro+",
        "V2046A": "X60", "V2047A": "X60 Pro", "V2056A": "X60 Pro+",
        "V2133A": "X70", "V2134A": "X70 Pro", "V2145A": "X70 Pro+",
        "V2183A": "X80", "V2185A": "X80 Pro",
        "V2178A": "X Fold", "V2170A": "X Note",
        "V2241A": "X90", "V2241HA": "X90s", "V2242A": "X90 Pro", "V2227A": "X90 Pro+",
        "V2266A": "X Fold2", "V2256A": "X Flip",
        "V2309A": "X100", "V2324A": "X100 Pro", "V2359A": "X100s",
        "V2324HA": "X100s Pro", "V2366GA": "X100 Ultra",
        "V2303A": "X Fold3", "V2337A": "X Fold3 Pro",
        "V2415A": "X200", "V2405A": "X200 Pro", "V2405DA": "X200 Pro 卫星版",
        "V2419A": "X200 Pro mini", "V2458A": "X200s", "V2454A": "X200 Ultra",
        "V2509A": "X300", "V2502A": "X300 Pro", "V2548A": "X300s", "V2547A": "X300 Ultra",
        # S 系列
        "V2162A": "S12", "V2163A": "S12 Pro", "V2203A": "S15", "V2207A": "S15 Pro",
        "V2244A": "S16", "V2245A": "S16 Pro", "V2283A": "S17", "V2284A": "S17 Pro",
        "V2323A": "S18", "V2344A": "S18 Pro", "V2364A": "S19", "V2362A": "S19 Pro",
        "V2429A": "S20", "V2430A": "S20 Pro",
        "V2464A": "S30", "V2465A": "S30 Pro mini",
        # Y 系列（主流）
        "V2318A": "Y36", "V2357A": "Y37", "V2313A": "Y100", "V2343A": "Y200",
        "V2410A": "Y300 Pro", "V2435A": "Y300", "V2506A": "Y500",
        # iQOO 数字系列（Make 常写作 vivo，故归入本组）
        "V2171A": "iQOO 9", "V2172A": "iQOO 9 Pro",
        "V2217A": "iQOO 10", "V2218A": "iQOO 10 Pro",
        "V2243A": "iQOO 11", "V2254A": "iQOO 11 Pro", "V2304A": "iQOO 11S",
        "V2307A": "iQOO 12", "V2329A": "iQOO 12 Pro",
        "V2408A": "iQOO 13", "V2505A": "iQOO 15",
        # iQOO Neo
        "V2301A": "iQOO Neo8", "V2302A": "iQOO Neo8 Pro",
        "V2338A": "iQOO Neo9", "V2339A": "iQOO Neo9 Pro", "V2403A": "iQOO Neo9S Pro+",
        "V2425A": "iQOO Neo10", "V2426A": "iQOO Neo10 Pro", "V2463A": "iQOO Neo10 Pro+",
        "V2520A": "iQOO Neo11",
        # iQOO Z
        "V2312A": "iQOO Z8x", "V2314A": "iQOO Z8",
        "V2353A": "iQOO Z9x", "V2361A": "iQOO Z9",
        "V2352A": "iQOO Z9 Turbo", "V2417A": "iQOO Z9 Turbo+",
        "V2452A": "iQOO Z10 Turbo", "V2453A": "iQOO Z10 Turbo Pro",
        "V2507A": "iQOO Z10 Turbo+",
    },
    "xiaomi": {
        "23127PN0CC": "14", "23116PN5BC": "14 Pro", "24030PN60G": "14 Ultra",
        "15": "15", "15 Pro": "15 Pro", "15 Ultra": "15 Ultra",
        "Redmi K70": "Redmi K70", "Redmi K70 Pro": "Redmi K70 Pro", "Redmi K70 Ultra": "Redmi K70 Ultra",
        "Redmi K80": "Redmi K80", "Redmi K80 Pro": "Redmi K80 Pro",
        "Redmi Note 13": "Redmi Note 13", "Redmi Note 14": "Redmi Note 14",
    },
    # OPPO（入网型号码来源：OPPO 开发者平台「机型 ColorOS 版本查询」官方表）
    "oppo": {
        "PKB110": "Find X8", "PKC110": "Find X8 Pro", "PKC130": "Find X8 Pro 卫星版",
        "PKJ110": "Find X8 Ultra", "PKT110": "Find X8s", "PLB110": "Find X8s+",
        "PHZ110": "Find X7", "PHY110": "Find X7 Ultra", "PHY120": "Find X7 Ultra 卫星版",
        "PGFM10": "Find X6", "PGEM10": "Find X6 Pro",
        "PFFM10": "Find X5", "PFEM10": "Find X5 Pro",
        "PLP110": "Find N6", "PKH110": "Find N5", "PHN110": "Find N3",
        "PGU110": "Find N2", "PGT110": "Find N2 Flip",
        "PMM110": "Reno 16", "PMK110": "Reno 16 Pro",
        "PLW110": "Reno 15", "PLV110": "Reno 15 Pro",
        "PKM110": "Reno 13", "PKK110": "Reno 13 Pro",
        "PJV110": "Reno 12", "PJW110": "Reno 12 Pro",
        "PJH110": "Reno11", "PJJ110": "Reno11 Pro",
        "PHW110": "Reno10", "PHV110": "Reno10 Pro", "PHU110": "Reno10 Pro+",
        "PHM110": "Reno9", "PGX110": "Reno9 Pro", "PGW110": "Reno9 Pro+",
        "PGBM10": "Reno8", "PGAM10": "Reno8 Pro", "PFZM10": "Reno8 Pro+",
    },
    # 荣耀（Magic 系列入网型号码来源：荣耀开发者平台/MobileModels；
    # 数字与 X 系列用机型名做 key，覆盖 EXIF 直写机型名的情形）
    "honor": {
        "BKQ-AN00": "Magic8", "BKQ-AN10": "Magic8 Pro", "LDY-AN00": "Magic8 Pro Air",
        "PTP-AN00": "Magic7", "PTP-AN10": "Magic7 Pro", "PTP-AN20": "Magic7 RSR",
        "BVL-AN00": "Magic6", "BVL-AN16": "Magic6 Pro", "BVL-AN20": "Magic6 至臻版",
        "PGT-AN00": "Magic5", "PGT-AN10": "Magic5 Pro", "PGT-AN20": "Magic5 至臻版",
        "PNM-AN10": "Magic V6", "MBH-AN10": "Magic V5", "FCP-AN10": "Magic V3",
        "FLC-AN00": "Magic Vs3", "VER-AN10": "Magic V2", "VER-AN00": "Magic Vs2",
        "LRA-AN00": "Magic V Flip", "CLE-AN00": "Magic V Flip2",
        "500": "500", "500 Pro": "500 Pro", "400": "400", "400 Pro": "400 Pro",
        "300": "300", "300 Pro": "300 Pro", "300 Ultra": "300 Ultra",
        "200": "200", "200 Pro": "200 Pro", "100": "100", "100 Pro": "100 Pro",
        "X60": "X60", "X60 Pro": "X60 Pro",
        "X50": "X50", "X50 Pro": "X50 Pro", "X50 GT": "X50 GT",
    },
}
# 品牌别名：Make 字段写子品牌或不同写法时，归一到型号表的品牌键
# （iQOO 是 vivo 子品牌，部分机型 Make 直接写 iQOO，但型号库仍属 vivo 体系）
_BRAND_ALIAS = {"iqoo": "vivo", "redmi": "xiaomi", "poco": "xiaomi"}

# 预排序：长码优先，避免短码误匹配长码
_MODEL_CODES_SORTED = {
    brand: sorted(table.items(), key=lambda kv: -len(kv[0]))
    for brand, table in _MODEL_CODE_MAP.items()
}


def guess_brand_from_code(model):
    """Make 字段缺失时，按型号代号的形态推断品牌。

    手机照片的 Make 可能被系统或修图 App 抹掉，此时仅凭 Model 也能定位品牌：
      SM-S928B → samsung ; ALN-AL00 → huawei ; V2309A → vivo ;
      PJD110 → oneplus ; CPH2581 → oneplus ; 23127PN0CC → xiaomi
    形态无法区分时返回空串（如 P 码同属一加与 OPPO 体系），宁可不猜。
    """
    import re as _re
    up = (model or "").strip().upper()
    if not up:
        return ""
    if up.startswith("SM-"):
        return "samsung"
    # 华为型号后缀为 AL/TL（如 BRA-AL00、ALN-AL80）；荣耀用 AN/AB，不能混为一谈
    if _re.match(r"^[A-Z]{3}-(AL|TL)\d{2}$", up):
        return "huawei"
    if _re.match(r"^V\d{4}[A-Z]{1,3}$", up):            # V2309A / V2227A
        return "vivo"
    if _re.match(r"^CPH\d{4}$", up):                    # CPH2581
        return "oneplus"
    if _re.match(r"^P[A-Z]{2}\d{3}$", up):              # PJD110 / PHK110
        return "oneplus"
    if _re.match(r"^\d{4,5}P[A-Z0-9]{4,6}$", up):        # 23127PN0CC
        return "xiaomi"
    return ""


def resolve_model_code(model, make=""):
    """把厂商内部型号代号换成通俗机型名（受品牌约束）；未收录的保持原样。"""
    raw = (model or "").strip()
    if not raw:
        return raw
    up = raw.upper()
    brand = (make or "").strip().lower()
    if not brand:                       # Make 缺失时按代号形态推断
        brand = guess_brand_from_code(raw)
    for alias, target in _BRAND_ALIAS.items():
        if alias in brand:
            brand = target
            break
    for b, pairs in _MODEL_CODES_SORTED.items():
        if b not in brand:          # 品牌不符不匹配，防止跨品牌型号撞车
            continue
        for code, name in pairs:
            if up == code.upper() or up.startswith(code.upper()):
                return name
    return raw


# ---------------- 品牌 Logo ----------------

_LOGO_MAP = [
    ("dji", "DJI.jpg"), ("iphone", "apple.png"), ("apple", "apple.png"),
    ("canon", "canon.png"), ("fujifilm", "fujifilm.png"), ("fuji", "fujifilm.png"),
    ("hasselblad", "hasselblad.png"), ("leica", "leica_logo.png"),
    ("nikon", "nikon.png"), ("olympus", "olympus_blue_gold.png"),
    ("om system", "olympus_blue_gold.png"), ("panasonic", "panasonic.png"),
    ("lumix", "panasonic.png"), ("pentax", "pentax.png"), ("ricoh", "ricoh.png"),
    ("sony", "sony.png"),
    # ---- 手机品牌（simple-icons 单色重绘版，视觉重量系数见 _LOGO_COEF）----
    ("xiaomi", "xiaomi.png"), ("redmi", "xiaomi.png"), ("poco", "xiaomi.png"),
    ("oppo", "oppo.png"), ("vivo", "vivo.png"), ("iqoo", "vivo.png"),
    ("samsung", "samsung.png"), ("oneplus", "oneplus.png"),
    ("google", "google.png"), ("pixel", "google.png"), ("meizu", "meizu.png"),
    ("honor", "honor.png"), ("huawei", "huawei.png"),
    ("motorola", "motorola.png"), ("moto", "motorola.png"),
]

# 各 Logo 的视觉重量系数：宽字标需放大、实心图形需缩小，否则同一行里大小失衡。
# 参考值来自实测墨迹覆盖率（小米 73.7% / OPPO 8.5% 等）。
_LOGO_COEF = {
    "xiaomi.png": 0.78, "motorola.png": 0.80, "google.png": 0.82,
    "oneplus.png": 0.88, "huawei.png": 0.95, "meizu.png": 1.15,
    "honor.png": 1.28, "vivo.png": 1.34, "oppo.png": 1.42, "samsung.png": 1.70,
}

# 机型名去品牌前缀时使用的关键词（去重后中间的品牌标才不出现重复）
# 注意：不含 "iqoo" —— iQOO 是 vivo 的产品线名，保留在机型名里（"iQOO 12"）更清晰
_BRAND_WORDS = [
    "xiaomi", "redmi", "poco", "oppo", "vivo", "samsung", "oneplus",
    "google", "pixel", "meizu", "honor", "huawei", "motorola", "moto",
]


def load_brand_logo(make, model=""):
    """按品牌名匹配 Logo 图片, 未识别品牌用 default, 无品牌返回 None。

    Make 字段缺失时（部分系统/修图 App 会抹掉），按型号代号形态推断品牌，
    避免"型号能识别但 Logo 画不出来"。
    """
    m = (make or "").strip().lower()
    if not m and model:
        m = guess_brand_from_code(model)
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
        logo = logo.convert("RGBA")
        logo.wm_logo_name = name     # 供渲染层查询视觉重量系数
        return logo
    except Exception:
        return None


def _strip_brand_from_model(model, make):
    """机型名以品牌词开头时去掉该词（品牌已由中间的 Logo 表达，避免重复）。

    仅当去掉后剩余长度 >= 2 才生效（如 "S19"、"Y36"、"14" 这类短型号名配合 Logo
    已经足够清晰）；只剩单个字符时保留原名。
    """
    if not model:
        return model
    low = model.lower().strip()
    for w in _BRAND_WORDS:
        if low.startswith(w):
            rest = model[len(w):].lstrip(" -_·")
            return rest if len(rest) >= 2 else model
    return model


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
    "oppo": (4, 106, 56), "xiaomi": (255, 105, 0),
}


def _accent_color(make):
    m = (make or "").lower()
    for k, c in _BRAND_ACCENT.items():
        if k in m:
            return c
    return (216, 216, 216)


def _model_text(exif):
    _custom = str(exif.get("_custom_model") or "").strip()
    if _custom:
        return _custom
    make = _nice_brand(_clean_make(exif.get("make", "")))
    model = resolve_model_code(exif.get("model") or "", exif.get("make") or "")
    model = _nice_brand(model.strip())
    if make and model.upper().startswith(make.upper()):
        model = model[len(make):].strip()
    return " ".join(x for x in (make, model) if x)


# ---------------- 基础模板(覆盖式水印) ----------------

def _render_normal(overlay, exif, opt):
    """极简右下角: 一行小字, 无底色, 白字细描边, 最不打扰画面。"""
    W, H = overlay.size
    d = ImageDraw.Draw(overlay)
    segs = []
    if opt.get("show_model", True):
        m = _model_text(exif)
        if m:
            segs.append(m)
    if opt.get("show_params", True) and exif.get("params"):
        segs.append(exif["params"])
    if opt.get("show_date", True) and exif.get("date"):
        segs.append(exif["date"])
    txt = "   ·   ".join(segs)
    if not txt:
        txt = (opt.get("custom_text") or "").strip()
    if not txt:
        return

    scale = float(opt.get("font_scale", 1.0))
    short = min(W, H)
    size = max(15, int(short * 0.021 * scale))
    font = _load_font(size, weight="med")
    bbox = d.textbbox((0, 0), txt, font=font)
    tw = bbox[2] - bbox[0]
    pad = int(short * 0.03)
    x = W - pad - tw
    y = H - pad - bbox[3]
    # 描边要够厚，否则在杂乱背景（树叶、石阶）上会糊掉
    d.text((x, y), txt, font=font, fill=(255, 255, 255, 240),
           stroke_width=max(1, size // 7), stroke_fill=(0, 0, 0, 165))


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
    ts = float(cfg.get("text_scale", 1.0))   # 字号系数: 社交卡片等"海报型"布局可放大
    t_norm_base = int(h * 0.3 * ts)
    t_bold_base = int(h * 0.3 * 1.13 * ts)
    pos = str(opt.get("logo_position", "right")).lower()

    model = _model_text(exif).replace("_", "") if opt.get("show_model", True) else ""
    # 机型名去品牌前缀（品牌交给中间 Logo 表达）；去不掉时（如 "OnePlus 12" 只剩 "12"）
    # 说明品牌仍在机型名里，此时隐藏中间 Logo 以免重复
    _brand_in_model = False
    if model:
        _stripped = _strip_brand_from_model(model, exif.get("make", ""))
        if _stripped == model:
            _low = model.lower().strip()
            _brand_in_model = any(_low.startswith(w) for w in _BRAND_WORDS)
        model = _stripped
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
        m["lg"] = (_fit_height(logo, m["elem_h"])
                   if (logo is not None and not _brand_in_model) else None)
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


    # 文字墨迹盒(实际字形范围): Logo 与分隔线按墨迹居中, 而非按行盒居中,
    # 否则 Logo 上边缘高于文字、视觉上呈"上对齐"
    ink_top, ink_bottom = None, None
    for txt, fnt, yy in ((model, f_model, lt_y), (lens, f_norm, lb_y),
                         (params, f_param, rt_y), (date, f_norm, rb_y)):
        if not txt:
            continue
        bb = d.textbbox((0, yy), txt, font=fnt)
        ink_top = bb[1] if ink_top is None else min(ink_top, bb[1])
        ink_bottom = bb[3] if ink_bottom is None else max(ink_bottom, bb[3])
    if ink_top is None:
        ink_top, ink_bottom = lt_y, lt_y + elem_h
    ink_center = (ink_top + ink_bottom) / 2

    # 左块 + 左侧 Logo
    lg = m["lg"]
    ink_h = ink_bottom - ink_top
    if lg is not None:
        # 视觉对齐: 高度 = 墨迹高 × 1.1 × 品牌系数（抵消各 Logo 墨迹覆盖率差异），
        # 并与墨迹中心对齐（不按行盒，否则视觉偏上）
        _coef = _LOGO_COEF.get(getattr(logo, "wm_logo_name", ""), 1.0)
        target_h = max(8, min(h, round(ink_h * 1.1 * _coef)))
        if lg.height != target_h:
            lg = _fit_height(logo, target_h)
    def _logo_y(obj):
        yy = round(ink_center - obj.height / 2)
        return max(y0, min(yy, y1 - obj.height))
    lx = x0 + cfg["pad_x"] + common_spacing
    if lg is not None and pos == "left":
        canvas.alpha_composite(lg, (lx, _logo_y(lg)))
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
            dh = int(max(lg.height * 1.1, ink_h * 1.05, 8))
            d.rectangle([div_x, int(ink_center - dh / 2),
                         div_x + div_w, int(ink_center + dh / 2)],
                        fill=(216, 216, 214))
        min_h = max(8, int(h * 0.08))
        if pos == "right":
            logo_x = div_x - common_spacing - lg.width
            while logo_x < left_end + common_spacing and lg.height > min_h:
                lg = _fit_height(logo, max(min_h, int(lg.height * 0.85)))
                logo_x = div_x - common_spacing - lg.width
            if logo_x >= left_end + common_spacing:
                canvas.alpha_composite(lg, (logo_x, _logo_y(lg)))
        else:  # center
            logo_x = (x0 + x1 - lg.width) // 2
            while (not (logo_x > left_end + common_spacing
                        and logo_x + lg.width < div_x - common_spacing)
                   and lg.height > min_h):
                lg = _fit_height(logo, max(min_h, int(lg.height * 0.85)))
                logo_x = (x0 + x1 - lg.width) // 2
            if (logo_x > left_end + common_spacing
                    and logo_x + lg.width < div_x - common_spacing):
                canvas.alpha_composite(lg, (logo_x, _logo_y(lg)))


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
    gap = sh_r                           # 照片与信息条之间只留阴影衰减所需的空间

    cw = W + 2 * pad + 2 * m
    ch = m + pad + H + gap + bar_h
    canvas = Image.new("RGBA", (cw, ch), (255, 255, 255, 255))

    px, py = m + pad, m + pad
    ph = _rounded(img, radius)
    _glow_shadow(canvas, ph, (px, py), radius=sh_r, alpha=180)

    footer_y = py + H + gap
    _draw_info_block(
        canvas, (0, footer_y, cw, footer_y + bar_h), exif, opt, logo,
        {"pad_x": m, "pad_r": m,
         "divider": False, "right_align_left": False, "text_scale": 1.18})
    return canvas.convert("RGB")


def _render_blur(img, exif, opt):
    """模糊背景: 照片居中加圆角阴影, 下方白字信息, 四周是自身的模糊放大。

    留白由 margin_scale 控制(与社交卡片/品牌留白共用同一滑杆):
      0   → 照片占画布宽 96%, 几乎满幅
      1.0 → 82%, 照片占画布高约 71% (实测选定值)
      3.0 → 54%, 留白充裕
    """
    W, H = img.size
    cw = W

    ms = min(3.0, max(0.0, float(opt.get("margin_scale", 1.0))))
    pw_ratio = min(0.96, max(0.24, 0.96 - 0.14 * ms))
    pw = int(W * pw_ratio)
    ph = int(pw * H / W)

    model = _model_text(exif) if opt.get("show_model", True) else ""
    params = exif.get("params", "") if opt.get("show_params", True) else ""
    f_model = _load_font(int(cw * 0.019), weight="bold")
    f_param = _load_font(int(cw * 0.015), weight="light")
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))   # 仅用于文字测量
    hm = _measure(probe, model or "Ag", f_model)[1] if model else 0
    hp = _measure(probe, params or "Ag", f_param)[1] if params else 0
    pad_text = int(cw * 0.024)
    text_h = hm + (pad_text + hp if params else 0)
    content_h = ph + pad_text + text_h

    # 画布高度由「目标照片高占比」反推: 占比 = ph / ch。
    # 宽占比减 0.11 是被上下留白与文字区吃掉的部分, ms=1 时即 82% - 11% = 71%。
    ratio = min(0.94, max(0.20, pw_ratio - 0.11))
    ch = max(int(ph / ratio), content_h + 2 * int(cw * 0.03))

    # 背景: 缩小→模糊→拉伸 (视觉等效大半径模糊, 快一个数量级)
    small_w = max(2, cw // 8)
    small_h = max(2, int(small_w * ch / cw))
    bg = _cover(img, small_w, small_h)
    bg = bg.filter(ImageFilter.GaussianBlur(3))
    bg = bg.resize((cw, ch), Image.BICUBIC)
    bg = ImageEnhance.Brightness(bg).enhance(0.72)
    canvas = bg.convert("RGBA")

    d = ImageDraw.Draw(canvas)
    px = (cw - pw) // 2
    py = max(int(cw * 0.02), (ch - content_h) // 2)
    photo = img.resize((pw, ph), Image.BICUBIC)  # 照片须缩放到画布 82% 宽, 否则超出画布被裁
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

def _render_film(img, exif, opt):
    """胶片边框: 上下黑色片基 + 齿孔, 模拟 35mm 底片一格。

    不发片边印字: 片基宽度只够容纳齿孔, 真实胶片的边缘编码印在齿孔之间的
    空隙里, 我们的字号塞不进去, 强行叠加会与齿孔重叠。需要日期可用「极简右下角」。
    """
    W, H = img.size

    film_h = max(16, int(H * 0.11))        # 上下片基高度
    side = max(6, int(W * 0.018))          # 左右窄边
    CW, CH = W + side * 2, H + film_h * 2

    canvas = Image.new("RGB", (CW, CH), (22, 22, 24))
    canvas.paste(img.convert("RGB"), (side, film_h))
    d = ImageDraw.Draw(canvas)

    # 齿孔：圆角矩形，上下各一排，垂直居中于片基
    hole_gap = max(20.0, W * 0.13)
    hole_w = hole_gap * 0.52
    hole_h = film_h * 0.46
    n = max(2, int(round(CW / hole_gap)))
    step = CW / n
    rad = hole_h * 0.30
    for i in range(n):
        cx = step * (i + 0.5)
        for cy in (film_h / 2.0, CH - film_h / 2.0):
            d.rounded_rectangle(
                [cx - hole_w / 2, cy - hole_h / 2, cx + hole_w / 2, cy + hole_h / 2],
                radius=rad, fill=(236, 236, 232))

    return canvas


def _render_polaroid(img, exif, opt):
    """拍立得: 照片缩进白色相纸, 上左右窄边, 底部宽边留作标注。

    比例参照宝丽来相纸(画面 79mm, 左右上 5mm, 底 22mm), 但保留原图比例:
    左右上 = 照片宽 4.5%, 底部标注区 = 短边 20%(相纸底部与边框约 4.4:1)。
    不做圆角与阴影——相纸是平贴的, 加阴影反而失真。
    """
    W, H = img.size
    border = max(6, int(W * 0.045))
    # 标注区高度按短边算: 横幅照片若按宽度算, 底部会空出近 20% 的高
    note_h = max(26, int(min(W, H) * 0.20))
    cw, ch = W + border * 2, border + H + note_h

    canvas = Image.new("RGB", (cw, ch), (255, 255, 255))
    canvas.paste(img.convert("RGB"), (border, border))
    d = ImageDraw.Draw(canvas)

    # 型号里的下划线与信息条口径一致: "Z5_2" -> "Z52"
    model = (_model_text(exif) or "").replace("_", "").strip() \
        if opt.get("show_model", True) else ""
    params = (exif.get("params") or "") if opt.get("show_params", True) else ""
    date = (exif.get("date") or "") if opt.get("show_date", True) else ""
    info = "   ·   ".join(x for x in (params, date) if x)
    if not model and not info:
        return canvas

    # 机型粗黑一行, 参数与日期细灰一行, 两行在标注区内垂直居中。
    # 字号按标注区高推导, 字高约占 21%(真实拍立得手写约 23%)。
    # 超过相纸宽 84% 时整组等比缩小, 避免竖幅长机型名溢出。
    inner = int(cw * 0.84)
    m_size = max(10, int(note_h * 0.27))
    i_size = max(9, int(note_h * 0.19))
    f_m = _load_font(m_size, weight="bold")
    f_i = _load_font(i_size, weight="light")
    for _ in range(24):
        mw = _measure(d, model, f_m)[0] if model else 0
        iw = _measure(d, info, f_i)[0] if info else 0
        if max(mw, iw) <= inner or m_size <= 11:
            break
        m_size = max(10, int(m_size * 0.94))
        i_size = max(9, int(i_size * 0.94))
        f_m = _load_font(m_size, weight="bold")
        f_i = _load_font(i_size, weight="light")

    mh = _measure(d, model or "Ag", f_m)[1] if model else 0
    ih = _measure(d, info or "Ag", f_i)[1] if info else 0
    gap = int(note_h * 0.06) if (model and info) else 0
    ty = border + H + max(2, (note_h - (mh + gap + ih)) // 2)

    if model:
        d.text(((cw - _measure(d, model, f_m)[0]) // 2, ty),
               model, font=f_m, fill=(0, 0, 0))
        ty += mh + gap
    if info:
        d.text(((cw - _measure(d, info, f_i)[0]) // 2, ty),
               info, font=f_i, fill=(74, 74, 74))
    return canvas


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

_OVERLAY_TPL = ("custom", "normal")


def apply_watermark(img, exif, settings):
    """按 settings 渲染水印, 返回新图(不修改原图)。"""
    tpl = settings.get("template", "standard1")

    # 手填机型：EXIF 里没有品牌/型号时（微信转码、修图软件会剥离）由用户补填，
    # 同时用于 Logo 匹配（如手填 "vivo X100 Pro" 能匹配到 vivo 标）
    _custom = str(settings.get("custom_model") or "").strip()
    if _custom:
        exif = dict(exif)
        exif["_custom_model"] = _custom

    if tpl in _OVERLAY_TPL:
        rgb = img.convert("RGB")
        overlay = Image.new("RGBA", rgb.size, (0, 0, 0, 0))
        if tpl == "custom":
            _render_corner(overlay, exif, settings, custom=True)
        elif tpl == "normal":
            _render_normal(overlay, exif, settings)
        return Image.alpha_composite(rgb.convert("RGBA"), overlay).convert("RGB")

    logo = settings.get("_logo")
    if logo is None:
        logo = load_brand_logo(exif.get("_custom_model") or exif.get("make", ""), exif.get("model", ""))
    if tpl == "standard1":
        return _render_standard1(img, exif, settings, logo)
    if tpl == "standard2":
        return _render_standard2(img, exif, settings, logo)
    if tpl == "blur":
        return _render_blur(img, exif, settings)
    if tpl == "film":
        return _render_film(img, exif, settings)
    if tpl == "polaroid":
        return _render_polaroid(img, exif, settings)
    if tpl == "center_logo":
        return _render_center_logo(img, exif, settings, logo)
    return img.convert("RGB")
