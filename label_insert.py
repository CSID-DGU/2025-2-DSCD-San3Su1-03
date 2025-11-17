# pip install pandas sqlalchemy psycopg2-binary
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

ENGINE_URL = "postgresql+psycopg2://postgres:dscd1234@dscd.czuosc4sm6fc.ap-northeast-2.rds.amazonaws.com:5432/life-recorder"
CSV_PATH   = "./Code/Clip_label_1.csv"

def clean(s):
    if pd.isna(s): return None
    s = str(s).replace("\u200b", "").strip()
    return s or None

def run():
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    cols = [c.strip() for c in df.columns]
    main_col = cols[0]                # Main_Label
    sub_cols = [c for c in cols[1:11]]  # Sub1~Sub10

    df[main_col] = df[main_col].map(clean)
    for c in sub_cols:
        if c in df.columns:
            df[c] = df[c].map(clean)

    eng = create_engine(ENGINE_URL, pool_pre_ping=True)

    with Session(eng) as ses:
        for _, row in df.iterrows():
            main_name = row[main_col]
            if not main_name:
                continue

            # 1) 대분류: 먼저 조회 → 없으면 INSERT … RETURNING
            pid = ses.execute(
                text("SELECT label_id FROM label WHERE level=1 AND name=:n"),
                {"n": main_name}
            ).scalar()
            if pid is None:
                pid = ses.execute(
                    text("""
                        INSERT INTO label(name, level, parent_id)
                        VALUES (:n, 1, NULL)
                        RETURNING label_id
                    """),
                    {"n": main_name}
                ).scalar()

            # 2) 소분류: 각 Sub에 대해 부모 고정으로 동일 패턴
            for c in sub_cols:
                sub = row.get(c)
                if not sub:
                    continue

                sid = ses.execute(
                    text("""
                        SELECT label_id FROM label
                        WHERE level=2 AND parent_id=:pid AND name=:n
                    """),
                    {"pid": pid, "n": sub}
                ).scalar()
                if sid is None:
                    ses.execute(
                        text("""
                            INSERT INTO label(name, level, parent_id)
                            VALUES (:n, 2, :pid)
                        """),
                        {"n": sub, "pid": pid}
                    )

        ses.commit()

    print("완료: 순차 입력 + 정확한 parent_id로 저장됐습니다.")

if __name__ == "__main__":
    run()
