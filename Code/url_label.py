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

SCALE_TO_PERCENT = True
REQ_TIMEOUT = 10
MAX_RETRY = 2

# =========================================================
# 2. SigLIP / SigLIP-2 모델 자동 선택 로드
# =========================================================
device = "cuda" if torch.cuda.is_available() else "cpu"
all_pretrained = open_clip.list_pretrained()

siglip_pairs = [
    (m, p) for (m, p) in all_pretrained
    if ("siglip" in m.lower()) or ("siglip" in p.lower())
]

if not siglip_pairs:
    raise RuntimeError("SigLIP/SigLIP-2 모델을 찾을 수 없습니다.")

def _rank(pair):
    m, p = pair
    s = (m + " " + p).lower()
    score = 0
    if "siglip2" in s: score += 1000
    if "so400m" in s:  score += 200
    if "giant" in s:   score += 80
    if "large" in s:   score += 50
    for r in [512, 448, 384, 336, 256]:
        if str(r) in s:
            score += r
            break
    return score

siglip_pairs.sort(key=_rank, reverse=True)
MODEL_NAME, PRETRAINED = siglip_pairs[0]
print(f"[사용 모델] {MODEL_NAME} / pretrained={PRETRAINED}")

model, _, preprocess = open_clip.create_model_and_transforms(
    MODEL_NAME,
    pretrained=PRETRAINED
)
tokenizer = open_clip.get_tokenizer(MODEL_NAME)
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
cur.execute("""SELECT label_id, name, level, parent_id FROM label""")
rows = cur.fetchall()

main_labels = []
sub_labels_by_parent = {}

for lid, name, lv, pid in rows:
    if lv == 1:
        main_labels.append((lid, name))
    elif lv == 2 and pid is not None:
        sub_labels_by_parent.setdefault(pid, []).append((lid, name))

main_label_ids = [x[0] for x in main_labels]
main_label_text = [x[1] for x in main_labels]

# =========================================================
# 5. 라벨 임베딩 캐싱 관련 함수
# =========================================================
def load_label_embeddings_from_db(label_ids):
    cur.execute(
        "SELECT label_id, embedding FROM label_embedding WHERE label_id = ANY(%s)",
        (label_ids,)
    )
    result = cur.fetchall()
    emb_dict = {lid: torch.tensor(emb, dtype=torch.float32) for lid, emb in result}
    return emb_dict

def save_label_embeddings_to_db(emb_dict):
    rows = [(lid, emb.tolist()) for lid, emb in emb_dict.items()]
    execute_values(cur,
        """
        INSERT INTO label_embedding(label_id, embedding)
        VALUES %s
        ON CONFLICT(label_id) DO UPDATE SET embedding = EXCLUDED.embedding
        """,
        rows
    )
    conn.commit()

def compute_missing_embeddings(label_pairs):
    """
    label_pairs: [(label_id, label_name), ...]
    """
    missing_ids = []
    texts = []
    for lid, name in label_pairs:
        if lid not in cached_embeddings:
            missing_ids.append(lid)
            texts.append(name)

    if not missing_ids:
        return

    tok = tokenizer(texts).to(device)
    with torch.no_grad():
        feats = model.encode_text(tok)
        feats /= feats.norm(dim=-1, keepdim=True)

    # DB 저장 및 캐시 업데이트
    for lid, v in zip(missing_ids, feats):
        cached_embeddings[lid] = v.cpu()

    save_label_embeddings_to_db(
        {lid: cached_embeddings[lid] for lid in missing_ids}
    )

# =========================================================
# 6. 라벨 임베딩 로드 & 누락분 계산
# =========================================================
cached_embeddings = load_label_embeddings_from_db(main_label_ids)

# main 누락분 계산
compute_missing_embeddings(main_labels)

# main embedding 행렬 생성
main_features = torch.stack(
    [cached_embeddings[lid] for lid, _ in main_labels]
).to(device)

# =========================================================
# 7. 유틸 함수
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
    img = preprocess(pil_img).unsqueeze(0).to(device)
    with torch.no_grad():
        img_feat = model.encode_image(img)
        img_feat /= img_feat.norm(dim=-1, keepdim=True)

    # 1차 main top2
    main_top = get_topk(img_feat, main_features, main_labels, TOPK_MAIN)

    # 2차 관련 sub 후보
    sub_candidates = []
    for mid, _ in main_top:
        sub_candidates.extend(sub_labels_by_parent.get(mid, []))

    if not sub_candidates:
        return main_top

    sub_ids = [lid for lid, _ in sub_candidates]
    sub_missing = load_label_embeddings_from_db(sub_ids)
    for lid, emb in sub_missing.items():
        cached_embeddings[lid] = emb

    compute_missing_embeddings(sub_candidates)

    sub_features = torch.stack(
        [cached_embeddings[lid] for lid, _ in sub_candidates]
    ).to(device)

    sub_top = get_topk(img_feat, sub_features, sub_candidates, TOPK_SUB)

    return main_top + sub_top

# =========================================================
# 8. OFFSET / LIMIT 입력
# =========================================================
start_offset = int(input("처리 시작 행 번호(0부터): ").strip())
limit_count = int(input("처리할 행 수(LIMIT): ").strip())

# =========================================================
# 9. place 테이블 읽기
# =========================================================
cur.execute("""
    SELECT place_id, image_urls
    FROM place
    ORDER BY place_id
    OFFSET %s LIMIT %s
""", (start_offset, limit_count))
places = cur.fetchall()

print(f"대상 place 수: {len(places)}")

# =========================================================
# 10. place 단위 라벨링
# =========================================================
for place_id, urls_str in tqdm(places, desc="place 라벨링"):

    if urls_str is None:
        continue

    urls = [u.strip() for u in urls_str.split(",") if u.strip()]
    if not urls:
        continue

    bucket = {}

    for url in urls:
        try:
            img = download_pil(url)
            label_scores = classify_image(img)
            for lid, sc in label_scores:
                bucket.setdefault(lid, []).append(sc)
        except Exception:
            continue

    if not bucket:
        continue

    avg_list = []
    for lid, arr in bucket.items():
        avg = sum(arr) / len(arr)
        if SCALE_TO_PERCENT:
            avg *= 100
        avg_list.append((lid, avg))

    avg_list.sort(key=lambda x: x[1], reverse=True)
    top3 = avg_list[:3]

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
