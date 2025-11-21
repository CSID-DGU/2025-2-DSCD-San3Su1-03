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
    "host": "YOUR_HOST",
    "port": 5432,
    "dbname": "YOUR_DBNAME",
    "user": "YOUR_USER",
    "password": "YOUR_PASSWORD",
}

# place_id 범위는 실행 시 직접 입력받음
# top_k 설정 (당신이 말한 그대로)
TOPK_MAIN = 2
TOPK_SUB  = 3

# cosine similarity 값을 퍼센트로 저장할지 여부
# 예시(weight=91.22)처럼 쓰려면 True 권장
SCALE_TO_PERCENT = True

# 이미지 다운로드 타임아웃/재시도 옵션
REQ_TIMEOUT = 10
MAX_RETRY = 2

# =========================================================
# 1. OpenCLIP 모델 로드
# =========================================================
device = "cuda" if torch.cuda.is_available() else "cpu"
model, _, preprocess = open_clip.create_model_and_transforms('ViT-B-32', pretrained='openai')
tokenizer = open_clip.get_tokenizer('ViT-B-32')
model.to(device).eval()

# =========================================================
# 2. DB 연결
# =========================================================
conn = psycopg2.connect(**DB_CONFIG)
conn.autocommit = False  # 트랜잭션 수동 관리
cur = conn.cursor()

# =========================================================
# 3. label 테이블에서 main/sub 라벨 불러오기
#    - main: level=1
#    - sub : level=2, parent_id로 묶음
# =========================================================
cur.execute("""
    SELECT label_id, name, level, parent_id
    FROM label
""")
label_rows = cur.fetchall()

main_labels = []              # [(label_id, name), ...]
sub_labels_by_parent = {}     # parent_id -> [(label_id, name), ...]

for label_id, name, level, parent_id in label_rows:
    if level == 1:
        main_labels.append((label_id, name))
    elif level == 2 and parent_id is not None:
        sub_labels_by_parent.setdefault(parent_id, []).append((label_id, name))

main_label_ids  = [x[0] for x in main_labels]
main_label_text = [x[1] for x in main_labels]

# main 라벨 텍스트 토큰은 미리 만들어두면 빨라짐
main_text_tokens = tokenizer(main_label_text).to(device)
with torch.no_grad():
    main_text_features = model.encode_text(main_text_tokens)
    main_text_features /= main_text_features.norm(dim=-1, keepdim=True)

# =========================================================
# 4. 유틸 함수
# =========================================================
def download_image_pil(url: str):
    """파일 저장 없이 URL -> PIL.Image 로 바로 로드"""
    last_err = None
    for _ in range(MAX_RETRY + 1):
        try:
            r = requests.get(url, timeout=REQ_TIMEOUT)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as e:
            last_err = e
    raise last_err

def topk_from_features(image_feature, text_features, label_pairs, top_k):
    """
    image_feature: (1, d)
    text_features: (n, d)
    label_pairs  : [(label_id, name), ...]
    """
    sim = (image_feature @ text_features.T)[0]
    values, indices = sim.topk(min(top_k, sim.shape[0]))
    out = []
    for j, idx in enumerate(indices):
        lid, lname = label_pairs[int(idx)]
        out.append((lid, lname, float(values[j])))
    return out

def compute_labels_for_one_image(pil_img):
    """
    1차(main top2) + 2차(sub top3) 통합하여
    [(label_id, score), ...] 5개 반환
    """
    # 이미지 임베딩
    image_tensor = preprocess(pil_img).unsqueeze(0).to(device)
    with torch.no_grad():
        image_feature = model.encode_image(image_tensor)
        image_feature /= image_feature.norm(dim=-1, keepdim=True)

    # 1차 main top2
    main_top = topk_from_features(
        image_feature,
        main_text_features,
        main_labels,
        TOPK_MAIN
    )  # [(main_label_id, name, score), ...]

    # main top2의 parent_id에 해당하는 sub 후보 모으기
    sub_candidates = []
    for mid, _, _ in main_top:
        sub_candidates.extend(sub_labels_by_parent.get(mid, []))

    # sub 후보가 없으면 main 결과만 반환
    if not sub_candidates:
        return [(mid, mscore) for mid, _, mscore in main_top]

    sub_text = [x[1] for x in sub_candidates]
    sub_tokens = tokenizer(sub_text).to(device)

    with torch.no_grad():
        sub_features = model.encode_text(sub_tokens)
        sub_features /= sub_features.norm(dim=-1, keepdim=True)

    sub_top = topk_from_features(
        image_feature,
        sub_features,
        sub_candidates,
        TOPK_SUB
    )  # [(sub_label_id, name, score), ...]

    # 최종 5개 (main 2 + sub 3)
    final = [(mid, mscore) for mid, _, mscore in main_top] + \
            [(sid, sscore) for sid, _, sscore in sub_top]
    return final

# =========================================================
# 5. place_id 범위 입력
# =========================================================
start_pid = int(input("처리 시작 place_id 입력: ").strip())
end_pid   = int(input("처리 끝 place_id 입력: ").strip())

# =========================================================
# 6. place 테이블에서 대상 place 가져오기
# =========================================================
cur.execute("""
    SELECT place_id, image_urls
    FROM place
    WHERE place_id BETWEEN %s AND %s
    ORDER BY place_id ASC
""", (start_pid, end_pid))
places = cur.fetchall()

print(f"대상 place 수: {len(places)}")

# =========================================================
# 7. place 단위 라벨링 -> place_label 저장
# =========================================================
for place_id, image_urls in tqdm(places, desc="place 단위 라벨링"):

    if image_urls is None or str(image_urls).strip() == "":
        continue

    # URL split
    urls = [u.strip() for u in str(image_urls).split(",") if u.strip()]
    if not urls:
        continue

    # label_id -> [scores...]
    score_bucket = {}

    # 모든 이미지 처리
    for url in urls:
        try:
            pil_img = download_image_pil(url)
            label_scores = compute_labels_for_one_image(pil_img)

            # 누적
            for lid, sc in label_scores:
                score_bucket.setdefault(lid, []).append(sc)

        except Exception as e:
            print(f"[경고] place_id={place_id} URL 처리 실패: {url} ({e})")
            continue

    if not score_bucket:
        continue

    # 라벨별 평균 계산
    avg_scores = []
    for lid, sc_list in score_bucket.items():
        avg = sum(sc_list) / len(sc_list)
        if SCALE_TO_PERCENT:
            avg *= 100.0
        avg_scores.append((lid, avg))

    # 평균 상위 3개 선택
    avg_scores.sort(key=lambda x: x[1], reverse=True)
    top3 = avg_scores[:3]

    # 기존 place_label 삭제 후 insert
    try:
        cur.execute("DELETE FROM place_label WHERE place_id = %s", (place_id,))

        insert_rows = [(place_id, lid, float(w), "model") for lid, w in top3]

        execute_values(cur, """
            INSERT INTO place_label (place_id, label_id, weight, source)
            VALUES %s
        """, insert_rows)

        conn.commit()

    except Exception as e:
        conn.rollback()
        print(f"[오류] place_id={place_id} DB 저장 실패 ({e})")
        continue

print("완료.")
cur.close()
conn.close()
