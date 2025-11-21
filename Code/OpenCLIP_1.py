import torch
import open_clip
import pandas as pd
from PIL import Image
import os
from tqdm import tqdm

# ===============================
# 1. 모델 및 토크나이저 불러오기
# ===============================
device = "cuda" if torch.cuda.is_available() else "cpu"

model, _, preprocess = open_clip.create_model_and_transforms(
    'ViT-B-32', pretrained='openai'
)
tokenizer = open_clip.get_tokenizer('ViT-B-32')
model.to(device).eval()

# ===============================
# 2. 라벨 CSV 불러오기
# ===============================
label_path = r"C:\Users\jin01\Videos\Captures\DGU\2025-2\DSCD\Project\2025-2-DSCD-San3Su1-03\Code\label.csv"
label_df = pd.read_csv(label_path, dtype=str)
labels = label_df.iloc[:, 0].dropna().tolist()

# ===============================
# 3. 이미지 폴더 내 파일 불러오기
# ===============================
image_dir = r"C:\Users\jin01\Videos\Captures\DGU\2025-2\DSCD\Project\2025-2-DSCD-San3Su1-03\Code\ai_test_images"

image_list = [
    os.path.join(image_dir, f)
    for f in os.listdir(image_dir)
    if f.lower().endswith((".jpg", ".jpeg", ".png"))
]

# ===============================
# 4. 라벨링 함수 (Softmax 제거, top_k=3)
# ===============================
def get_topk_labels(model, tokenizer, preprocess, image, label_list, top_k=3):
    text_tokens = tokenizer(label_list).to(device)

    with torch.no_grad():
        image_tensor = preprocess(image).unsqueeze(0).to(device)
        image_features = model.encode_image(image_tensor)
        text_features = model.encode_text(text_tokens)

        # Normalize
        image_features /= image_features.norm(dim=-1, keepdim=True)
        text_features /= text_features.norm(dim=-1, keepdim=True)

        # Raw cosine similarity
        similarity = (image_features @ text_features.T)[0]

        values, indices = similarity.topk(top_k)

    return [(label_list[i], float(values[j])) for j, i in enumerate(indices)]

# ===============================
# 5. OpenCLIP 1차 라벨링
# ===============================
results = []
for idx, img_path in enumerate(tqdm(image_list, desc="OpenCLIP 1차 라벨링(Softmax 제거, top3)")):
    try:
        image = Image.open(img_path).convert("RGB")
    except Exception as e:
        print(f"[경고] 이미지 로드 실패: {img_path} ({e})")
        continue

    top_labels = get_topk_labels(model, tokenizer, preprocess, image, labels, top_k=3)

    labels_only = [label for label, _ in top_labels]
    scores_only = [score for _, score in top_labels]

    results.append({
        "index": idx,
        "filename": os.path.basename(img_path),

        "label1": labels_only[0],
        "score1": round(scores_only[0], 4),

        "label2": labels_only[1],
        "score2": round(scores_only[1], 4),

        "label3": labels_only[2],
        "score3": round(scores_only[2], 4),
    })

# ===============================
# 6. 결과 CSV 저장
# ===============================
df = pd.DataFrame(results)
output_path = os.path.join(os.path.dirname(label_path), "Open_1th_output.csv")
df.to_csv(output_path, index=False, encoding="utf-8-sig")

print(f"1차 라벨링(top3) 완료 → {output_path}")
