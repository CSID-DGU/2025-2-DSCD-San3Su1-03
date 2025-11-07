# pages/02_Route.py
import os, sys
import streamlit as st
import pandas as pd
import pydeck as pdk
from datetime import datetime
from sqlalchemy import create_engine, text

# ---------- 인증 가드 ----------
auth = st.session_state.get("auth")
if not auth or "user_id" not in auth:
    st.warning("로그인 후에 접근할 수 있어요.")
    st.switch_page("app.py")
    st.stop()

def apply_ui():
    from core.ui import hide_default_nav
    hide_default_nav()
    from app import render_sidebar as _render_sidebar
    _render_sidebar()
apply_ui()

st.title("지도 시각화 (마커 & 이동경로)")

user_id = auth["user_id"]

# 세션 키 가져오기
keys = st.session_state.get("selected_image_keys", [])
meta = st.session_state.get("selected_image_meta", [])
group_id = st.session_state.get("selected_imgs_group_id")

if not keys and not group_id:
    st.info("먼저 업로드 페이지에서 이미지를 업로드해주세요.")
    st.stop()

# ---------- DB에서 좌표/시간 불러오기 ----------
# (예) core.db에 get_engine() 있다면 사용. 없으면 secrets/environment에서 구성.
def get_engine():
    # 예시: Streamlit secrets 또는 환경변수 기반
    # st.secrets["postgres"] 사용 예시
    cfg = st.secrets.get("postgres", {})
    user = cfg.get("user")
    password = cfg.get("password")
    host = cfg.get("host")
    port = cfg.get("port", 5432)
    database = cfg.get("database")
    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
    return create_engine(url, pool_pre_ping=True)

engine = get_engine()

# 1) keys가 있으면 keys 기반, 없고 group_id가 있으면 group_id 기반으로 조회
rows = []
with engine.connect() as conn:
    if keys:
        q = text("""
            SELECT key, lon, lat, taken_at_utc
            FROM photos
            WHERE user_id = :uid AND key = ANY(:keys)
        """)
        rows = conn.execute(q, {"uid": str(user_id), "keys": keys}).fetchall()
    elif group_id is not None:
        q = text("""
            SELECT key, lon, lat, taken_at_utc
            FROM photos
            WHERE user_id = :uid AND key LIKE :prefix
        """)
        prefix = f"users/{user_id}/imgs/{int(group_id)}/%"
        rows = conn.execute(q, {"uid": str(user_id), "prefix": prefix}).fetchall()

df = pd.DataFrame(rows, columns=["key","lon","lat","taken_at_utc"]) if rows else pd.DataFrame(columns=["key","lon","lat","taken_at_utc"])

# EXIF 없는 항목 처리
df["taken_at_utc"] = pd.to_datetime(df["taken_at_utc"], errors="coerce")
df_valid = df.dropna(subset=["lon","lat"])  # 경로/마커에 쓸 유효 좌표만
df_valid = df_valid.sort_values("taken_at_utc", na_position="last").reset_index(drop=True)

if df_valid.empty:
    st.warning("유효한 GPS 좌표가 포함된 사진을 찾지 못했어요. 사진의 위치정보(EXIF GPS)가 있는지 확인해주세요.")
    if st.button("➡ 업로드 페이지로 이동"):
        st.switch_page("pages/00_Upload.py")
    st.stop()

# ---------- 시간 필터 ----------
min_dt, max_dt = df_valid["taken_at_utc"].min(), df_valid["taken_at_utc"].max()
if pd.isna(min_dt) or pd.isna(max_dt) or min_dt == max_dt:
    dt_range = None
else:
    dt_range = st.slider(
        "시간 범위 선택",
        min_value=min_dt.to_pydatetime(),
        max_value=max_dt.to_pydatetime(),
        value=(min_dt.to_pydatetime(), max_dt.to_pydatetime())
    )
if dt_range:
    df_show = df_valid[(df_valid["taken_at_utc"]>=dt_range[0]) & (df_valid["taken_at_utc"]<=dt_range[1])].copy()
else:
    df_show = df_valid.copy()

# ---------- pydeck 시각화 ----------
# 마커 레이어
scatter = pdk.Layer(
    "ScatterplotLayer",
    data=df_show.assign(
        tooltip=df_show.apply(
            lambda r: f"{r['taken_at_utc']}<br/>{r['key']}", axis=1
        ),
        coordinates=df_show.apply(lambda r: [r["lon"], r["lat"]], axis=1)
    ),
    get_position="coordinates",
    get_radius=30,
    pickable=True
)

# 경로 레이어 (시간 순 정렬 후 LineString)
if len(df_show) >= 2:
    path_coords = df_show.sort_values("taken_at_utc") \
                         .apply(lambda r: [r["lon"], r["lat"]], axis=1).tolist()
    path_df = pd.DataFrame([{"path": path_coords}])

    line = pdk.Layer(
        "PathLayer",
        data=path_df,
        get_path="path",
        width_scale=2,
        width_min_pixels=3,
        pickable=False
    )
    layers = [scatter, line]
else:
    layers = [scatter]

mid_lon = float(df_show["lon"].mean())
mid_lat = float(df_show["lat"].mean())

view = pdk.ViewState(latitude=mid_lat, longitude=mid_lon, zoom=11)

st.pydeck_chart(pdk.Deck(
    initial_view_state=view,
    layers=layers,
    tooltip={"html": "{tooltip}", "style": {"color": "white"}}
))

st.caption(f"표시된 사진 수: {len(df_show)} / 전체 유효 좌표 사진 수: {len(df_valid)}")
