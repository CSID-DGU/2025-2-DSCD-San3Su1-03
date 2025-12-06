# pages/00_Upload.py
import sys, os, uuid
import streamlit as st
import pandas as pd
from PIL import Image, ExifTags, ImageOps
from io import BytesIO
from datetime import datetime

# ✅ .env를 최우선으로 로드 (상위 경로까지 탐색)
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)

from core.geocode import kakao_reverse
from core.db import get_address_cache_by_coord, upsert_address_cache, upsert_episode_for_upload, refresh_episode_meta

# ---------- 0) 인증 가드 ----------
auth = st.session_state.get("auth")
if not auth or "user_id" not in auth:
    st.warning("로그인 후에 업로드가 가능해요.")
    st.switch_page("app.py")
    st.stop()

# ---------- 세션 기본값 세팅 ----------
if "selected_image_keys" not in st.session_state:
    st.session_state["selected_image_keys"] = []

if "selected_image_meta" not in st.session_state:
    st.session_state["selected_image_meta"] = []

if "episode_no" not in st.session_state:          # 실제로 들고 다닐 에피소드 번호
    st.session_state["episode_no"] = None

if "episode_no_input" not in st.session_state:    # 위젯용 임시값
    st.session_state["episode_no_input"] = 1

if "episode_title_key" not in st.session_state:
    st.session_state["episode_title_key"] = "episode_title"


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


st.title("이미지 업로드")

# ---------- 2) 사용자 정보 ----------
user_id = auth["user_id"]
st.text_input("User ID (UUID)", value=user_id, disabled=True)

# ---------- 3) 스토리지 정보 ----------
storage = get_storage()
st.caption(f"Storage backend: **{type(storage).__name__}**")
if hasattr(storage, "bucket"):
    st.caption(f"Bucket: **{getattr(storage, 'bucket', None)}**")

# ---------- 4) 입력 UI ----------
# 현재 저장된 episode_no가 있으면 그걸 기본값으로 사용
# default_episode = st.session_state.get("episode_no") or 1

imgs_id = st.number_input(
    "에피소드 번호",
    min_value=1,
    step=1,
    # value=default_episode,     # 🔹 기본값
    key="episode_no_input", 
)

episode_title = st.text_input(
    "에피소드 제목",
    key=st.session_state["episode_title_key"],
    placeholder="예: 한강 나들이, 부산 2박 3일 여행",
)

# ✅ 업로드 가능 여부: 번호 있고, 제목이 공백이 아닐 때만 True
upload_ready = bool(imgs_id) and bool(episode_title.strip())

files = st.file_uploader(
    "이미지 업로드",
    type=["jpg","jpeg","png","heic","heif"],
    accept_multiple_files=True,
    key=st.session_state.get("upload_files_key", "upload_files"),            # ✅ 세션 키에 바인딩
    disabled=not upload_ready,
)

if not upload_ready:
    st.caption("먼저 에피소드 번호와 제목을 입력해 주세요.")

from PIL import ExifTags

def _as_float(x):
    """IFDRational / Fraction / tuple(num,den) / int / float 모두 안전 변환"""
    try:
        if isinstance(x, tuple) and len(x) == 2:
            n, d = x
            return float(n) / float(d) if d else None
        return float(x)
    except Exception:
        return None

def _norm_ref(v):
    """b'N' 같은 bytes → 'N' 로 교정"""
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode(errors="ignore")
        except Exception:
            return str(v)
    return v

def _dms_to_deg(dms, ref):
    """
    dms: [deg, min, sec] 각 원소가 IFDRational/tuple/float 등 다양할 수 있음
    ref: 'N','S','E','W' (bytes일 수도 있음)
    """
    try:
        ref = _norm_ref(ref)
        parts = [ _as_float(p) for p in dms ]
        if any(p is None for p in parts):
            return None
        deg, minutes, seconds = parts[0], (parts[1] if len(parts) > 1 else 0.0), (parts[2] if len(parts) > 2 else 0.0)
        sign = -1 if str(ref).upper() in ("S", "W") else 1
        return sign * (deg + minutes/60.0 + seconds/3600.0)
    except Exception:
        return None

def _get_exif(img: Image.Image):
    try:
        exif = img._getexif() or {}
        return { ExifTags.TAGS.get(k, k): v for k, v in exif.items() }
    except Exception:
        return {}

def extract_gps_datetime(img: Image.Image):
    exif = _get_exif(img)

    # ---- 촬영시각 ----
    taken_at = None
    for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        raw = exif.get(key)
        if not raw:
            continue
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                from datetime import datetime
                taken_at = datetime.strptime(str(raw), fmt)
                break
            except Exception:
                pass
        if taken_at:
            break

    # ---- GPS ----
    lon = lat = None
    gps_info = exif.get("GPSInfo")
    if isinstance(gps_info, dict):
        gps = { ExifTags.GPSTAGS.get(k, k): v for k, v in gps_info.items() }

        # (A) 십진수로 바로 들어있는 경우
        if isinstance(gps.get("GPSLatitude"), (int, float)) and isinstance(gps.get("GPSLongitude"), (int, float)):
            lat = float(gps["GPSLatitude"])
            lon = float(gps["GPSLongitude"])
            return lon, lat, taken_at

        # (B) DMS + Ref 표준 케이스
        lat_ref = _norm_ref(gps.get("GPSLatitudeRef"))
        lon_ref = _norm_ref(gps.get("GPSLongitudeRef"))
        if gps.get("GPSLatitude") is not None and gps.get("GPSLongitude") is not None and lat_ref and lon_ref:
            lat = _dms_to_deg(gps["GPSLatitude"], lat_ref)
            lon = _dms_to_deg(gps["GPSLongitude"], lon_ref)

    return lon, lat, taken_at


# ---------- 6) 업로드 처리 ----------
if files and upload_ready:
    uploaded_keys = []
    meta_pack = []   # 지도 페이지에서 바로 쓰려는 간단 메타: [{key, lon, lat, taken_at}, ...]
    with_location = []    # 위치 정보 있는 파일명
    without_location = [] # 위치 정보 없는 파일명
    failed_files = []     # 업로드 자체가 실패한 파일명

    progress = st.progress(0.0, text="업로드 준비 중...")
    total = len(files)

    for idx, file in enumerate(files, start=1):
        progress.progress(idx/total, text=f"업로드 중... ({idx}/{total})")

        try:
            # 1) 한 번만 파일 바이트를 뽑아둔다 (Streamlit UploadedFile은 .read() 후 포인터 이동)
            file_bytes = file.read()

            # 2) 원본(변환 전) 이미지로 EXIF 먼저 추출
            img_raw = Image.open(BytesIO(file_bytes))
            lon, lat, taken_at = extract_gps_datetime(img_raw)  # ✅ EXIF 있는 상태에서 추출
            img_raw = ImageOps.exif_transpose(img_raw)

            # 3) 저장용으로만 RGB 변환해서 재인코딩
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
            # ✅ 역지오코딩 캐시 저장 (좌표가 있을 때만, 조용히 처리)
            if lat is not None and lon is not None:
                try:
                    cached = get_address_cache_by_coord(lat, lon, provider="kakao")
                    if cached and cached.get("data"):
                        upsert_address_cache(photo_id, lat, lon, "kakao", cached["data"])
                    else:
                        info = kakao_reverse(lat, lon)
                        if info:
                            upsert_address_cache(photo_id, lat, lon, info.get("provider","kakao"), info)
                except Exception:
                    pass  # 주소 캐시 실패는 조용히 넘김

            # g) 세션용 메타도 누적
            meta_pack.append({
                "key": key,
                "lon": lon, "lat": lat,
                "taken_at": taken_at.isoformat() if isinstance(taken_at, datetime) else None,
                "preview_url": getattr(storage, "url", lambda k: None)(key)
            })

            # 위치 정보 유무에 따라 분류
            if lat is not None and lon is not None:
                with_location.append(file.name)
            else:
                without_location.append(file.name)

        except Exception as e:
            failed_files.append((file.name, str(e)))

    # 업로드 완료 후 결과 표시
    progress.empty()  # 프로그레스 바 제거

    # 업로드 자체가 실패한 파일
    if failed_files:
        for fname, err in failed_files:
            st.error(f"❌ {fname} - 업로드 실패")

    # 위치 정보 없는 파일
    if without_location:
        for fname in without_location:
            st.warning(f"📍 {fname} - 위치 정보가 없어 지도에 표시되지 않습니다.")

    # 위치 정보 있는 파일 (지도 시각화 가능)
    if with_location:
        for fname in with_location:
            st.success(f"✅ {fname} - 업로드 완료 (지도 시각화 가능)")

    # 요약 메시지
    total_uploaded = len(with_location) + len(without_location)
    if total_uploaded > 0:
        st.divider()
        st.markdown(f"**총 {total_uploaded}장 중 {len(with_location)}장은 지도에 표시됩니다.**")
        if with_location:
            st.caption("🗺️ 지도에 표시 가능한 사진은 **이동경로 시각화** 메뉴에서 확인할 수 있어요.")

    # ---------- 7) 루프 종료 후 세션에 한 번만 반영 ----------
    if uploaded_keys:
        st.session_state["selected_image_keys"] = uploaded_keys
        st.session_state["selected_image_meta"] = meta_pack


        episode_no = int(imgs_id)
        st.session_state["episode_no"] = episode_no   # 🔹 여기서만 저장

        title = episode_title.strip() or None
        upsert_episode_for_upload(user_id, episode_no, title=title)
        refresh_episode_meta(user_id, episode_no)

        # ✅ CSV 파일로도 저장 (지도 실험 페이지용)
        csv_dir = os.path.join(os.path.dirname(__file__), "..", "data", "route_csv")
        os.makedirs(csv_dir, exist_ok=True)
        csv_path = os.path.join(csv_dir, f"episode_{user_id}_{episode_no}.csv")

        csv_rows = []
        for m in meta_pack:
            csv_rows.append({
                "key": m["key"],
                "lat": m["lat"],
                "lon": m["lon"],
                "taken_at": m["taken_at"],
                "preview_url": m["preview_url"],
            })

        if csv_rows:
            df_csv = pd.DataFrame(csv_rows)
            df_csv.to_csv(csv_path, index=False, encoding="utf-8-sig")
            st.session_state["route_csv_path"] = csv_path

        st.info("👉 이제 좌측 메뉴에서 기능들을 이용하실 수 있습니다.")


# ---------- 8) 현재 세션에 저장된 에피소드 정보 보여주기 ----------
saved_keys = st.session_state.get("selected_image_keys", [])
saved_meta = st.session_state.get("selected_image_meta", [])
saved_group = st.session_state.get("episode_no", None)

if saved_keys:
    st.success(
        f"현재 에피소드 번호: {saved_group} | 업로드된 이미지: {len(saved_keys)}장"
    )

    with st.expander("현재 에피소드 미리보기", expanded=False):
        for m in saved_meta[:min(6, len(saved_meta))]:
            st.image(
                m["preview_url"] or "❌",
                caption=f"{m['key']} | taken_at={m['taken_at']} | (lat,lon)=({m['lat']}, {m['lon']})",
                use_container_width=True
            )
else:
    st.info("아직 업로드된 이미지가 없습니다. 이미지를 업로드해 주세요.")


# ---------- 9) 새로운 에피소드 올리기 (초기화 버튼) ----------
st.divider()
if st.button("🆕 새로운 에피소드 올리기"):
    # 이전 에피소드 관련 상태 싹 정리
    st.session_state["selected_image_keys"] = []
    st.session_state["selected_image_meta"] = []

    # 🔥 파일 업로더 초기화 방법 → 업로드 위젯의 key를 교체
    st.session_state["upload_files_key"] = str(uuid.uuid4())

    # 🔥 episode_title도 key를 변경하여 초기화
    st.session_state["episode_title_key"] = "episode_title_" + str(uuid.uuid4())

    st.success("새로운 에피소드 업로드를 시작할 준비가 되었습니다.")
    st.rerun()
