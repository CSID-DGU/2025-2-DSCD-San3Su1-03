import os
import pandas as pd

# ===============================
# 1. 경로 설정
# ===============================
BASE = r"C:\Users\jin01\Videos\Captures\DGU\2025-2\DSCD\Project\2025-2-DSCD-San3Su1-03\Code"

open2_csv   = os.path.join(BASE, "Open_2th_output_no_softmax.csv")
similar_csv = os.path.join(BASE, "similar_name.csv")
clip_csv    = os.path.join(BASE, "Clip_label_1.csv")

# 임계값
THRESHOLD = 0.25

# ===============================
# 2. CSV 로드
# ===============================
df2 = pd.read_csv(open2_csv, dtype=str)
df_sim = pd.read_csv(similar_csv, dtype=str)
df_clip = pd.read_csv(clip_csv, dtype=str)

# 숫자 변환
for col in ["점수1", "점수2", "점수3", "점수4", "점수5"]:
    if col in df2.columns:
        df2[col] = df2[col].astype(float)

df_sim["확률1(%)"] = df_sim["확률1(%)"].astype(float)

sub_cols = [c for c in df_clip.columns if c.startswith("Sub")]

# ===============================
# 3. 소분류 → 1차 라벨 매핑
# ===============================
def find_main_label(sub_label):
    sub_label = str(sub_label).strip().lower()
    for _, row in df_clip.iterrows():
        main_label = row["Main_Label"]
        for c in sub_cols:
            val = row[c]
            if pd.notna(val) and str(val).strip().lower() == sub_label:
                return main_label
    return None

# ===============================
# 4. 비교 수행 (임계값 넘는 모든 소분류 검사)
# ===============================
same_count = 0
total = 0
below_threshold = 0

for _, row in df2.iterrows():
    filename = row["파일명"]

    # similar_name.csv에서 동일 파일 찾아오기
    sim_row = df_sim[df_sim["파일명"] == filename]
    if sim_row.empty:
        continue
    sim_label = sim_row.iloc[0]["소분류1"].strip().lower()

    # 임계값 넘는 모든 소분류 후보 추출 (1~5)
    sub_candidates = []
    for j in range(1, 6):  # ★ 여기만 5까지 확장됨
        sub_col = f"소분류{j}"
        score_col = f"점수{j}"
        if sub_col in row and score_col in row:
            score_val = float(row[score_col])
            if score_val >= THRESHOLD:
                sub_candidates.append(str(row[sub_col]).strip())

    if len(sub_candidates) == 0:
        below_threshold += 1
        continue

    # 소분류 후보들을 —> 1차 라벨로 모두 매핑
    main_labels = []
    for sub in sub_candidates:
        ml = find_main_label(sub)
        if ml is not None:
            main_labels.append(ml.strip().lower())

    if len(main_labels) == 0:
        continue

    total += 1

    # 하나라도 겹치면 통과
    if sim_label in main_labels:
        same_count += 1

# ===============================
# 5. 출력
# ===============================
print(f"같은 갯수 : {same_count}/{total}")
print(f"임계값 미만 : {below_threshold}개")