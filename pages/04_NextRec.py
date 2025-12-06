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
import random

def apply_ui():
    from core.ui import hide_default_nav
    hide_default_nav()
    from app import render_sidebar as _render_sidebar
    _render_sidebar()

apply_ui() 

st.title("📍 업로드 사진기반 다음 장소 추천")

# Upload 페이지에서 선택한 S3 key들
keys = st.session_state.get("selected_image_keys", [])
if not keys:
    st.info("먼저 Upload 페이지에서 이미지를 업로드해주세요.")
    time.sleep(1)
    st.switch_page("pages/00_Upload.py")
    st.stop()

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

def get_photo_id_by_s3key(cur, key: str) -> int | None:
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
    selected_main_ids = [mid for mid, _ in main_top]

    # 2차: 선택된 대분류의 소분류만 후보로 사용
    sub_candidates = []
    for mid in selected_main_ids:
        sub_candidates.extend(sub_labels_by_parent.get(mid, []))

    # 소분류 후보가 없으면? 아무 label도 반환하지 않음
    if not sub_candidates:
        return []

    # sub 텍스트 임베딩
    sub_texts = [x[1] for x in sub_candidates]
    sub_tok = tokenizer(sub_texts).to(device)
    with torch.no_grad():
        sub_feat = model.encode_text(sub_tok)
        sub_feat /= sub_feat.norm(dim=-1, keepdim=True)

    sub_top = get_topk(img_feat, sub_feat, sub_candidates, TOPK_SUB)

    return sub_top   # 총 3개

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


# 1) 사진에 달린 라벨 3개 가져오기 (photo_label → label_id)
def get_photo_labels(cur, photo_id: int, limit: int = 3) -> list[int]:
    cur.execute(
        """
        SELECT label_id
        FROM photo_label
        WHERE photo_id = %s
        ORDER BY rank ASC   -- rank가 1,2,3이라면 우선순위 기준
        LIMIT %s
        """,
        (photo_id, limit)
    )
    rows = cur.fetchall()
    return [r[0] for r in rows]


# 2) 주어진 label_id 목록 중에서, 최소 N개 이상을 포함하는 place 찾기
def find_places_matching_labels(cur, label_ids: list[int], min_match: int) -> list[int]:
    """
    label_ids 중 최소 min_match 개 이상의 label_id를 가진 place_id 리스트 반환
    """
    if len(label_ids) < min_match:
        return []

    # IN (%s, %s, %s) 형태로 사용하기 위해 tuple로 캐스팅
    cur.execute(
        f"""
        SELECT place_id, COUNT(DISTINCT label_id) AS match_cnt
        FROM place_label
        WHERE label_id IN %s
        GROUP BY place_id
        HAVING COUNT(DISTINCT label_id) >= %s
        """,
        (tuple(label_ids), min_match)
    )
    rows = cur.fetchall()
    return [r[0] for r in rows]


# 3) 최종 추천 함수: 3개 매치 → 없으면 2개 매치 → 랜덤 3개 샘플링
def recommend_places_for_photo(cur, photo_id: int, max_results: int = 3):
    # 1) 이 사진의 라벨 3개 가져오기
    label_ids = get_photo_labels(cur, photo_id, limit=3)
    if not label_ids:
        return []

    # 2) 먼저 3개 모두 매칭되는 place 찾기
    place_ids = find_places_matching_labels(cur, label_ids, min_match=3)

    # 3개 모두 매칭되는 곳이 없다면, 최소 2개 이상 매칭되는 place로 완화
    if not place_ids:
        place_ids = find_places_matching_labels(cur, label_ids, min_match=2)

    # 그래도 없다면 추천 불가
    if not place_ids:
        return []

    # 3) 후보가 많으면 랜덤으로 max_results개만 샘플링
    if len(place_ids) > max_results:
        selected_place_ids = random.sample(place_ids, max_results)
    else:
        selected_place_ids = place_ids

    # 4) place 테이블에서 name, address 가져오기
    cur.execute(
        """
        SELECT place_id, name, address
        FROM place
        WHERE place_id IN %s
        """,
        (tuple(selected_place_ids),)
    )
    rows = cur.fetchall()

    # 선택된 순서를 유지하기 위해 map 후 재정렬
    info_map = {pid: (name, addr) for pid, name, addr in rows}
    results = []
    for pid in selected_place_ids:
        if pid in info_map:
            name, addr = info_map[pid]
            results.append(
                {"place_id": pid, "name": name, "address": addr}
            )

    return results

def collect_recommended_places_for_photos(cur, photo_ids, max_results=3):
    """
    여러 사진(photo_ids)에 대해 각각 추천 후보 place를 모아서
    최종적으로 랜덤 max_results개만 반환.
    """
    all_place_ids = set()

    for pid in photo_ids:
        # 이 사진의 라벨 3개
        label_ids = get_photo_labels(cur, pid, limit=3)
        if not label_ids:
            continue

        # 1) 3개 모두 매칭되는 place
        p3 = find_places_matching_labels(cur, label_ids, min_match=3)

        # 2) 없으면 2개 이상 매칭되는 place
        if not p3:
            p2 = find_places_matching_labels(cur, label_ids, min_match=2)
        else:
            p2 = []

        for x in p3 + p2:
            all_place_ids.add(x)

    if not all_place_ids:
        return []

    place_ids = list(all_place_ids)
    if len(place_ids) > max_results:
        selected_place_ids = random.sample(place_ids, max_results)
    else:
        selected_place_ids = place_ids

    # place 테이블에서 name, address 조회
    cur.execute(
        """
        SELECT place_id, name, address
        FROM place
        WHERE place_id IN %s
        """,
        (tuple(selected_place_ids),)
    )
    rows = cur.fetchall()

    info_map = {pid: (name, addr) for pid, name, addr in rows}
    results = []
    for pid in selected_place_ids:
        if pid in info_map:
            name, addr = info_map[pid]
            results.append(
                {"place_id": pid, "name": name, "address": addr}
            )
    return results


def get_recommendations(cur, photo_label_list):
    """
    photo_label_list: 각 사진마다 top3 label들 (list[list[label_id]])
    예) [[101,102,103], [201,202,203], ...]

    return:
        strong_set: 강력추천 place_id (사진당 3개 매치된 장소)
        medium_set: 중간추천 place_id (사진당 3개 매치 없을 때 2개 매치된 장소)
    """

    strong_set = set()
    medium_set = set()

    for labels in photo_label_list:   # 사진별 탐색
        a, b, c = labels  # 소분류 3개 (이미 top3로 들어있다고 가정)

        # 1) 🔥 라벨 3개 모두 매치되는 장소 탐색
        cur.execute("""
            SELECT place_id
            FROM place_label
            WHERE label_id IN (%s, %s, %s)
            GROUP BY place_id
            HAVING COUNT(place_id) = 3
        """, (a, b, c))

        rows3 = cur.fetchall()

        if rows3:
            # 3개 매치되는 장소 있는 경우 → 강력추천에 추가하고 2개 탐색 스킵
            for (pid,) in rows3:
                strong_set.add(pid)
            continue  # ❗ 다음 사진으로 넘어감

        # 2) ✨ 2개 매치되는 장소 탐색 (3개 없을 때만)
        cur.execute("""
            SELECT place_id
            FROM place_label
            WHERE label_id IN (%s, %s, %s)
            GROUP BY place_id
            HAVING COUNT(place_id) = 2
        """, (a, b, c))

        rows2 = cur.fetchall()

        if rows2:
            for (pid,) in rows2:
                medium_set.add(pid)

    return strong_set, medium_set


# =========================================================
# 7. Streamlit UI + session_state 기반 실행
# =========================================================

st.write(f"선택된 이미지 개수: {len(keys)}")

storage = get_storage()  # S3Storage

if st.button("장소 추천 확인하기"):
    conn = get_conn()
    cur = conn.cursor()

    try:
        labeled_photo_ids: list[int] = []
        label_triplets: list[list[int]] = []   # 사진별 top3 label_id 저장

        # 1) 선택된 이미지들 라벨링 + photo_label 저장
        with st.spinner("📷 사진을 분석하고 있어요... 잠시만 기다려 주세요!"):
            for key in keys:
                # photos 테이블에서 PK(id) 찾기
                photo_id = get_photo_id_by_s3key(cur, key)
                if photo_id is None:
                    st.warning(f"photos 테이블에 key={key} 인 row가 없습니다. 건너뜁니다.")
                    continue

                # S3에서 이미지 로드
                try:
                    raw = storage.get(key).read()
                    pil_img = Image.open(io.BytesIO(raw)).convert("RGB")
                except Exception as e:
                    st.warning(f"이미지 로딩 실패: {key} / {e}")
                    continue

                # OpenCLIP 라벨링 (소분류 TOP3만 반환하도록 구현돼 있다고 가정)
                label_scores = classify_image(pil_img)  # [(label_id, score), ...]

                if not label_scores:
                    st.warning(f"라벨링 결과 없음: {key}")
                    continue

                # 이미 TOP3만 들어온다 가정, 혹시 모를 경우 상위 3개만 자르기
                top3 = sorted(label_scores, key=lambda x: x[1], reverse=True)[:3]

                # DB photo_label 적재
                insert_photo_labels(cur, photo_id, top3)

                labeled_photo_ids.append(photo_id)
                label_triplets.append([lid for (lid, _) in top3])

        if not labeled_photo_ids:
            conn.rollback()
            st.error("라벨링에 성공한 사진이 없습니다. 추천을 진행할 수 없습니다.")
            cur.close(); conn.close()
            st.stop()

        conn.commit()
        st.success("업로드된 사진들에 대한 라벨링이 완료되었습니다. 라벨과 매치되는 장소를 추천해드릴게요.")

        # 2) 사진별로 3개/2개 매치 장소 찾기
        strong_ids: set[int] = set()   # 라벨 3개 모두 매치된 place_id들
        medium_ids: set[int] = set()   # 3개 매치는 없고, 2개 매치된 place_id들

        with st.spinner("📍 장소를 추천 중입니다... 조금만 기다려 주세요!"):
            for labels in label_triplets:
                if len(labels) < 3:
                    continue
                a, b, c = labels

                # 🔥 1) 라벨 3개 모두 매치되는 장소
                cur.execute(
                    """
                    SELECT place_id
                    FROM place_label
                    WHERE label_id IN (%s, %s, %s)
                    GROUP BY place_id
                    HAVING COUNT(DISTINCT label_id) = 3
                    """,
                    (a, b, c),
                )
                rows3 = cur.fetchall()
                if rows3:
                    for (pid,) in rows3:
                        strong_ids.add(pid)
                    # 3개 매치가 있으면 이 사진은 여기서 끝 → 2개 매치는 보지 않고 다음 사진으로
                    continue

                # ✨ 2) 3개 매치가 없을 때만 2개 매치 탐색
                cur.execute(
                    """
                    SELECT place_id
                    FROM place_label
                    WHERE label_id IN (%s, %s, %s)
                    GROUP BY place_id
                    HAVING COUNT(DISTINCT label_id) = 2
                    """,
                    (a, b, c),
                )
                rows2 = cur.fetchall()
                for (pid,) in rows2:
                    # 얘는 strong에 안 올라간 애들만 medium에 넣기
                    if pid not in strong_ids:
                        medium_ids.add(pid)

        strong_total = len(strong_ids)
        medium_total = len(medium_ids)

        # 3) tier별로 최대 3개만 랜덤 샘플링
        def sample_ids(id_set: set[int], k: int = 3) -> list[int]:
            ids = list(id_set)
            if len(ids) > k:
                return random.sample(ids, k)
            return ids

        strong_sample = sample_ids(strong_ids, 3)
        medium_sample = sample_ids(medium_ids, 3)

        # 4) place 테이블에서 name, address 조회
        query_ids = strong_sample + [pid for pid in medium_sample if pid not in strong_sample]
        place_info: dict[int, tuple[str, str]] = {}

        if query_ids:
            cur.execute(
                """
                SELECT place_id, name, address
                FROM place
                WHERE place_id IN %s
                """,
                (tuple(query_ids),),
            )
            for pid, name, address in cur.fetchall():
                place_info[pid] = (name, address)

        # 5) 화면 출력
        if not strong_sample and not medium_sample:
            st.info("업로드된 사진들에 맞는 추천 장소를 찾지 못했어요.")
        else:
            if strong_sample:
                st.subheader("🔥 강력 추천 (라벨 3개 매치)")
                st.caption(f"총 {strong_total}개 중 랜덤 {len(strong_sample)}개를 보여줍니다.")
                for pid in strong_sample:
                    if pid in place_info:
                        name, addr = place_info[pid]
                        st.markdown(
                            f"- **{name}**  \n"
                            f"  📍 {addr}  \n"
                            f"  *(place_id={pid})*"
                        )

                # 강력추천이 있는 경우는 여기서 끝 → medium 출력하지 않음
                st.stop()
            
            # ✨ 강력추천이 없을 때만 → 중간추천 출력
            if medium_sample:
                st.subheader("✨ 중간 추천 (라벨 2개 매치)")
                st.caption(f"총 {medium_total}개 중 랜덤 {len(medium_sample)}개를 보여줍니다.")
                for pid in medium_sample:
                    if pid in place_info:
                        name, addr = place_info[pid]
                        st.markdown(
                            f"- **{name}**  \n"
                            f"  📍 {addr}  \n"
                            f"  *(place_id={pid})*"
                        )

    except Exception as e:
        conn.rollback()
        st.error(f"DB 처리/추천 중 오류 발생: {e}")
    finally:
        cur.close()
        conn.close()
