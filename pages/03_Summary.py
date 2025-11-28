import streamlit as st
from openai import OpenAI
from core.config import get_openai_client
from core.storage import get_storage
from core.vision import normalize_image, analyze_photo_bytes, generate_diary
from core.db import insert_episode_diary
import time

def apply_ui():
    from core.ui import hide_default_nav
    hide_default_nav()
    from app import render_sidebar as _render_sidebar
    _render_sidebar()

apply_ui() 

st.title("📸 AI 여행일기 생성")

# Upload 페이지에서 선택한 s3 키들을 세션으로 전달
keys = st.session_state.get("selected_image_keys", [])
if not keys:
    st.info("먼저 Upload 페이지에서 이미지를 업로드해주세요.")
    time.sleep(1)
    st.switch_page("pages/00_Upload.py")
    st.stop()

st.write(f"선택된 이미지 개수: {len(keys)}")

# ✅ 여기서 클라이언트 생성
client = get_openai_client()
episode_no = st.session_state.get("selected_imgs_group_id")
user_id = st.session_state["auth"]["user_id"]  # 인증 가드가 있다고 가정

# 기본값 세팅
if "generated_text" not in st.session_state:
    st.session_state["generated_text"] = None

if "diary" not in st.session_state:
    st.session_state["diary"] = None

# --- 유저 입력 ---
platform = st.selectbox("플랫폼을 선택해주세요", ["Instagram", "Blog", "X(Twitter)"])
mood = st.selectbox("일기의 분위기를 선택해주세요", ["잔잔하고 감성적이게","밝고 명랑하게","모험적이고 활기차게","차분하고 사색적이게","사실 중심으로 담백하게"])
include_elements = st.text_input("포함되기 원하는 키워드를 입력해주세요", placeholder = "예시: 추억, 낭만, 여운")
language = st.selectbox("언어", ["Korean", "English"])

if st.button("✏️ 여행일기 생성하기"):
    storage = get_storage()
    photos = []

    with st.spinner("📷 이미지 분석 중..."):
        for key in keys:
            # 1) S3에서 이미지 바이너리 로드
            raw = storage.get(key).read()
            # 2) Vision friendly resizing
            norm = normalize_image(raw)
            # 3) Vision 분석
            result = analyze_photo_bytes(norm, client)
            photos.append({"key": key, **result})

    photo_metadata = {"trip_date": "N/A", "weather": "N/A", "photos": photos}
    req = {
        "platform": platform,
        "mood": mood,
        "include_elements": [e.strip() for e in include_elements.split(",")],
        "language": language
    }

    diary = generate_diary(photo_metadata, req, client)

    # 생성 결과를 세션에 저장
    st.session_state["diary"] = diary
    st.success("여행 일기가 생성되었습니다 ✅")

    # st.subheader("📊 태그 결과")
    # st.json(photo_metadata)

# ===================== 2) 생성된 일기 표시 + 저장 버튼 =====================
diary = st.session_state.get("diary")

if diary:
    st.subheader("📝 생성된 여행 일기")
    st.markdown(f"### {diary.get('title', '(제목 없음)')}")
    st.write(diary.get("content", ""))

    # 해시태그 처리 (키가 없거나 문자열/리스트일 수 있으니 안전하게)
    raw_tags = diary.get("hashtags") or diary.get("tags") or []
    if isinstance(raw_tags, str):
        hashtags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    else:
        hashtags = list(raw_tags)

    if hashtags:
        st.markdown("**해시태그:** " + " ".join(f"#{t}" for t in hashtags))
    else:
        st.caption("해시태그 정보가 없습니다.")

    
    if episode_no and st.button("이 일기 저장하기"):
        insert_episode_diary(
            user_id=user_id,
            episode_no=int(episode_no),
            mood=mood,
            title=diary.get("title", "(제목 없음)"),
            content=diary.get("content", ""),
            tags=", ".join(hashtags) if hashtags else None,
        )
        st.success("마이페이지에 이 일기를 저장했습니다 ✅")
