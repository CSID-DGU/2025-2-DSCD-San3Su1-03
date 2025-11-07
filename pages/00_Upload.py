# pages/00_Upload.py
import sys, os, uuid
import streamlit as st
from PIL import Image, ExifTags
from io import BytesIO
from datetime import datetime
from dotenv import load_dotenv

# ---------- 0) 인증 가드 ----------
auth = st.session_state.get("auth")
if not auth or "user_id" not in auth:
    st.warning("로그인 후에 업로드가 가능해요.")
    st.switch_page("app.py")
    st.stop()

# ---------- 1) UI 공통 적용 ----------
def apply_ui():
    from core.ui import hide_default_nav
    hide_default_nav()
    from app import render_sidebar as _render_sidebar
    _render_sidebar()
apply_ui()

# 모듈 경로
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.storage import get_storage
from core.db import insert_photo_record

load_dotenv()

st.title("Image Upload")

# ---------- 2) 사용자 정보 ----------
user_id = auth["user_id"]
st.text_input("User ID (UUID)", value=user_id, disabled=True)

# ---------- 3) 스토리지 정보 ----------
storage = get_storage()
st.caption(f"Storage backend: **{type(storage).__name__}**")
if hasattr(storage, "bucket"):
    st.caption(f"Bucket: **{getattr(storage, 'bucket', None)}**")

# ---------- 4) 입력 UI ----------
imgs_id = st.number_input("IMGS ID (에피소드 번호)", min_value=1, step=1)
files = st.file_uploader("사진 업로드", type=["jpg","jpeg","png","heic","heif"], accept_multiple_files=True)

# ---------- 5) EXIF 헬퍼 ----------
def _get_exif(img: Image.Image):
    try:
        exif = img._getexif() or {}
        return { ExifTags.TAGS.get(k, k): v for k, v in exif.items() }
    except Exception:
        return {}

def _dms_to_deg(dms, ref):
    # dms: ((deg_num, deg_den), (min_num, min_den), (sec_num, sec_den))
    try:
        deg = dms[0][0] / dms[0][1]
        minutes = dms[1][0] / dms[1][1]
        seconds = dms[2][0] / dms[2][1]
        sign = -1 if ref in ["S", "W"] else 1
        return sign * (deg + minutes/60 + seconds/3600)
    except Exception:
        return None

def extract_gps_datetime(img: Image.Image):
    exif = _get_exif(img)
    # 촬영시각
    taken_at = None
    for key in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
        if exif.get(key):
            raw = exif[key]
            # "YYYY:MM:DD HH:MM:SS" 형태가 일반적
            try:
                taken_at = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
                break
            except Exception:
                # 다른 포맷이 들어오는 경우도 드물게 존재 → 실패 시 None 유지
                pass

    # GPS
    gps_info = exif.get("GPSInfo")
    lon = lat = None
    if isinstance(gps_info, dict):
        # 키가 숫자일 수도 있어 가드
        gps = {}
        for k, v in gps_info.items():
            tag = ExifTags.GPSTAGS.get(k, k)
            gps[tag] = v

        if "GPSLatitude" in gps and "GPSLatitudeRef" in gps \
           and "GPSLongitude" in gps and "GPSLongitudeRef" in gps:
            lat = _dms_to_deg(gps["GPSLatitude"], gps["GPSLatitudeRef"])
            lon = _dms_to_deg(gps["GPSLongitude"], gps["GPSLongitudeRef"])

    return lon, lat, taken_at

# ---------- 6) 업로드 처리 ----------
if files and imgs_id:
    uploaded_keys = []
    meta_pack = []   # 지도 페이지에서 바로 쓰려는 간단 메타: [{key, lon, lat, taken_at}, ...]

    progress = st.progress(0.0, text="업로드 준비 중...")
    total = len(files)

    for idx, file in enumerate(files, start=1):
        try:
            # 1) 한 번만 파일 바이트를 뽑아둔다 (Streamlit UploadedFile은 .read() 후 포인터 이동)
            file_bytes = file.read()

            # 2) 원본(변환 전) 이미지로 EXIF 먼저 추출
            img_raw = Image.open(BytesIO(file_bytes))
            lon, lat, taken_at = extract_gps_datetime(img_raw)  # ✅ EXIF 있는 상태에서 추출

            # a) 이미지 로딩
            img = Image.open(file).convert("RGB")

            # 3) 저장용으로만 RGB 변환해서 재인코딩 (EXIF는 굳이 보존 안 해도 됨)
            img = img_raw.convert("RGB")
            raw = BytesIO()
            img.save(raw, format="JPEG", quality=92)
            data = raw.getvalue()
            size = len(data)

            # d) S3 key 생성
            key  = f"users/{user_id}/imgs/{int(imgs_id)}/original/{uuid.uuid4()}.jpg"

            # e) 업로드
            storage.put(BytesIO(data), key, content_type="image/jpeg")
            uploaded_keys.append(key)

            # f) DB 기록 (메타 동시 저장)
            photo_id = insert_photo_record(
                user_id=user_id,
                bucket=getattr(storage, "bucket", "local-bucket"),
                key=key,
                content_type="image/jpeg",
                size=size,
                taken_at=taken_at, lon=lon, lat=lat
            )

            # g) 세션용 메타도 누적
            meta_pack.append({
                "key": key,
                "lon": lon, "lat": lat,
                "taken_at": taken_at.isoformat() if isinstance(taken_at, datetime) else None,
                "preview_url": getattr(storage, "url", lambda k: None)(key)
            })

            # h) per-file 알림은 얌전하게
            st.success(f"[{file.name}] 업로드/DB 기록 완료 (photo_id={photo_id})")
            st.write("DEBUG EXIF:", {"lon": lon, "lat": lat, "taken_at": taken_at})

        except Exception as e:
            st.error(f"[{file.name}] 처리 실패: {e!r}")

        progress.progress(idx/total, text=f"업로드 진행 중... ({idx}/{total})")

    # ---------- 7) 루프 종료 후 세션에 한 번만 반영 ----------
    if uploaded_keys:
        st.session_state["selected_image_keys"] = uploaded_keys
        st.session_state["selected_image_meta"] = meta_pack
        st.session_state["selected_imgs_group_id"] = int(imgs_id)

        st.info(
            f"✅ 총 {len(uploaded_keys)}개의 이미지 업로드 완료!\n"
            f"👉 이제 상단 메뉴에서 **지도 시각화**로 이동해 경로를 확인하세요."
        )

        # 미리보기(첫 몇 장)
        with st.expander("미리보기", expanded=False):
            for m in meta_pack[:min(6, len(meta_pack))]:
                st.image(
                    m["preview_url"] or "❌",
                    caption=f"{m['key']} | taken_at={m['taken_at']} | (lat,lon)=({m['lat']}, {m['lon']})",
                    use_container_width=True
                )

        # 페이지 이동 버튼 (선택)
        if st.button("➡ 지도 시각화로 이동"):
            st.switch_page("pages/02_Route.py")
