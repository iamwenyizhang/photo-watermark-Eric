# -*- coding: utf-8 -*-
"""生成一张带 DJI 风格 EXIF 的样例照片, 用于测试水印效果。

用法: python tests/make_sample.py  ->  在 tests/ 下生成 sample_dji.jpg
"""
import os

from PIL import Image, ImageDraw
from fractions import Fraction
import piexif

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    # 模拟天空渐变照片
    W, H = 4000, 3000
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    top, bottom = (135, 196, 235), (60, 90, 140)
    for y in range(H):
        t = y / H
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=c)

    exif = {
        "0th": {
            piexif.ImageIFD.Make: b"DJI",
            piexif.ImageIFD.Model: b"Mavic 3 Pro",
            piexif.ImageIFD.DateTime: b"2026:10:09 10:30:00",
        },
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: b"2026:10:09 10:30:00",
            piexif.ExifIFD.FocalLength: (24, 1),
            piexif.ExifIFD.FNumber: (28, 10),
            piexif.ExifIFD.ExposureTime: (1, 2000),
            piexif.ExifIFD.ISOSpeedRatings: 100,
        },
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: ((22, 1), (32, 1), (18, 1)),
            piexif.GPSIFD.GPSLongitudeRef: b"E",
            piexif.GPSIFD.GPSLongitude: ((114, 1), (3, 1), (22, 1)),
            piexif.GPSIFD.GPSAltitudeRef: 0,
            piexif.GPSIFD.GPSAltitude: (120, 1),
        },
    }
    out = os.path.join(HERE, "sample_dji.jpg")
    img.save(out, "JPEG", quality=90, exif=piexif.dump(exif))
    print("已生成:", out)


if __name__ == "__main__":
    main()
