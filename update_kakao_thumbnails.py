import os
import requests
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values  # execute_values를 사용하기 위해 추가
from dotenv import load_dotenv, find_dotenv
import time

# =========================================================
# 1. 설정 및 초기화
# =========================================================

# .env 파일 로드 (DB 및 KAKAO_API_KEY 로드)
load_dotenv(find_dotenv(), override=True)

# DB 설정: .env 파일의 환경 변수를 사용하도록 완성
DB_CONFIG = {
    "host": os.getenv("PGHOST"),
    "port": os.getenv("PGPORT"),
    "dbname": os.getenv("PGDATABASE"),
    "user": os.getenv("PGUSER"),
    "password": os.getenv("PGPASSWORD"),
}

# 카카오 API 설정
KAKAO_KEY = os.getenv("KAKAO_API_KEY")
KAKAO_URL = "https://dapi.kakao.com/v2/search/image"

# 필수 환경 변수 검사
if not all([DB_CONFIG["host"], DB_CONFIG["user"], KAKAO_KEY]):
    print("❌ ERROR: DB 설정(PGHOST 등) 또는 KAKAO_API_KEY가 .env에 설정되지 않았습니다.")
    exit()

def get_conn():
    """DB 연결 객체를 반환합니다."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        print(f"❌ ERROR: DB 연결 실패: {e}")
        exit()

# =========================================================
# 2. 카카오 이미지 검색 API 호출 함수 (기존 유지)
# =========================================================

def search_kakao_image(query: str) -> str | None:
    """카카오 이미지 검색 API를 호출하여 첫 번째 이미지 URL을 반환합니다."""
    headers = {
        "Authorization": f"KakaoAK {KAKAO_KEY}",
    }
    params = {
        "query": query,
        "size": 1,  # 딱 1개만 요청
    }

    try:
        response = requests.get(KAKAO_URL, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

        if data.get("documents"):
            # 첫 번째 검색 결과의 'image_url' 반환
            return data["documents"][0].get("image_url")
        return None
        
    except requests.exceptions.HTTPError as e:
        print(f"    [HTTP ERROR {response.status_code}] Query: '{query}'")
        return None
    except Exception as e:
        print(f"    [API Error] {e}")
        return None

# =========================================================
# 3. 배치 실행 메인 로직 (완성)
# =========================================================
def main():
    conn = get_conn()
    cur = conn.cursor()

    # 1. kakao_search_url이 비어있는 모든 장소를 조회합니다.
    # (컬럼명은 이전에 제안된 kakao_search_url을 사용합니다.)
    cur.execute("""
        SELECT place_id, name, address
        FROM place
        WHERE kakao_search_url IS NULL OR kakao_search_url = ''
        ORDER BY place_id ASC
    """)
    places_to_update = cur.fetchall()
    
    if not places_to_update:
        print("✅ 업데이트할 장소가 없습니다. 모든 썸네일 URL이 채워져 있습니다.")
        cur.close(); conn.close()
        return

    total_count = len(places_to_update)
    print(f"🔍 총 {total_count}개의 장소 썸네일 업데이트를 시작합니다 (Kakao API).")

    updates = []
    # API 호출 간격 설정 (카카오 API 제한을 고려하여 안전하게 0.1초)
    API_CALL_DELAY = 0.1 

    for i, (place_id, name, address) in enumerate(places_to_update):
        # 쿼리 생성: 장소 이름과 주소의 첫 부분을 조합하여 정확도 높임
        # 주소가 공백으로 분리된 배열이라고 가정 (예: '서울시 강남구 ...')
        addr_parts = address.split()
        search_addr = f"{addr_parts[0]} {addr_parts[1]}" if len(addr_parts) >= 2 else address
        query = f"{name} {search_addr}"
        
        # API 호출
        image_url = search_kakao_image(query)
        
        if image_url:
            # 업데이트할 (URL, place_id) 튜플 저장
            updates.append((image_url, place_id))
            print(f"  [{i+1}/{total_count}] ID:{place_id} '{name}' -> URL 확보")
        else:
            print(f"  [{i+1}/{total_count}] ID:{place_id} '{name}' -> URL 실패 (SKIP)")

        # API 제한을 피하기 위해 딜레이
        time.sleep(API_CALL_DELAY) 

    # 2. DB 일괄 업데이트
    if updates:
        print(f"\n🚀 {len(updates)}개 장소 정보를 DB에 일괄 업데이트합니다...")
        
        # execute_values를 사용한 효율적인 업데이트
        update_query = """
            UPDATE place SET kakao_search_url = data.url
            FROM (VALUES %s) AS data (url, place_id)
            WHERE place.place_id = data.place_id
        """
        execute_values(cur, update_query, updates)
        
        conn.commit()
        print("✅ DB 업데이트 완료.")
    else:
        print("데이터 수집에 실패하여 업데이트된 장소가 없습니다.")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()