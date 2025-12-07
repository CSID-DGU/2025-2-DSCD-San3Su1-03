# pages/02_Route_re.py
# CSV 기반 지도 시각화 (실험 버전)
import os, sys
import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
from folium.plugins import MarkerCluster

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

# ---------- 2) 모듈 경로 ----------
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.storage import get_storage
from core.db import fetch_episodes_for_user

st.title("지도 시각화")

# ---------- 3) CSV 파일 확인 ----------
csv_path = st.session_state.get("route_csv_path")
episode_no = st.session_state.get("episode_no")

# CSV 디렉토리에서 현재 유저의 파일 목록 가져오기
csv_dir = os.path.join(os.path.dirname(__file__), "..", "data", "route_csv")
available_csvs = []

if os.path.exists(csv_dir):
    for f in os.listdir(csv_dir):
        if f.startswith(f"episode_{user_id}_") and f.endswith(".csv"):
            available_csvs.append(f)

if not available_csvs and not csv_path:
    st.info("먼저 업로드 페이지에서 이미지를 업로드해주세요.")
    st.stop()

# ---------- 4) CSV 선택 UI ----------
st.subheader("에피소드 선택", divider="gray")

# DB에서 에피소드 정보 가져오기
episodes_rows = fetch_episodes_for_user(user_id)
episodes_map = {}  # episode_no -> {title, started_at, ended_at, photo_count}
for row in episodes_rows:
    episodes_map[row["episode_no"]] = {
        "title": row.get("title") or "(제목 없음)",
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "photo_count": row.get("photo_count") or 0,
    }

def format_episode_label(csv_filename: str) -> str:
    """CSV 파일명에서 에피소드 번호 추출 후, DB 정보로 포맷팅"""
    # episode_{user_id}_{episode_no}.csv 형태에서 episode_no 추출
    try:
        base = csv_filename.replace(f"episode_{user_id}_", "").replace(".csv", "")
        ep_no = int(base)
    except ValueError:
        return csv_filename  # 파싱 실패 시 원본 반환

    info = episodes_map.get(ep_no)
    if not info:
        return f"[{ep_no}] (정보 없음)"

    title = info["title"]
    cnt = info["photo_count"]

    # 기간 포맷팅
    s = info["started_at"]
    e = info["ended_at"]
    if s and e:
        try:
            s_str = s.strftime("%Y-%m-%d")
            e_str = e.strftime("%Y-%m-%d")
            date_range = f"{s_str} ~ {e_str}" if s_str != e_str else s_str
        except Exception:
            date_range = "-"
    elif s:
        try:
            date_range = s.strftime("%Y-%m-%d")
        except Exception:
            date_range = "-"
    else:
        date_range = "-"

    return f"[{ep_no}] {title} | {date_range} | {cnt}장"

# 현재 세션의 CSV가 있으면 기본 선택
default_idx = 0
if csv_path and os.path.basename(csv_path) in available_csvs:
    default_idx = available_csvs.index(os.path.basename(csv_path))

selected_csv = st.selectbox(
    "열어볼 에피소드를 선택하세요:",
    options=available_csvs,
    index=default_idx,
    format_func=format_episode_label,
)

if not selected_csv:
    st.warning("선택할 CSV 파일이 없습니다.")
    st.stop()

# ---------- 5) CSV 로드 ----------
csv_full_path = os.path.join(csv_dir, selected_csv)

@st.cache_data(show_spinner=True)
def load_csv(path):
    df = pd.read_csv(path)
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df["taken_at"] = pd.to_datetime(df["taken_at"], errors="coerce")
    # 유효 좌표만 + 시간순 정렬
    df = df.dropna(subset=["lat", "lon"])
    df = df.sort_values(["taken_at"], na_position="last").reset_index(drop=True)
    return df

df = load_csv(csv_full_path)

if df.empty:
    st.info("표시할 좌표가 있는 사진이 없어요.")
    st.stop()

# ---------- 6) 시간 범위 필터 ----------
min_dt, max_dt = df["taken_at"].min(), df["taken_at"].max()

with st.container():
    if pd.notna(min_dt) and pd.notna(max_dt) and min_dt != max_dt:
        rng = st.slider(
            "시간 범위 선택",
            min_value=min_dt.to_pydatetime(),
            max_value=max_dt.to_pydatetime(),
            value=(min_dt.to_pydatetime(), max_dt.to_pydatetime()),
            key="csv_route_time_range",
        )
        df_show = df[
            (df["taken_at"] >= rng[0]) &
            (df["taken_at"] <= rng[1])
        ].copy()
    else:
        df_show = df.copy()

if df_show.empty:
    st.warning("선택한 범위에 표시할 사진이 없습니다.")
    st.stop()

# ---------- 7) 스토리지 (이미지 URL용) ----------
storage = get_storage()

def preview_url(key: str) -> str | None:
    try:
        return storage.url(key)
    except Exception:
        return None

# ---------- 8) 레이아웃 ----------
left, right = st.columns([1, 2])

with left:
    st.subheader("정보", divider="gray")
    st.markdown(f"- **전체 사진 수(좌표 O)**: `{len(df)}`")
    st.markdown(f"- **현재 범위 내 사진 수**: `{len(df_show)}`")

    if pd.notna(min_dt):
        st.markdown(f"- **시작 시각**:\n\n  `{min_dt}`")

    if pd.notna(max_dt):
        st.markdown(f"- **종료 시각**:\n\n  `{max_dt}`")

with right:
    st.subheader("이동 경로 지도", divider="gray")

    # 지도 경계 계산 (데이터 기반)
    min_lat = float(df_show["lat"].min())
    max_lat = float(df_show["lat"].max())
    min_lon = float(df_show["lon"].min())
    max_lon = float(df_show["lon"].max())
    center = [(min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0]

    # folium 지도 생성
    m = folium.Map(
        location=center,
        tiles="OpenStreetMap",
        control_scale=True,
        max_zoom=18,
    )

    # 모든 마커가 보이도록 경계에 맞춰 자동 줌 조정
    m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

    # 클러스터링
    cluster = MarkerCluster(
        options={
            "maxClusterRadius": 20,
            "spiderfyOnMaxZoom": True,
            "disableClusteringAtZoom": 18
        }
    ).add_to(m)

    # 경로 PolyLine
    coords = df_show[["lat", "lon"]].to_numpy().tolist()
    if coords:
        folium.PolyLine(coords, color="blue", weight=3, opacity=0.6).add_to(m)

    # 마커들
    N = len(df_show)

    for i, r in df_show.reset_index(drop=True).iterrows():
        lat, lon = float(r["lat"]), float(r["lon"])
        time_val = r["taken_at"]

        # preview_url 컬럼이 있으면 사용, 없으면 key로 생성
        if "preview_url" in r and pd.notna(r["preview_url"]):
            img_url = r["preview_url"]
        elif "key" in r and pd.notna(r["key"]):
            img_url = preview_url(r["key"])
        else:
            img_url = None

        parts = []
        if img_url:
            parts.append(
                f'<img src="{img_url}" '
                f'style="max-width:220px;height:auto;border-radius:6px;'
                f'display:block;margin-bottom:4px;">'
            )
        if pd.notna(time_val):
            parts.append(f"<b>{time_val:%Y-%m-%d %H:%M:%S}</b><br>")
        parts.append(f"({lat:.5f}, {lon:.5f})")

        tooltip = folium.Tooltip("".join(parts), sticky=True)

        if i == 0:
            icon = folium.Icon(color="green", icon="play", prefix="fa")
        elif i == N - 1:
            icon = folium.Icon(color="darkred", icon="flag", prefix="fa")
        else:
            icon = folium.Icon(color="red", icon="map-marker", prefix="fa")

        folium.Marker(
            [lat, lon],
            tooltip=tooltip,
            icon=icon,
        ).add_to(cluster)

    # 지도 렌더링
    st_folium(
        m,
        width=None,
        height=620,
        key=f"csv_route_map_{user_id}",
    )

    st.caption(
        f"표시된 사진 수(현재 범위): {len(df_show)} / "
        f"전체 유효 좌표 사진 수: {len(df)}"
    )
