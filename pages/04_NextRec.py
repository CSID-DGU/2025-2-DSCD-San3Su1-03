import os
import io
import torch
import requests
import streamlit as st
from PIL import Image
from core.storage import get_storage
import psycopg2
from psycopg2.extras import execute_values
import time
from core.openclip_model import load_openclip_model

# =========================================================
# 0. DB 설정 (기존 그대로 사용)
# =========================================================
DB_CONFIG = {
    "host": "dscd.czuosc4sm6fc.ap-northeast-2.rds.amazonaws.com",
    "port": 5432,
    "dbname": "life-recorder",
    "user": "postgres",
    "password": "dscd1234",
}

# =========================================================
# 1. 파라미터
# =========================================================
TOPK_MAIN = 2
TOPK_SUB  = 3
SCALE_TO_PERCENT = True

# =========================================================
# 2. OpenCLIP 모델 로드
# =========================================================
model, preprocess, tokenizer, device = load_openclip_model()
# =========================================================
# 3. DB 연결
# =========================================================
def get_conn():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn

def get_photo_id_by_s3_key(cur, key: str) -> int | None:
    """
    photos 테이블에서 s3_key에 해당하는 id(photo_id)를 찾는다.
    없으면 None 반환.
    """
    cur.execute(
        "SELECT id FROM photos WHERE key = %s",
        (key,)
    )
    row = cur.fetchone()
    return row[0] if row else None

# =========================================================
# 4. label 테이블 읽기 + main text embedding 미리 계산
# =========================================================
@st.cache_resource
def load_label_dictionary():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT label_id, name, level, parent_id
        FROM label
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()

    main_labels = []               # [(id, name), ...] level=1
    sub_labels_by_parent = {}      # parent_id -> [(id, name), ...]

    for lid, name, lv, pid in rows:
        if lv == 1:
            main_labels.append((lid, name))
        elif lv == 2 and pid is not None:
            sub_labels_by_parent.setdefault(pid, []).append((lid, name))

    # main label 텍스트는 미리 임베딩
    main_texts = [x[1] for x in main_labels]
    main_tok = tokenizer(main_texts).to(device)
    with torch.no_grad():
        main_features = model.encode_text(main_tok)
        main_features /= main_features.norm(dim=-1, keepdim=True)

    return rows, main_labels, sub_labels_by_parent, main_features

rows, main_labels, sub_labels_by_parent, main_features = load_label_dictionary()

# label_id -> level 맵 (나중에 level 저장할 때 사용 가능)
level_by_id = {lid: lv for lid, name, lv, pid in rows}

# =========================================================
# 5. 유틸: topk 계산 + 2단계 라벨링
# =========================================================
def get_topk(image_feat, text_feats, label_pairs, k):
    sim = (image_feat @ text_feats.T)[0]
    v, idxs = sim.topk(min(k, sim.shape[0]))
    out = []
    for j, ix in enumerate(idxs):
        lid, _ = label_pairs[int(ix)]
        out.append((lid, float(v[j])))
    return out

def classify_image(pil_img):
    # 이미지 임베딩
    img = preprocess(pil_img).unsqueeze(0).to(device)
    with torch.no_grad():
        img_feat = model.encode_image(img)
        img_feat /= img_feat.norm(dim=-1, keepdim=True)

    # 1차 main top2
    main_top = get_topk(img_feat, main_features, main_labels, TOPK_MAIN)

    # 2차 sub 후보 풀 생성
    sub_candidates = []
    for mid, _ in main_top:
        sub_candidates.extend(sub_labels_by_parent.get(mid, []))

    if not sub_candidates:
        return main_top

    # sub 텍스트 임베딩
    sub_texts = [x[1] for x in sub_candidates]
    sub_tok = tokenizer(sub_texts).to(device)
    with torch.no_grad():
        sub_feat = model.encode_text(sub_tok)
        sub_feat /= sub_feat.norm(dim=-1, keepdim=True)

    sub_top = get_topk(img_feat, sub_feat, sub_candidates, TOPK_SUB)

    return main_top + sub_top   # 총 5개

# =========================================================
# 6. photo_label 저장 함수
# =========================================================
def insert_photo_labels(cur, photo_id: int, top3_avg_labels: list[tuple[int, float]]):
    """
    top3_avg_labels: [(label_id, avg_score), ... top3]
    주어진 photo_id에 대해 photo_label을 덮어쓴다.
    """
    # 기존 photo_label 삭제(같은 사진 재계산 대비)
    cur.execute("DELETE FROM photo_label WHERE photo_id = %s", (photo_id,))

    rows_to_insert = []
    for rank, (lid, score) in enumerate(top3_avg_labels, start=1):
        lv = level_by_id.get(lid, None)  # label 테이블에서 level 가져옴
        rows_to_insert.append(
            (photo_id, lid, float(score), lv, rank, "openclip_vit_g14")
        )

    execute_values(cur, """
        INSERT INTO photo_label (photo_id, label_id, score, level, rank, model)
        VALUES %s
    """, rows_to_insert)

# =========================================================
# 7. Streamlit UI + session_state 기반 실행
# =========================================================
st.title("사용자 업로드 이미지(S3) → OpenCLIP 라벨링 → photo_label 저장")

# Upload 페이지에서 선택한 S3 key들
keys = st.session_state.get("selected_image_keys", [])
if not keys:
    st.warning("먼저 Upload 페이지에서 이미지를 업로드/선택하세요.")
    time.sleep(0.7)
    st.switch_page("pages/00_Upload.py")
    st.stop()

st.write(f"선택된 이미지 개수: {len(keys)}")

storage = get_storage()  # S3Storage

if st.button("라벨링 실행 및 DB 저장"):
    conn = get_conn()
    cur = conn.cursor()

    try:
        for key in keys:
            # 1) photos.id 조회 (photo_id 대신 photos.id 사용)
            photo_id = get_photo_id_by_s3_key(cur, key)
            if photo_id is None:
                st.warning(f"photos 테이블에 s3_key={key} 인 row가 없습니다. 건너뜁니다.")
                continue

            # 2) S3에서 이미지 로드
            try:
                raw = storage.get(key).read()
                pil_img = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception as e:
                st.warning(f"이미지 로딩 실패: {key} / {e}")
                continue

            # 3) 이미지 한 장 라벨링
            label_scores = classify_image(pil_img)  # [(label_id, score), ...] 5개

            if not label_scores:
                st.warning(f"라벨링 결과 없음: {key}")
                continue

            # 4) 상위 3개 라벨만 선택 (대/소분류 섞어서 score 기준으로 top3)
            label_scores_sorted = sorted(label_scores, key=lambda x: x[1], reverse=True)
            top3 = label_scores_sorted[:3]

            # 5) photo_label 테이블에 저장
            insert_photo_labels(cur, photo_id, top3)

        # 모든 key 처리 후 commit
        conn.commit()
        st.success("선택된 모든 이미지에 대해 photo_label 저장이 완료되었습니다.")

    except Exception as e:
        conn.rollback()
        st.error(f"DB 처리 중 오류 발생: {e}")

    finally:
        cur.close()
        conn.close()