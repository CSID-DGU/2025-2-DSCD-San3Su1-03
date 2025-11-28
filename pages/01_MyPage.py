# pages/01_MyPage.py
import os, sys
import streamlit as st
import pandas as pd
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
    fetch_photos_by_group_prefix,  # 이미 있을 거라고 가정
)
from core.storage import get_storage

st.title("My Page - 과거 에피소드 기록")

# ---------- 2) 에피소드 목록 로드 ----------
rows = fetch_episodes_for_user(user_id)
if not rows:
    st.info("아직 저장된 에피소드가 없습니다.\n\n먼저 이미지를 업로드해보세요.")
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

st.markdown("---")

# ---------- 4) 레이아웃 ----------
left, right = st.columns([2, 3])

# ========== 왼쪽: 사진 썸네일 & 간단 지도 프리뷰 ==========
with left:
    st.subheader("사진 & 간단 프리뷰", divider="gray")

    photos = fetch_photos_by_group_prefix(user_id, episode_no)
    if not photos:
        st.info("이 에피소드에는 아직 사진이 없습니다.")
    else:
        storage = get_storage()
        # 썸네일 몇 장만
        preview_urls = []
        for r in photos[:12]:
            try:
                url = storage.url(r["key"])
                preview_urls.append(url)
            except Exception:
                continue

        if preview_urls:
            st.markdown("#### 사진 미리보기 (일부)")
            st.image(preview_urls, width=160)
        else:
            st.caption("썸네일을 불러오지 못했습니다.")

        st.caption(f"총 사진 수: {len(photos)} 장")

        # 간단한 안내
        st.info(
            "👉 '이 에피소드로 이동경로 지도 보기' 버튼을 누르면\n"
            "지도 시각화 페이지에서 이 에피소드의 이동 경로를 자세히 볼 수 있어요."
        )

# ========== 오른쪽: 에피소드/일기 정보 ==========
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

    # 저장된 AI 일기 목록
    st.markdown("### 저장된 AI 일기")
    diaries = fetch_diaries_for_episode(user_id, episode_no)
    if diaries:
        for d in diaries:
            with st.expander(f"[{d['mood'] or 'Unknown'}] {d['title'] or '(제목 없음)'}  ·  {d['created_at'].date()}"):
                if d.get("tags"):
                    st.caption(f"태그: {d['tags']}")
                st.write(d["content"])
    else:
        st.caption("아직 저장된 AI 일기가 없습니다.\nAI 요약/일기 페이지에서 생성 후 '저장하기'를 눌러보세요.")

    st.markdown("### 에피소드 열기")
    if st.button("이 에피소드로 이동경로 지도 보기"):
        # Route 페이지에서 사용할 세션 상태 세팅
        st.session_state["selected_image_keys"] = []  # 굳이 안 채워도 됨
        st.session_state["selected_imgs_group_id"] = episode_no
        st.switch_page("pages/02_Route.py")

    if st.button("이 에피소드로 AI 일기/요약 생성하기"):
        st.session_state["selected_image_keys"] = []  # 필요하면 채워도 됨
        st.session_state["selected_imgs_group_id"] = episode_no
        st.switch_page("pages/03_Summary.py")  # 네 AI 요약 페이지 파일명에 맞게 수정


