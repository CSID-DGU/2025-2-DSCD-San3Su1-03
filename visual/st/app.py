import streamlit as st
import pandas as pd
import folium
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium
from datetime import datetime
from PIL import Image, ImageOps
import os, io, base64


# -----------------------------
# 경로 설정
# -----------------------------
CSV_PATH  = "/data1/project/yeonu/capstone/data/photo_meta_addr_pretty.csv"
PHOTO_DIR = "/data1/project/yeonu/capstone/data/photo"


# -----------------------------
# 이미지 유틸
# -----------------------------
def find_image_path(row):
    p = row.get("SourceFile")
    if isinstance(p, str) and os.path.exists(p):
        return p
    d, f = row.get("Directory"), row.get("FileName")
    if isinstance(d, str) and isinstance(f, str):
        p2 = os.path.join(d, f)
        if os.path.exists(p2):
            return p2
    if isinstance(f, str):
        p3 = os.path.join(PHOTO_DIR, f)
        if os.path.exists(p3):
            return p3
    return None


def img_to_data_uri(path, max_w=280):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    img.thumbnail((max_w, 10000), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


# -----------------------------
# 데이터 로드
# -----------------------------
df = pd.read_csv(CSV_PATH)
df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")

# 시간 파싱
src_cols = ["CapturedTime", "DateTimeOriginal", "CreateDate", "ModifyDate"]
src_cols = [c for c in src_cols if c in df.columns]

for c in src_cols:
    df["time"] = pd.to_datetime(df[c].astype(str).str.replace(
        r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", regex=True
    ), errors="coerce")
    if df["time"].notna().any():
        break

df = df.dropna(subset=["Latitude", "Longitude", "time"])
df = df.sort_values("time").reset_index(drop=True)


# -----------------------------
# Streamlit UI
# -----------------------------
st.title("📍 여행 경로 / 사진 지도 시각화")

# Zoom 설정 slider (원하면)
zoom_start = st.slider("초기 지도 확대 수준", 10, 18, 13)


# -----------------------------
# Folium 지도 생성
# -----------------------------
center = [df["Latitude"].mean(), df["Longitude"].mean()]
m = folium.Map(location=center, zoom_start=zoom_start)

# 경로 라인
coords = df[["Latitude", "Longitude"]].to_numpy().tolist()
folium.PolyLine(coords, color="blue", weight=3, opacity=0.6).add_to(m)

# 가까운 점만 클러스터링
cluster = MarkerCluster(
    options={
        "maxClusterRadius": 20,
        "spiderfyOnMaxZoom": True,
        "disableClusteringAtZoom": 18
    }
).add_to(m)

# 마커 추가
N = len(df)
for i, r in df.iterrows():
    img_path = find_image_path(r)
    if img_path:
        uri = img_to_data_uri(img_path)
        img_html = f'<img src="{uri}" style="width:280px; height:auto;"><br>'
    else:
        img_html = ""

    html = (
        f"{img_html}"
        f"{r['time']:%Y-%m-%d %H:%M}<br>"
        f"{r.get('addr_best', r.get('addr_pretty',''))}<br>"
    )

    if i == 0:
        icon = folium.Icon(color="green", icon="play", prefix="fa")
    elif i == N - 1:
        icon = folium.Icon(color="darkred", icon="flag", prefix="fa")
    else:
        icon = folium.Icon(color="red", icon="stop", prefix="fa")

    folium.Marker(
        [r["Latitude"], r["Longitude"]],
        popup=html,
        icon=icon,
        tooltip=f"{r['time']:%Y-%m-%d %H:%M}"
    ).add_to(cluster)


# -----------------------------
# Streamlit에 지도 렌더링
# -----------------------------
st_folium(m, width=900, height=600)
