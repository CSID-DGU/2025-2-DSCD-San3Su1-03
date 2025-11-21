import os
import pandas as pd
from openai import OpenAI

# ===============================
# 1. OpenAI API Key
# ===============================
client = OpenAI(api_key="")  # 여기에 API 키 입력

# ===============================
# 2. 이미지 폴더 경로
# ===============================
image_dir = r"C:\Users\jin01\Videos\Captures\DGU\2025-2\DSCD\Project\2025-2-DSCD-San3Su1-03\Code\ai_test_images"

# ===============================
# 3. 이미지 첫 단어(한글 라벨) 자동 수집
# ===============================
kor_labels = set()  # 중복 제거용

for filename in os.listdir(image_dir):
    if filename.lower().endswith((".jpg", ".jpeg", ".png")):
        first = filename.split("_")[0].strip()
        kor_labels.add(first)

kor_labels = sorted(list(kor_labels))
print("추출된 한글 라벨:", kor_labels)

# ===============================
# 4. GPT로 한국어 → 영어 번역
# ===============================
eng_labels = []

for label in kor_labels:
    prompt = f"Translate the following place/category into English : '{label}'. Return ONLY the translation."

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    eng = response.choices[0].message.content.strip()
    eng_labels.append(eng)

    print(f"{label} → {eng}")

# ===============================
# 5. CSV 저장
# ===============================
df = pd.DataFrame({"kor_label": kor_labels, "eng_label": eng_labels})

save_path = os.path.join(os.path.dirname(image_dir), "Kor_to_Eng_label.csv")
df.to_csv(save_path, index=False, encoding="utf-8-sig")

print(f"\nKor_to_Eng_label.csv 생성 완료 → {save_path}")
