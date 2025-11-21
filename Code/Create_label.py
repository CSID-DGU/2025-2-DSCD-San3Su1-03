import os
import re
import time
import pandas as pd
from tqdm import tqdm
from openai import OpenAI

# ------------------------------
# 설정
# ------------------------------
MODEL = "gpt-4o"
MAX_RETRIES = 5
REQUESTS_PER_MIN = 40
SLEEP_BETWEEN_CALLS = 60.0 / REQUESTS_PER_MIN

BASE = r"C:/Users/jin01/Videos/Captures/DGU/2025-2/DSCD/Project/2025-2-DSCD-San3Su1-03/Code"
LABEL_CSV = os.path.join(BASE, "label.csv")
OUT_CSV = os.path.join(BASE, "Clip_label_1.csv")  # 결과 저장 경로

# ------------------------------
# 1. 라벨 로드
# ------------------------------
def load_labels_from_csv(path):
    df = pd.read_csv(path, header=None)
    labels = [str(x).strip() for x in df.iloc[:, 0].tolist() if str(x).strip()]
    print(f"총 {len(labels)}개 라벨 로드 완료 (예시: {labels[:5]})")
    return labels

# ------------------------------
# 2. GPT 프롬프트
# ------------------------------
SYSTEM_PROMPT = """
You are an expert travel photographer, location scout, and visual data curator.
Your job is to generate high-quality English sublabels for place categories.

Guidelines:
- Focus on visually distinct, photo-worthy variations of the location.
- Prefer scenery, atmosphere, architecture, natural features, and vantage points.
- Avoid: people, food, animals close-up, menus, shops, subjective emotions.
- Avoid generic or repetitive descriptors.
- Assume these labels will be used for CLIP-based machine learning image tagging.
- Output should reflect visual styles found in global travel photography.
"""

USER_PROMPT_TEMPLATE = """
Main place category: {category}

Generate exactly 10 short English sublabels.
Purpose: image labeling for travel photography and CLIP-based visual tagging.

Rules:
- 3–8 words each
- No numbering, no bullets
- Each line must be a distinct photo-worthy variation *of or around* the place
- Focus on scenery, atmosphere, structure, or vantage points
- Avoid: people, food, stores, events, crowds, subjective adjectives
- Avoid indoor-only contexts unless the main place is inherently indoors
- Avoid vague labels like “beautiful view”

Output format:
10 lines, each containing one sublabel only.
"""

def normalize_category(cat):
    return re.sub(r"\s+", " ", cat.replace("_", " ").replace("/", " ").strip())

# ------------------------------
# 3. GPT 호출 함수
# ------------------------------
def call_gpt(client, category):
    user_prompt = USER_PROMPT_TEMPLATE.format(category=normalize_category(category))
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.8,
            )
            text = resp.choices[0].message.content.strip()
            lines = [re.sub(r"^\d+[\).\-\s]+", "", ln).strip()
                     for ln in text.splitlines() if ln.strip()]
            return lines[:10]
        except Exception as e:
            print(f"[{category}] API 오류: {e} (시도 {attempt}/{MAX_RETRIES})")
            if attempt == MAX_RETRIES:
                base = normalize_category(category)
                return [
                    f"beautiful {base} at sunset",
                    f"quiet morning view of {base}",
                    f"panoramic shot of {base}",
                    f"scenic {base} by the sea",
                    f"{base} under night sky",
                    f"misty {base} at dawn",
                    f"vivid landscape near {base}",
                    f"reflection of {base} on water",
                    f"wide view from {base}",
                    f"calm surroundings of {base}",
                ]
            time.sleep(2 ** attempt * 0.5)

# ------------------------------
# 4. 메인 실행부
# ------------------------------
def main():
    client = OpenAI(api_key="")  # 환경 변수 사용 권장
    labels = load_labels_from_csv(LABEL_CSV)
    rows = []

    for i, cat in enumerate(tqdm(labels, desc="Generating sublabels")):
        try:
            subs = call_gpt(client, cat)
            row = {"Main_Label": cat}
            for j in range(10):
                row[f"Sub{j+1}"] = subs[j] if j < len(subs) else ""
            rows.append(row)
            time.sleep(SLEEP_BETWEEN_CALLS)
        except Exception as e:
            print(f"[{cat}] 전체 루프 오류: {e}")
            continue

    pd.DataFrame(rows).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n=== 저장 완료 ===\n{OUT_CSV}")

# ------------------------------
if __name__ == "__main__":
    main()
