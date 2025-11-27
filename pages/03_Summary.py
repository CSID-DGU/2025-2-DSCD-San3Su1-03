import streamlit as st
from openai import OpenAI
from core.config import get_openai_client
from core.storage import get_storage
from core.vision import normalize_image, analyze_photo_bytes, generate_diary
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


# --- 유저 입력 ---
platform = st.selectbox("플랫폼", ["Instagram", "Blog", "X(Twitter)"])
mood = st.text_input("분위기", "Calm and sentimental")
include_elements = st.text_input("포함 요소", "afternoon walk, autumn leaves")
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

    st.subheader("📊 태그 결과")
    st.json(photo_metadata)

    st.subheader("📝 생성된 여행 일기")
    st.markdown(f"### {diary['title']}")
    st.write(diary["content"])
    st.markdown("**해시태그:** " + " ".join(f"#{t}" for t in diary["hashtags"]))
