import os
import io
import requests
import torch
import pandas as pd
from PIL import Image
import open_clip
from tqdm import tqdm
import psycopg2
from psycopg2.extras import execute_values

# =========================================================
# 0. 사용자 수정 구간
# =========================================================
DB_CONFIG = {
    "host": "dscd.czuosc4sm6fc.ap-northeast-2.rds.amazonaws.com",
    "port": 5432,
    "dbname": "life-recorder",
    "user": "postgres",
    "password": "dscd1234",
}

# =========================================================
# 1. 파라미터 설정
# =========================================================
TOPK_MAIN = 2
TOPK_SUB = 3

SCALE_TO_PERCENT = True       # True면 score*100 저장
REQ_TIMEOUT = 10
MAX_RETRY = 2

# =========================================================
# 2. OpenCLIP 모델 로드
# =========================================================
device = "cuda" if torch.cuda.is_available() else "cpu"
model, _, preprocess = open_clip.create_model_and_transforms(
    'ViT-G-14',
    pretrained='laion2b_s12b_b42k'
)
tokenizer = open_clip.get_tokenizer('ViT-G-14')

model.to(device).eval()

# =========================================================
# 3. DB 연결
# =========================================================
conn = psycopg2.connect(**DB_CONFIG)
conn.autocommit = False
cur = conn.cursor()

# =========================================================
# 4. label 테이블 읽기
# =========================================================
cur.execute("""
    SELECT label_id, name, level, parent_id
    FROM label
""")
rows = cur.fetchall()

main_labels = []               # level=1
sub_labels_by_parent = {}      # parent_id → sub list

for lid, name, lv, pid in rows:
    if lv == 1:
        main_labels.append((lid, name))
    elif lv == 2 and pid is not None:
        sub_labels_by_parent.setdefault(pid, []).append((lid, name))

main_label_ids  = [x[0] for x in main_labels]
main_label_text = [x[1] for x in main_labels]

# main label 텍스트는 미리 임베딩 해둔다
main_tok = tokenizer(main_label_text).to(device)
with torch.no_grad():
    main_features = model.encode_text(main_tok)
    main_features /= main_features.norm(dim=-1, keepdim=True)

# =========================================================
# 5. 유틸 함수 (이미지 다운로드, top_k 계산)
# =========================================================
def download_pil(url):
    last = None
    for _ in range(MAX_RETRY + 1):
        try:
            r = requests.get(url, timeout=REQ_TIMEOUT)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as e:
            last = e
    raise last

def get_topk(image_feat, text_feats, label_pairs, k):
    sim = (image_feat @ text_feats.T)[0]
    v, idxs = sim.topk(min(k, sim.shape[0]))
    out = []
    for j, ix in enumerate(idxs):
        lid, lname = label_pairs[int(ix)]
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
    # main_top: [(main_label_id, score), ...]

    # 2차 sub 후보 가져오기
    sub_candidates = []
    for mid, _ in main_top:
        sub_candidates.extend(sub_labels_by_parent.get(mid, []))

    if not sub_candidates:
        return main_top  # sub label 없음

    # sub 텍스트 임베딩
    sub_texts = [x[1] for x in sub_candidates]
    sub_tok = tokenizer(sub_texts).to(device)

    with torch.no_grad():
        sub_feat = model.encode_text(sub_tok)
        sub_feat /= sub_feat.norm(dim=-1, keepdim=True)

    sub_top = get_topk(img_feat, sub_feat, sub_candidates, TOPK_SUB)

    # 최종 5개 반환
    return main_top + sub_top

# =========================================================
# 6. OFFSET / LIMIT 입력 (행 순서 기반 처리)
# =========================================================
start_offset = int(input("처리 시작 행 번호(0부터): ").strip())
limit_count  = int(input("처리할 행 수(LIMIT): ").strip())

# =========================================================
# 7. place 테이블에서 OFFSET/LIMIT으로 가져오기
# =========================================================
cur.execute("""
    SELECT place_id, image_urls
    FROM place
    ORDER BY place_id
    OFFSET %s
    LIMIT %s
""", (start_offset, limit_count))

places = cur.fetchall()
print(f"대상 place 수: {len(places)}")

# =========================================================
# 8. place 단위 라벨링
# =========================================================
for place_id, urls_str in tqdm(places, desc="place 라벨링"):

    if urls_str is None:
        continue

    urls = [u.strip() for u in urls_str.split(",") if u.strip()]
    if not urls:
        continue

    # 라벨별 score 누적 bucket
    bucket = {}

    for url in urls:
        try:
            img = download_pil(url)
            label_scores = classify_image(img)  # [(lid, score), ...]

            for lid, sc in label_scores:
                bucket.setdefault(lid, []).append(sc)

        except Exception:
            continue

    if not bucket:
        continue

    # 평균 점수 계산
    avg_list = []
    for lid, arr in bucket.items():
        avg = sum(arr) / len(arr)
        if SCALE_TO_PERCENT:
            avg *= 100
        avg_list.append((lid, avg))

    avg_list.sort(key=lambda x: x[1], reverse=True)
    top3 = avg_list[:3]

    # 기존 place_label 삭제 후 새로 insert
    try:
        cur.execute("DELETE FROM place_label WHERE place_id = %s", (place_id,))

        rows_to_insert = [(place_id, lid, float(w), "model") for lid, w in top3]

        execute_values(cur, """
            INSERT INTO place_label (place_id, label_id, weight, source)
            VALUES %s
        """, rows_to_insert)

        conn.commit()

    except Exception as e:
        conn.rollback()
        print(f"DB 오류 place_id={place_id}: {e}")

cur.close()
conn.close()

print("완료.")