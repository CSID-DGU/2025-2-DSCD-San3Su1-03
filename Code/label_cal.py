import os
import torch
import open_clip
import pandas as pd
from tqdm import tqdm

# ===============================
# 1. 모델 로드
# ===============================
device = "cuda" if torch.cuda.is_available() else "cpu"

model, _, preprocess = open_clip.create_model_and_transforms(
    'ViT-B-32', pretrained='openai'
)
tokenizer = open_clip.get_tokenizer('ViT-B-32')
model.to(device).eval()

# ===============================
# 2. 경로 설정
# ===============================
BASE = r"C:\Users\jin01\Videos\Captures\DGU\2025-2\DSCD\Project\2025-2-DSCD-San3Su1-03\Code"

image_dir = os.path.join(BASE, "ai_test_images")
label_csv = os.path.join(BASE, "label.csv")
kor2eng_csv = os.path.join(BASE, "Kor_to_Eng_label.csv")  # ★ 추가
out_csv   = os.path.join(BASE, "similar_name.csv")

# ===============================
# 3. 라벨 로드
# ===============================
label_df = pd.read_csv(label_csv, dtype=str)
labels = label_df.iloc[:, 0].dropna().tolist()

# Kor→Eng 변환표 로드
kor_eng_df = pd.read_csv(kor2eng_csv, dtype=str)
kor2eng = {row["kor_label"]: row["eng_label"] for _, row in kor_eng_df.iterrows()}

def convert_kor_to_eng(text):
    text = text.strip()
    return kor2eng.get(text, text)  # 매핑 없는 경우 원문 그대로

# ===============================
# 4. 이미지 파일 목록
# ===============================
image_list = [
    f for f in os.listdir(image_dir)
    if f.lower().endswith((".jpg", ".jpeg", ".png"))
]

# ===============================
# 5. CLIP 유사도 계산 함수
# ===============================
def get_topk_from_text(model, tokenizer, text_query, label_list, top_k=3):

    with torch.no_grad():
        query_token = tokenizer([text_query]).to(device)
        query_feat = model.encode_text(query_token)
        query_feat /= query_feat.norm(dim=-1, keepdim=True)

        label_tokens = tokenizer(label_list).to(device)
        label_feats = model.encode_text(label_tokens)
        label_feats /= label_feats.norm(dim=-1, keepdim=True)

        sim = (query_feat @ label_feats.T)[0]
        top_values, top_indices = sim.topk(min(top_k, len(label_list)))

    return [(label_list[i], top_values[j].item()) for j, i in enumerate(top_indices)]

# ===============================
# 6. 파일명 기반 유사도 계산
# ===============================
results = []

for filename in tqdm(image_list, desc="파일명 기반 유사도 계산 중"):
    try:
        main_kor = filename.split("_")[0].strip()
    except:
        print(f"[경고] 파일명 파싱 실패: {filename}")
        continue

    # ★ 한글 → 영어 변환
    main_eng = convert_kor_to_eng(main_kor)

    # CLIP text similarity
    top3 = get_topk_from_text(model, tokenizer, main_eng, labels, top_k=3)

    labels_only = [t[0] for t in top3]
    scores_only = [t[1] for t in top3]

    results.append({
        "파일명": filename,
        "원래분류명(한글)": main_kor,
        "변환된분류명(영어)": main_eng,
        "소분류1": labels_only[0],
        "확률1(%)": round(scores_only[0] * 100, 2),
        "소분류2": labels_only[1],
        "확률2(%)": round(scores_only[1] * 100, 2),
        "소분류3": labels_only[2],
        "확률3(%)": round(scores_only[2] * 100, 2),
    })

# ===============================
# 7. CSV 저장
# ===============================
df = pd.DataFrame(results)
df.to_csv(out_csv, index=False, encoding="utf-8-sig")

print(f"\nsimilar_name.csv 생성 완료 → {out_csv}")
