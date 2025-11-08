# pages/02_Route.py
import os, sys, json, math
import streamlit as st
import pandas as pd
from datetime import datetime
from sqlalchemy import create_engine, text
import folium
from streamlit_folium import st_folium

# ---------- 인증 가드 ----------
auth = st.session_state.get("auth")
if not auth or "user_id" not in auth:
    st.warning("로그인 후에 접근할 수 있어요.")
    st.switch_page("app.py")
    st.stop()

# ---------- 공통 UI ----------
def apply_ui():
    from core.ui import hide_default_nav
    hide_default_nav()
    from app import render_sidebar as _render_sidebar
    _render_sidebar()
apply_ui()

# 모듈 경로/의존
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.db import get_engine, fetch_photos_by_keys, fetch_photos_by_group_prefix
from core.storage import get_storage

st.title("지도 시각화")

user_id = auth["user_id"]
# 세션 키 가져오기
keys = st.session_state.get("selected_image_keys", [])
group_id = st.session_state.get("selected_imgs_group_id")

if not keys and not group_id:
    st.info("먼저 업로드 페이지에서 이미지를 업로드해주세요.")
    # 페이지 이동 버튼 (선택)
    if st.button("➡ 지도 시각화로 이동"):
        st.switch_page("pages/00_Upload.py")
    st.stop()

# ---------- 데이터 로드 ----------
# photos + (LEFT JOIN) photo_addresses
def load_df():
    eng = get_engine()
    if keys:
        rows = fetch_photos_by_keys(user_id, keys)
    elif group_id is not None:
        rows = fetch_photos_by_group_prefix(user_id, int(group_id))
    else:
        return pd.DataFrame(columns=["id","key","bucket","content_type","size","taken_at_utc","lat","lon","addr_json"])

    pids = [r["id"] for r in rows]
    df = pd.DataFrame(rows)

    # JOIN 주소 캐시
    if not df.empty:
        with eng.connect() as conn:
            addr = conn.execute(
                text("SELECT photo_id, addr_json FROM photo_addresses WHERE photo_id = ANY(:ids)"),
                {"ids": pids}
            ).mappings().all()
        adf = pd.DataFrame(addr) if addr else pd.DataFrame(columns=["photo_id","addr_json"])
        df = df.merge(adf, left_on="id", right_on="photo_id", how="left")
        df.drop(columns=["photo_id"], inplace=True, errors="ignore")
        if "addr_json" in df.columns:
            def _fix(v):
                if v is None: return None
                if isinstance(v, float) and math.isnan(v): return None
                return v
            df["addr_json"] = df["addr_json"].map(_fix)

    # 타입 정리
    df["taken_at_utc"] = pd.to_datetime(df["taken_at_utc"], errors="coerce")
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat","lon"])  # 유효 좌표만
    df = df.sort_values(["taken_at_utc","id"], na_position="last").reset_index(drop=True)
    return df

df = load_df()
if df.empty:
    st.info("표시할 좌표가 있는 사진이 없어요. EXIF 위치가 있는 사진으로 올려보세요.")
    st.stop()

# ---------- 시간 범위 필터 ----------
min_dt, max_dt = df["taken_at_utc"].min(), df["taken_at_utc"].max()
if pd.notna(min_dt) and pd.notna(max_dt) and min_dt != max_dt:
    rng = st.slider(
        "시간 범위 선택",
        min_value=min_dt.to_pydatetime(),
        max_value=max_dt.to_pydatetime(),
        value=(min_dt.to_pydatetime(), max_dt.to_pydatetime())
    )
    df_show = df[(df["taken_at_utc"] >= rng[0]) & (df["taken_at_utc"] <= rng[1])].copy()
else:
    df_show = df.copy()

if df_show.empty:
    st.warning("선택한 범위에 표시할 사진이 없습니다.")
    st.stop()

# ---------- 미리보기 URL 준비 ----------
storage = get_storage()
def preview_url(key: str) -> str | None:
    try:
        return storage.url(key)
    except Exception:
        return None

# ---------- 주소 문자열 유틸 ----------
def pretty_addr(addr_json):
    # 결측/NaN 가드
    if addr_json is None:
        return None
    if isinstance(addr_json, float) and math.isnan(addr_json):
        return None

    # bytes → str
    if isinstance(addr_json, (bytes, bytearray)):
        addr_json = addr_json.decode("utf-8", errors="ignore")

    # str → dict
    if isinstance(addr_json, str):
        try:
            addr_json = json.loads(addr_json)
        except Exception:
            return None

    if not isinstance(addr_json, dict):
        return None

    return (addr_json.get("road_address")
            or addr_json.get("address_name")
            or None)

# ---------- 지도의 중심/타일 ----------
center = [float(df_show["lat"].mean()), float(df_show["lon"].mean())]
tiles_opt = st.selectbox("지도 스타일", ["CartoDB dark_matter", "OpenStreetMap", "CartoDB positron"], index=0)
tiles_map = {
    "CartoDB dark_matter": "CartoDB dark_matter",
    "OpenStreetMap": "OpenStreetMap",
    "CartoDB positron": "CartoDB positron",
}
m = folium.Map(location=center, zoom_start=13, tiles=tiles_map[tiles_opt], control_scale=True)

# ---------- 경로 라인 (시간순) ----------
coords = df_show[["lat","lon"]].to_numpy().tolist()
folium.PolyLine(coords, color="blue", weight=3, opacity=0.6).add_to(m)

# ---------- 마커 (시작/중간/종료 아이콘 구분) ----------
N = len(df_show)
for i, r in df_show.reset_index(drop=True).iterrows():
    lat, lon = float(r["lat"]), float(r["lon"])
    t = r["taken_at_utc"]
    addr_txt = pretty_addr(r.get("addr_json"))
    img_url = preview_url(r["key"])

    # 팝업 HTML
    parts = []
    if img_url:
        parts.append(f'<img src="{img_url}" style="max-width:280px;height:auto;display:block;margin-bottom:6px;border-radius:8px;">')
    parts.append(f"<b>{t:%Y-%m-%d %H:%M:%S}</b><br>")
    if addr_txt:
        parts.append(f"{addr_txt}<br>")
    parts.append(f"({lat:.5f}, {lon:.5f})")
    html = "".join(parts)

    # 아이콘
    if i == 0:
        icon = folium.Icon(color="green", icon="play", prefix="fa")    # 시작
        tooltip = f"START • {t:%Y-%m-%d %H:%M}" if pd.notna(t) else "START"
    elif i == N-1:
        icon = folium.Icon(color="darkred", icon="flag", prefix="fa")  # 종료
        tooltip = f"END • {t:%Y-%m-%d %H:%M}" if pd.notna(t) else "END"
    else:
        icon = folium.Icon(color="red", icon="map-marker", prefix="fa")  # 중간 지점 통일
        tooltip = f"{t:%Y-%m-%d %H:%M}" if pd.notna(t) else "waypoint"

    folium.Marker(
        [lat, lon],
        icon=icon,
        popup=folium.Popup(html, max_width=320),
        tooltip=tooltip
    ).add_to(m)

# ---------- 렌더 ----------
st_folium(m, width=None, height=620)
st.caption(f"표시된 사진 수: {len(df_show)} / 전체 유효 좌표 사진 수: {len(df)}")
if st.button("➡ 다른 에피소드 업로드"):
        st.switch_page("pages/00_Upload.py")