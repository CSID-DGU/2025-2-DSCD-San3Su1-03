# core/db.py
import os
import json
from typing import Iterable, Optional, List, Dict, Any
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_engine: Optional[Engine] = None

# ---------------------------------------
# 0) Engine
# ---------------------------------------
def get_engine() -> Engine:
    """
    DATABASE_URL 예: postgresql+psycopg2://USER:PW@HOST:PORT/DBNAME
    """
    global _engine
    if _engine is None:
        url = os.getenv("DATABASE_URL")
        assert url, "DATABASE_URL 미설정"
        _engine = create_engine(url, pool_pre_ping=True)
        # 연결 확인
        with _engine.connect() as c:
            c.execute(text("SELECT 1"))
    return _engine


# ---------------------------------------
# 1) 시간 정규화 (UTC)
# ---------------------------------------
def _to_utc(ts: Optional[datetime]) -> Optional[datetime]:
    """
    - naive datetime이면 '이미 UTC'로 간주하여 tzinfo=UTC 부여
      (카메라 EXIF는 타임존 정보가 없는 경우가 많음)
    - aware면 UTC로 변환
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


# ---------------------------------------
# 2) photos INSERT/UPSERT
# ---------------------------------------
def insert_photo_record(
    user_id: str,
    bucket: str,
    key: str,
    content_type: Optional[str],
    size: Optional[int],
    taken_at: Optional[datetime] = None,
    lon: Optional[float] = None,
    lat: Optional[float] = None,
) -> int:
    """
    photos(user_id, bucket, key, content_type, size, taken_at_utc, lat, lon)
    스키마에 맞게 저장. (user_id, key) 유니크라 중복시 UPSERT.

    RETURN: photo id
    """
    taken_at_utc = _to_utc(taken_at)

    sql = """
    INSERT INTO photos (user_id, bucket, key, content_type, size, taken_at_utc, lat, lon)
    VALUES (:uid, :bucket, :key, :ct, :size, :taken_at_utc, :lat, :lon)
    ON CONFLICT (user_id, key)
    DO UPDATE SET
        content_type   = EXCLUDED.content_type,
        size           = EXCLUDED.size,
        taken_at_utc   = COALESCE(EXCLUDED.taken_at_utc, photos.taken_at_utc),
        lat            = COALESCE(EXCLUDED.lat, photos.lat),
        lon            = COALESCE(EXCLUDED.lon, photos.lon),
        updated_at     = NOW()
    RETURNING id;
    """
    eng = get_engine()
    with eng.begin() as conn:
        pid = conn.execute(
            text(sql),
            {
                "uid": user_id,
                "bucket": bucket,
                "key": key,
                "ct": content_type,
                "size": size,
                "taken_at_utc": taken_at_utc,
                "lat": lat,
                "lon": lon,
            },
        ).scalar_one()
    return int(pid)


# ---------------------------------------
# 3) photos 조회 유틸
# ---------------------------------------
def fetch_photos_by_keys(user_id: str, keys: Iterable[str]) -> List[Dict[str, Any]]:
    """
    특정 유저의 S3 key들에 해당하는 사진 메타를 가져온다.
    """
    keys = list(keys)
    if not keys:
        return []

    sql = """
    SELECT id, key, bucket, content_type, size, taken_at_utc, lat, lon, created_at, updated_at
    FROM photos
    WHERE user_id = :uid
      AND key = ANY(:keys)
    """
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(text(sql), {"uid": user_id, "keys": keys}).mappings().all()
        return [dict(r) for r in rows]


def fetch_photos_by_group_prefix(user_id: str, group_id: int) -> List[Dict[str, Any]]:
    """
    업로드 시 사용한 prefix 형태(users/{user_id}/imgs/{group_id}/...)로 묶음 조회.
    """
    prefix = f"users/{user_id}/imgs/{int(group_id)}/"
    sql = """
    SELECT id, key, bucket, content_type, size, taken_at_utc, lat, lon, created_at, updated_at
    FROM photos
    WHERE user_id = :uid
      AND key LIKE :prefix
    ORDER BY taken_at_utc NULLS LAST, created_at
    """
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(text(sql), {"uid": user_id, "prefix": prefix + "%"}).mappings().all()
        return [dict(r) for r in rows]


# ---------------------------------------
# 4) 주소 캐시(photo_addresses) 유틸
# ---------------------------------------
def get_address_cache_by_photo(photo_id: int) -> Optional[Dict[str, Any]]:
    """
    photo_id 기반 주소 캐시 1건 조회 (없으면 None)
    """
    sql = "SELECT addr_json, provider, updated_at FROM photo_addresses WHERE photo_id = :pid"
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(text(sql), {"pid": photo_id}).mappings().first()
        if row and row["addr_json"]:
            # addr_json은 JSONB → 파이썬 dict로 반환
            return {
                "provider": row["provider"],
                "updated_at": row["updated_at"],
                "data": row["addr_json"] if isinstance(row["addr_json"], dict)
                        else json.loads(row["addr_json"]),
            }
    return None


def upsert_address_cache(
    photo_id: int,
    lat: float,
    lon: float,
    provider: str,
    addr_dict: Dict[str, Any],
) -> None:
    """
    photo_addresses UPSERT
    - (photo_id) PK 1:1 보장
    - lat/lon은 round(6)로 저장 (좌표 캐시 key 안정화)
    """
    lat_r6 = round(float(lat), 6)
    lon_r6 = round(float(lon), 6)
    sql = """
    INSERT INTO photo_addresses (photo_id, lat_round6, lon_round6, provider, addr_json, updated_at)
    VALUES (:pid, :la, :lo, :prov, :js, NOW())
    ON CONFLICT (photo_id)
    DO UPDATE SET
        lat_round6 = EXCLUDED.lat_round6,
        lon_round6 = EXCLUDED.lon_round6,
        provider   = EXCLUDED.provider,
        addr_json  = EXCLUDED.addr_json,
        updated_at = NOW();
    """
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text(sql),
            {
                "pid": photo_id,
                "la": lat_r6,
                "lo": lon_r6,
                "prov": provider,
                "js": json.dumps(addr_dict, ensure_ascii=False),
            },
        )


def get_address_cache_by_coord(lat: float, lon: float, provider: str = "kakao") -> Optional[Dict[str, Any]]:
    """
    좌표(라운드6) + provider로 캐시된 주소를 찾는다.
    동일 좌표를 여러 사진이 공유할 때 API 호출을 줄이기 위해 사용.
    """
    lat_r6 = round(float(lat), 6)
    lon_r6 = round(float(lon), 6)
    sql = """
    SELECT addr_json, updated_at
    FROM photo_addresses
    WHERE lat_round6 = :la AND lon_round6 = :lo AND provider = :prov
    LIMIT 1
    """
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(text(sql), {"la": lat_r6, "lo": lon_r6, "prov": provider}).mappings().first()
        if row and row["addr_json"]:
            return {
                "updated_at": row["updated_at"],
                "data": row["addr_json"] if isinstance(row["addr_json"], dict)
                        else json.loads(row["addr_json"]),
            }
    return None

#######################
def upsert_episode_for_upload(user_id: str, episode_no: int, title: str | None = None):
    """
    업로드 시점에 에피소드 메타 정보 upsert.
    (시작/끝 시각, 사진 수는 뒤에서 한 번에 갱신해도 됨)
    """
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO episodes (user_id, episode_no, title, created_at, updated_at)
                VALUES (:user_id, :episode_no, :title, NOW(), NOW())
                ON CONFLICT (user_id, episode_no)
                DO UPDATE SET title = COALESCE(:title, episodes.title), updated_at = NOW()
            """),
            {"user_id": user_id, "episode_no": episode_no, "title": title,}
        )


def refresh_episode_meta(user_id: str, episode_no: int):
    """
    photos 테이블에서,
    S3 key prefix (users/{user_id}/imgs/{episode_no}/...) 기준으로
    해당 에피소드의 시작/끝 시각, 사진 수를 갱신.
    """
    eng = get_engine()

    # S3 key 패턴과 동일하게 prefix 구성
    prefix = f"users/{user_id}/imgs/{episode_no}/"

    with eng.begin() as conn:
        row = conn.execute(
            text("""
                SELECT 
                    MIN(taken_at_utc) AS started_at,
                    MAX(taken_at_utc) AS ended_at,
                    COUNT(*)          AS photo_count
                FROM photos
                WHERE user_id = :user_id
                  AND key LIKE :prefix
            """),
            {"user_id": user_id, "prefix": prefix + "%"}
        ).mappings().first()

        # 해당 에피소드에 사진이 하나도 없으면 그냥 리턴
        if not row or row["photo_count"] == 0:
            return

        conn.execute(
            text("""
                INSERT INTO episodes (
                    user_id, episode_no, started_at, ended_at,
                    photo_count, created_at, updated_at
                )
                VALUES (
                    :user_id, :episode_no, :started_at, :ended_at,
                    :photo_count, NOW(), NOW()
                )
                ON CONFLICT (user_id, episode_no)
                DO UPDATE SET
                    started_at  = EXCLUDED.started_at,
                    ended_at    = EXCLUDED.ended_at,
                    photo_count = EXCLUDED.photo_count,
                    updated_at  = NOW()
            """),
            {
                "user_id": user_id,
                "episode_no": episode_no,
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "photo_count": row["photo_count"],
            }
        )


def fetch_episodes_for_user(user_id: str):
    """
    유저의 모든 에피소드 리스트 반환 (최신 순).
    """
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    id,
                    episode_no,
                    title,
                    started_at,
                    ended_at,
                    photo_count,
                    created_at
                FROM episodes
                WHERE user_id = :user_id
                ORDER BY started_at NULLS LAST, episode_no ASC
            """),
            {"user_id": user_id}
        ).mappings().all()
    return rows


def insert_episode_diary(
    user_id: str,
    episode_no: int,
    mood: str,
    title: str,
    content: str,
    tags: str | None = None,
):
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO episode_diaries (user_id, episode_no, mood, title, content, tags, created_at)
                VALUES (:user_id, :episode_no, :mood, :title, :content, :tags, NOW())
            """),
            {
                "user_id": user_id,
                "episode_no": episode_no,
                "mood": mood,
                "title": title,
                "content": content,
                "tags": tags,
            }
        )


def fetch_diaries_for_episode(user_id: str, episode_no: int):
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, mood, title, content, tags, created_at
                FROM episode_diaries
                WHERE user_id = :user_id
                  AND episode_no = :episode_no
                ORDER BY created_at DESC
            """),
            {"user_id": user_id, "episode_no": episode_no}
        ).mappings().all()
    return rows