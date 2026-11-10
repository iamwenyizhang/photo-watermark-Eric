# -*- coding: utf-8 -*-
"""照片 EXIF 水印工具 — 本地 Web 服务。

用法: python app.py  (首次运行自动安装依赖, 并打开 http://127.0.0.1:8765)
环境变量: WM_PORT=端口号  WM_NO_BROWSER=1(不自动开浏览器)
"""
import importlib.util
import io
import os
import subprocess
import sys
import threading
import uuid
import webbrowser
import zipfile
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def _ensure_deps():
    need = [m for m in ("flask", "PIL", "piexif")
            if importlib.util.find_spec(m) is None]
    if need:
        print(f"正在安装依赖: {', '.join(need)} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *need])


_ensure_deps()

from flask import Flask, jsonify, request, send_file  # noqa: E402
from PIL import Image, ImageOps                        # noqa: E402
import piexif                                          # noqa: E402

import watermark as wm                                # noqa: E402

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MAX_PREVIEW = 1800
JPEG_QUALITY = 92

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB


def _find_upload(fid):
    for ext in ALLOWED_EXT:
        p = os.path.join(UPLOAD_DIR, fid + ext)
        if os.path.exists(p):
            return p
    return None


def _strip_orientation(exif_bytes):
    """输出图已按 EXIF 方向物理转正, 将 Orientation 置 1 避免二次旋转。"""
    if not exif_bytes:
        return None
    try:
        d = piexif.load(exif_bytes)
        if d.get("0th") and 274 in d["0th"]:
            d["0th"][274] = 1
        return piexif.dump(d)
    except Exception:
        return exif_bytes


def _resolve_logo(settings):
    """从 settings.logo_id 加载用户上传的 Logo, 返回 RGBA Image 或 None。"""
    lid = str(settings.get("logo_id") or "").strip()
    if not lid or "/" in lid or "\\" in lid or ".." in lid:
        return None
    p = os.path.join(UPLOAD_DIR, lid)
    if not os.path.exists(p):
        return None
    try:
        im = Image.open(p)
        im.load()
        return im.convert("RGBA")
    except Exception:
        return None


def _prepare(settings):
    settings = dict(settings or {})
    settings["_logo"] = _resolve_logo(settings)
    return settings


@app.get("/")
def index():
    """返回界面页。

    本地工具，禁用缓存：改完 static/index.html 后浏览器刷新即生效，
    不必强制刷新。同时把文件修改时间注入页头，方便确认加载的是哪一版。
    """
    path = os.path.join(STATIC_DIR, "index.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    try:
        stamp = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%m-%d %H:%M")
    except OSError:
        stamp = ""
    html = html.replace("__BUILD__", f"· 页面 {stamp}" if stamp else "")
    resp = app.response_class(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.post("/api/upload")
def upload():
    results = []
    for f in request.files.getlist("files"):
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        fid = uuid.uuid4().hex[:12]
        path = os.path.join(UPLOAD_DIR, fid + ext)
        f.save(path)
        try:
            with Image.open(path) as im:
                exif = wm.parse_exif(im)
                w, h = im.size
            results.append({"id": fid, "name": f.filename,
                            "size": os.path.getsize(path),
                            "width": w, "height": h, "exif": exif})
        except Exception:
            if os.path.exists(path):
                os.remove(path)
    return jsonify(results)


@app.post("/api/upload-logo")
def upload_logo():
    f = request.files.get("logo")
    if not f:
        return jsonify({"error": "未收到文件"}), 400
    fid = "logo_" + uuid.uuid4().hex[:10]
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".png"
    f.save(os.path.join(UPLOAD_DIR, fid + ext))
    return jsonify({"logo_id": fid + ext})


@app.post("/api/preview")
def preview():
    data = request.get_json(force=True)
    src = _find_upload(data.get("id", ""))
    if not src:
        return jsonify({"error": "文件不存在"}), 404
    settings = _prepare(data.get("settings", {}))
    with Image.open(src) as im:
        exif = wm.parse_exif(im)
        im = ImageOps.exif_transpose(im)
    if max(im.size) > MAX_PREVIEW:
        im.thumbnail((MAX_PREVIEW, MAX_PREVIEW), Image.LANCZOS)
    out = wm.apply_watermark(im, exif, settings)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=88)
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg")


@app.post("/api/process")
def process():
    data = request.get_json(force=True)
    settings = _prepare(data.get("settings", {}))
    done, failed = [], []

    for item in data.get("items", []):
        fid = item.get("id", "")
        src = _find_upload(fid)
        if not src:
            failed.append(item.get("name", fid))
            continue
        try:
            stem = os.path.splitext(item.get("name", fid))[0] or fid
            out_name = f"{stem}_水印.jpg"
            k = 1
            while os.path.exists(os.path.join(OUTPUT_DIR, out_name)):
                k += 1
                out_name = f"{stem}_水印_{k}.jpg"

            with Image.open(src) as im:
                exif = wm.parse_exif(im)
                exif_bytes = im.info.get("exif")
                im2 = ImageOps.exif_transpose(im)
            result = wm.apply_watermark(im2, exif, settings)

            out_path = os.path.join(OUTPUT_DIR, out_name)
            exif_out = _strip_orientation(exif_bytes)
            if exif_out:
                result.save(out_path, "JPEG", quality=JPEG_QUALITY, exif=exif_out)
            else:
                result.save(out_path, "JPEG", quality=JPEG_QUALITY)
            done.append(out_name)
        except Exception as e:
            failed.append(f"{item.get('name', fid)} ({e})")

    zip_name = None
    if done:
        zip_name = f"photos_watermark_{datetime.now():%Y%m%d_%H%M%S}.zip"
        with zipfile.ZipFile(os.path.join(OUTPUT_DIR, zip_name), "w",
                             zipfile.ZIP_DEFLATED) as zf:
            for n in done:
                zf.write(os.path.join(OUTPUT_DIR, n), n)

    return jsonify({"count": len(done), "failed": failed, "files": done,
                    "zip": zip_name, "output_dir": OUTPUT_DIR})


@app.get("/api/download/<name>")
def download(name):
    path = os.path.join(OUTPUT_DIR, os.path.basename(name))
    if not os.path.exists(path):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(path, as_attachment=True)


@app.post("/api/open-folder")
def open_folder():
    try:
        os.startfile(OUTPUT_DIR)  # Windows
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    PORT = int(os.environ.get("WM_PORT", "8765"))
    if os.environ.get("WM_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(
            f"http://127.0.0.1:{PORT}")).start()
    print(f"* 水印工具已启动: http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
