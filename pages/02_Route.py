# pages/02_Route.py
import os, sys, json, math, time
import streamlit as st
import pandas as pd
from datetime import datetime
from sqlalchemy import text
import folium
from streamlit_folium import st_folium
from folium import Tooltip

# ---------- 0) 인증 가드 ----------
auth = st.session_state.get("auth")
if not auth or "user_id" not in auth:
    st.warning("로그인 후에 접근할 수 있어요.")
    st.switch_page("app.py")
    st.stop()

user_id = auth["user_id"]

# ---------- 1) 공통 UI ----------
def apply_ui():
    from core.ui import hide_default_nav
    hide_default_nav()
    from app import render_sidebar as _render_sidebar
    _render_sidebar()

apply_ui()

# ---------- 2) 모듈 경로/의존 ----------
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.db import get_engine, fetch_photos_by_keys, fetch_photos_by_group_prefix
from core.storage import get_storage

st.title("지도 시각화")

# ---------- 3) 세션의 에피소드/사진 키 확인 ----------
keys = st.session_state.get("selected_image_keys", [])
group_id = st.session_state.get("selected_imgs_group_id")

if not keys and not group_id:
    st.info("먼저 업로드 페이지에서 이미지를 업로드해주세요.")
    time.sleep(1)
    st.switch_page("pages/00_Upload.py")
    st.stop()

# ---------- 4) 데이터 로드 (캐싱) ----------
@st.cache_data(show_spinner=True)
def load_df_cached(user_id: str, keys_tup, group_id_val):
    """
    photos + (LEFT JOIN) photo_addresses 를 읽어와서
    - lat/lon 유효값만 남기고
    - 시간순으로 정렬한 DataFrame 반환
    """
    from core.db import get_engine, fetch_photos_by_keys, fetch_photos_by_group_prefix

    eng = get_engine()

    if keys_tup:
        rows = fetch_photos_by_keys(user_id, list(keys_tup))
    elif group_id_val is not None:
        rows = fetch_photos_by_group_prefix(user_id, int(group_id_val))
    else:
        return pd.DataFrame(columns=[
            "id", "key", "bucket", "content_type", "size",
            "taken_at_utc", "lat", "lon", "addr_json"
        ])

    if not rows:
        return pd.DataFrame(columns=[
            "id", "key", "bucket", "content_type", "size",
            "taken_at_utc", "lat", "lon", "addr_json"
        ])

    pids = [r["id"] for r in rows]
    df = pd.DataFrame(rows)

    # JOIN 주소 캐시
    with eng.connect() as conn:
        addr = conn.execute(
            text("SELECT photo_id, addr_json FROM photo_addresses WHERE photo_id = ANY(:ids)"),
            {"ids": pids}
        ).mappings().all()

    adf = pd.DataFrame(addr) if addr else pd.DataFrame(columns=["photo_id", "addr_json"])
    df = df.merge(adf, left_on="id", right_on="photo_id", how="left")
    df.drop(columns=["photo_id"], inplace=True, errors="ignore")

    # addr_json NaN/None 정리
    if "addr_json" in df.columns:
        def _fix(v):
            if v is None:
                return None
            if isinstance(v, float) and math.isnan(v):
                return None
            return v
        df["addr_json"] = df["addr_json"].map(_fix)

    # 타입 정리
    df["taken_at_utc"] = pd.to_datetime(df["taken_at_utc"], errors="coerce")
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")

    # 유효 좌표만 + 시간순 정렬
    df = df.dropna(subset=["lat", "lon"])
    df = df.sort_values(["taken_at_utc", "id"], na_position="last").reset_index(drop=True)
    return df

# keys 를 튜플로 변환해서 캐시 키로 사용
keys_tup = tuple(keys) if keys else None
df = load_df_cached(user_id, keys_tup, group_id)

if df.empty:
    st.info("표시할 좌표가 있는 사진이 없어요. EXIF 위치가 있는 사진으로 올려보세요.")
    st.stop()

# ---------- 5) 시간 범위 필터 ----------
min_dt, max_dt = df["taken_at_utc"].min(), df["taken_at_utc"].max()

with st.container():
    if pd.notna(min_dt) and pd.notna(max_dt) and min_dt != max_dt:
        rng = st.slider(
            "시간 범위 선택",
            min_value=min_dt.to_pydatetime(),
            max_value=max_dt.to_pydatetime(),
            value=(min_dt.to_pydatetime(), max_dt.to_pydatetime()),
            help="슬라이더를 움직여서 지도에 표시할 사진의 시간 범위를 선택하세요.",
        )
        df_show = df[
            (df["taken_at_utc"] >= rng[0]) &
            (df["taken_at_utc"] <= rng[1])
        ].copy()
    else:
        df_show = df.copy()

if df_show.empty:
    st.warning("선택한 범위에 표시할 사진이 없습니다.")
    st.stop()

# ---------- 6) 미리보기 URL 준비 ----------
storage = get_storage()

def preview_url(key: str) -> str | None:
    try:
        return storage.url(key)
    except Exception:
        return None

# ---------- 7) 주소 문자열 유틸 ----------
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

# ---------- 8) 레이아웃: 왼쪽 필터/정보, 오른쪽 지도 ----------
left, right = st.columns([1, 2])

with left:
    st.subheader("정보 / 필터", divider="gray")
    st.markdown(f"- **전체 사진 수(좌표 O)**: `{len(df)}`")
    st.markdown(f"- **현재 범위 내 사진 수**: `{len(df_show)}`")

    if pd.notna(min_dt):
        st.markdown(f"- **시작 시각**: `{min_dt}`")
    if pd.notna(max_dt):
        st.markdown(f"- **종료 시각**: `{max_dt}`")

    st.caption("시간 범위를 줄이면 이동 경로를 더 세밀하게 볼 수 있어요.")


with right:
    st.subheader("이동 경로 지도", divider="gray")

    # ---------- 9) 지도의 중심/타일 (세션으로 상태 유지) ----------
    default_center = [
        float(df_show["lat"].mean()),
        float(df_show["lon"].mean())
    ]

    # 이번 실행이 '처음 진입'인지 체크 (center/zoom이 아직 없으면 True)
    first_load = (
        "route_map_center" not in st.session_state
        or "route_map_zoom" not in st.session_state
    )

    # 세션 기본값
    if "route_map_center" not in st.session_state:
        st.session_state["route_map_center"] = default_center
    if "route_map_zoom" not in st.session_state:
        st.session_state["route_map_zoom"] = 13

    center = st.session_state["route_map_center"]
    zoom = st.session_state["route_map_zoom"]

    # center 타입 정규화 (dict → [lat, lon])
    if isinstance(center, dict):
        lat = center.get("lat")
        lng = center.get("lng") or center.get("lon")
        if lat is not None and lng is not None:
            center = [lat, lng]
            st.session_state["route_map_center"] = center
        else:
            center = default_center
            st.session_state["route_map_center"] = center
    elif not (isinstance(center, (list, tuple)) and len(center) == 2):
        center = default_center
        st.session_state["route_map_center"] = center

    m = folium.Map(
        location=center,
        zoom_start=int(zoom),
        tiles="OpenStreetMap",
        control_scale=True,
    )

    # ---------- 10) 경로 라인 (시간순) ----------
    coords = df_show[["lat", "lon"]].to_numpy().tolist()
    folium.PolyLine(coords, color="blue", weight=3, opacity=0.6).add_to(m)

    # ---------- 11) 마커 (시작/중간/종료 아이콘 구분) ----------
    N = len(df_show)
    for i, r in df_show.reset_index(drop=True).iterrows():
        lat, lon = float(r["lat"]), float(r["lon"])
        t = r["taken_at_utc"]
        addr_txt = pretty_addr(r.get("addr_json"))
        img_url = preview_url(r["key"])

        # ✨ hover 시에 보여줄 HTML (사진 + 정보)
        parts = []
        if img_url:
            parts.append(
                f'<img src="{img_url}" '
                f'style="max-width:220px;height:auto;display:block;'
                f'margin-bottom:4px;border-radius:6px;">'
            )
        if pd.notna(t):
            parts.append(f"<b>{t:%Y-%m-%d %H:%M:%S}</b><br>")
        if addr_txt:
            parts.append(f"{addr_txt}<br>")
        parts.append(f"({lat:.5f}, {lon:.5f})")

        hover_html = "".join(parts)

        # folium Tooltip 객체 (hover용)
        tooltip = Tooltip(hover_html, sticky=True)

        # 아이콘 (시작/중간/종료 구분)
        if i == 0:
            icon = folium.Icon(color="green", icon="play", prefix="fa")
        elif i == N - 1:
            icon = folium.Icon(color="darkred", icon="flag", prefix="fa")
        else:
            icon = folium.Icon(color="red", icon="map-marker", prefix="fa")

        folium.Marker(
            [lat, lon],
            icon=icon,
            # popup은 필요없으면 None으로 두거나 아예 빼도 됨
            # popup=folium.Popup(hover_html, max_width=320),
            tooltip=tooltip,   # 👈 이제 hover 시 사진+정보 표시
        ).add_to(m)
    # 👉 여기서 처음 진입일 때 한 번만 전체 마커가 다 보이도록 맞춰주기
    if coords and first_load:
        lats = [c[0] for c in coords]
        lons = [c[1] for c in coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        # 여유 padding
        lat_span = max(max_lat - min_lat, 0.001)
        lon_span = max(max_lon - min_lon, 0.001)

        lat_pad = lat_span * 0.2    # 위아래 20% 여유
        lon_pad = lon_span * 0.3    # 좌우 30% 여유 (왼쪽 붙는 거 방지)

        sw = [min_lat - lat_pad, min_lon - lon_pad]  # southwest
        ne = [max_lat + lat_pad, max_lon + lon_pad]  # northeast

        m.fit_bounds([sw, ne])

    # ---------- 12) 지도 렌더 + 상태 회수 ----------
    map_state = st_folium(
        m,
        width=None,
        height=620,
        key="route_map",
    )

    # folium 인터랙션 결과로부터 center/zoom을 회수해서 세션에 저장
    if isinstance(map_state, dict):
        center_info = map_state.get("center")
        zoom_info = map_state.get("zoom")

        if center_info:
            if isinstance(center_info, dict):
                lat = center_info.get("lat")
                lng = center_info.get("lng") or center_info.get("lon")
                if lat is not None and lng is not None:
                    st.session_state["route_map_center"] = [lat, lng]
            elif isinstance(center_info, (list, tuple)) and len(center_info) == 2:
                st.session_state["route_map_center"] = list(center_info)

        if zoom_info is not None:
            try:
                st.session_state["route_map_zoom"] = int(zoom_info)
            except Exception:
                pass

    st.caption(f"표시된 사진 수(현재 범위): {len(df_show)} / 전체 유효 좌표 사진 수: {len(df)}")
