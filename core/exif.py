# core/exif.py (신규)
from PIL import Image, ExifTags
from datetime import datetime

def _dms_to_deg(dms, ref):
    deg = dms[0][0] / dms[0][1]
    minutes = dms[1][0] / dms[1][1]
    seconds = dms[2][0] / dms[2][1]
    sign = -1 if ref in ("S","W") else 1
    return sign * (deg + minutes/60 + seconds/3600)

def extract_exif_basic(img: Image.Image):
    exif_raw = getattr(img, "_getexif", lambda: None)() or {}
    exif = {ExifTags.TAGS.get(k,k): v for k,v in exif_raw.items()}
    # 시간
    taken_at = None
    for key in ("DateTimeOriginal","DateTimeDigitized","DateTime"):
        if exif.get(key):
            try:
                taken_at = datetime.strptime(exif[key], "%Y:%m:%d %H:%M:%S")
                break
            except: pass
    # GPS
    lon=lat=None
    gps_info = exif.get("GPSInfo")
    if isinstance(gps_info, dict):
        gps = {ExifTags.GPSTAGS.get(k,k): v for k,v in gps_info.items()}
        if all(k in gps for k in ("GPSLatitude","GPSLatitudeRef","GPSLongitude","GPSLongitudeRef")):
            lat = _dms_to_deg(gps["GPSLatitude"], gps["GPSLatitudeRef"])
            lon = _dms_to_deg(gps["GPSLongitude"], gps["GPSLongitudeRef"])
    return lon, lat, taken_at
