# pages/01_MyPage.py
import os, sys, time
import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
from datetime import datetime

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

# 모듈 경로
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.db import (
    fetch_episodes_for_user,
    fetch_diaries_for_episode,
    fetch_photos_by_group_prefix,
)
from core.storage import get_storage

# 🔹 전역 storage 객체 한 번만 생성
storage = get_storage()

def preview_url(key: str) -> str | None:
    """S3 key → 실제 접근 가능한 URL 생성"""
    try:
        return storage.url(key)
    except Exception:
        return None

st.title("My Page - 과거 에피소드 기록")

# ---------- 2) 에피소드 목록 로드 ----------
rows = fetch_episodes_for_user(user_id)
if not rows:
    st.info("아직 저장된 에피소드가 없습니다. 먼저 이미지를 업로드해보세요.")
    time.sleep(1)
    st.switch_page("pages/00_Upload.py")
    st.stop()

episodes_df = pd.DataFrame(rows)

# 없을 수 있는 컬럼 방어
for col in ["title", "started_at", "ended_at", "photo_count"]:
    if col not in episodes_df.columns:
        episodes_df[col] = None

# ---------- 3) 에피소드 선택 UI ----------
st.subheader("에피소드 선택", divider="gray")

def format_episode(row):
    no = row["episode_no"]
    title = row["title"] or "(제목 없음)"
    cnt = row.get("photo_count") or 0
    if row.get("started_at") and row.get("ended_at"):
        try:
            s = row["started_at"].strftime("%Y-%m-%d")
            e = row["ended_at"].strftime("%Y-%m-%d")
            date_range = f"{s} ~ {e}" if s != e else s
        except Exception:
            date_range = "-"
    else:
        date_range = "-"
    return f"[{no}] {title} | {date_range} | {cnt}장"

episode_options = list(episodes_df.index)

selected_idx = st.selectbox(
    "열어볼 에피소드를 선택하세요:",
    options=episode_options,
    format_func=lambda i: format_episode(episodes_df.loc[i]),
)

selected = episodes_df.loc[selected_idx]
episode_no = int(selected["episode_no"])

# 🔹 이 에피소드의 사진들을 한 번만 로드 + URL까지 붙여두기
photos = fetch_photos_by_group_prefix(user_id, episode_no)
if photos:
    photos_df = pd.DataFrame(photos)
    # key → 실제 접근 가능한 URL
    photos_df["preview_url"] = photos_df["key"].apply(preview_url)
else:
    photos_df = pd.DataFrame()

st.markdown("---")

# ---------- 4) 레이아웃 ----------
left, right = st.columns([2, 3])

# ========== 왼쪽: 사진 썸네일 ==========
with left:
    st.subheader("사진 & 간단 프리뷰", divider="gray")

    if photos_df.empty:
        st.info("이 에피소드에는 아직 사진이 없습니다.")
    else:
        urls = [u for u in photos_df["preview_url"].tolist() if u]

        if urls:
            st.markdown("#### 사진 미리보기 (일부)")

            preview_urls = urls[:12]  # 최대 12장만 미리보기
            n_cols = 2                # 🔹 한 행에 2장

            for i in range(0, len(preview_urls), n_cols):
                row_urls = preview_urls[i:i + n_cols]
                cols = st.columns(n_cols, gap="small")

                for col, url in zip(cols, row_urls):
                    with col:
                        # 컨테이너 폭에 맞게 채우기 → 인스타그램 타일 느낌
                        st.image(url, use_container_width=True)

        else:
            st.caption("썸네일을 불러오지 못했습니다.")

        st.caption(f"총 사진 수: {len(photos_df)} 장")

# ========== 오른쪽: 에피소드 정보 + 지도 + AI 일기 ==========
with right:
    st.subheader("에피소드 정보", divider="gray")
    st.write(f"**에피소드 번호:** `{episode_no}`")
    st.write(f"**제목:** `{selected.get('title') or '제목 없음'}`")

    started_at = selected.get("started_at")
    ended_at = selected.get("ended_at")
    if started_at and ended_at:
        st.write(f"**기간:** `{started_at}` ~ `{ended_at}`")
    elif started_at:
        st.write(f"**시작 시각:** `{started_at}`")

    st.write(f"**사진 수:** `{selected.get('photo_count') or 0}` 장")

    # --- 이동 경로 지도 ---
    st.subheader("이동 경로 지도", divider="gray")

    if photos_df.empty:
        st.info("이 에피소드에는 위치 정보가 있는 사진이 없어요.")
    else:
        # lat/lon/timestamp 정리
        df_map = photos_df.copy()
        df_map["lat"] = pd.to_numeric(df_map["lat"], errors="coerce")
        df_map["lon"] = pd.to_numeric(df_map["lon"], errors="coerce")
        df_map["taken_at_utc"] = pd.to_datetime(df_map.get("taken_at_utc"), errors="coerce")

        df_map = df_map.dropna(subset=["lat", "lon"])
        if df_map.empty:
            st.info("위치 정보가 있는 사진이 없습니다.")
        else:
            # 중심 & 전체 좌표
            df_map = df_map.sort_values(["taken_at_utc", "id"],
                                        na_position="last").reset_index(drop=True)
            coords = df_map[["lat", "lon"]].to_numpy().tolist()
            center = [float(df_map["lat"].mean()), float(df_map["lon"].mean())]

            # 지도 생성
            m = folium.Map(
                location=center,
                zoom_start=13,
                tiles="OpenStreetMap",
                control_scale=True,
                max_zoom=18,
            )

            # 경로 라인 추가
            if coords:
                folium.PolyLine(coords, color="blue", weight=3, opacity=0.6).add_to(m)

            # 🔥 마커들을 모아두는 FeatureGroup 생성 (핵심 포인트)
            marker_group = folium.FeatureGroup(name="markers")

            N = len(df_map)
            for i, r in df_map.iterrows():
                lat, lon = float(r["lat"]), float(r["lon"])
                t = r["taken_at_utc"]

                # S3 이미지 URL
                try:
                    img_url = storage.url(r["key"])
                except Exception:
                    img_url = None

                # 촬영 시각 문자열
                time_str = f"{t:%Y-%m-%d %H:%M}" if pd.notna(t) else ""

                # Tooltip HTML
                if img_url:
                    tooltip_html = (
                        "<div style='width:200px;border:1px solid #ddd;"
                        "background-color:white;padding:4px;border-radius:6px;'>"
                        f"<img src='{img_url}' "
                        "style='width:100%;border-radius:4px;margin-bottom:4px;'>"
                        f"<div style='font-size:11px;color:#333;'>{time_str}</div>"
                        "</div>"
                    )
                else:
                    tooltip_html = time_str or "photo"

                tooltip = folium.Tooltip(tooltip_html, sticky=True)

                # 아이콘
                if i == 0:
                    icon = folium.Icon(color="green", icon="play", prefix="fa")
                elif i == N - 1:
                    icon = folium.Icon(color="darkred", icon="flag", prefix="fa")
                else:
                    icon = folium.Icon(color="red", icon="map-marker", prefix="fa")

                # 🔥 Marker를 직접 m에 add하지 않고 marker_group에만 add
                folium.Marker(
                    [lat, lon],
                    icon=icon,
                    tooltip=tooltip,
                ).add_to(marker_group)

            # 그룹을 맵에 한 번에 추가
            marker_group.add_to(m)

            # 🔹 모든 마커가 한 화면에 들어오도록 zoom 조정
            if coords:
                lats = [c[0] for c in coords]
                lons = [c[1] for c in coords]

                padding = 0.005
                min_lat, max_lat = min(lats) - padding, max(lats) + padding
                min_lon, max_lon = min(lons) - padding, max(lons) + padding
                bounds = [[min_lat, min_lon], [max_lat, max_lon]]

                m.fit_bounds(bounds, max_zoom=13)

            # 지도 렌더링
            st_folium(m, width=None, height=520, key=f"mypage_map_{episode_no}")
    

    # --- 저장된 AI 일기 목록 ---
    st.markdown("### 저장된 AI 일기")
    diaries = fetch_diaries_for_episode(user_id, episode_no)
    if diaries:
        for d in diaries:
            with st.expander(
                f"[{d['mood'] or 'Unknown'}] {d['title'] or '(제목 없음)'}  ·  {d['created_at'].date()}"
            ):
                if d.get("tags"):
                    st.caption(f"태그: {d['tags']}")
                st.write(d["content"])
    else:
        st.caption("아직 저장된 AI 일기가 없습니다.\nAI 요약/일기 페이지에서 생성 후 '저장하기'를 눌러보세요.")

    st.divider()
    # ---------- 9) 새로운 에피소드 올리기 (초기화 버튼) ----------
    if st.button("🆕 새로운 에피소드 올리기"):
        # 이전 에피소드 관련 상태 싹 정리
        st.session_state["selected_image_keys"] = []
        st.session_state["selected_image_meta"] = []
        st.session_state["upload_files"] = None
        st.session_state["episode_title"] = ""
        st.success("새로운 에피소드 업로드를 시작할 준비가 되었습니다.")
        st.switch_page("pages/00_Upload.py")
        st.rerun()
