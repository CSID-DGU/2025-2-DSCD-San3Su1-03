# core/geocode.py
import os, time, requests

KAKAO_KEY = os.getenv("KAKAO_REST_API_KEY")

def kakao_reverse(lat: float, lon: float, timeout=6, retries=2, sleep_s=0.3):
    assert KAKAO_KEY, "KAKAO_REST_API_KEY 미설정"
    h = {"Authorization": f"KakaoAK {KAKAO_KEY}"}

    def _req(url, params):
        return requests.get(url, headers=h, params=params, timeout=timeout)

    params = {"x": str(lon), "y": str(lat), "input_coord": "WGS84"}

    # 1) coord2address
    for i in range(retries + 1):
        r = _req("https://dapi.kakao.com/v2/local/geo/coord2address.json", params)
        if r.status_code == 200:
            docs = r.json().get("documents", [])
            if docs:
                a = docs[0].get("address") or {}
                raddr = docs[0].get("road_address") or {}
                return {
                    "sido": a.get("region_1depth_name"),
                    "sigungu": a.get("region_2depth_name"),
                    "eup_myeon_dong": a.get("region_3depth_name"),
                    "road_address": raddr.get("address_name"),
                    "address_name": a.get("address_name") or raddr.get("address_name"),
                    "provider": "kakao",
                }
        time.sleep(sleep_s)

    # 2) 폴백: regioncode
    for i in range(retries + 1):
        r = _req("https://dapi.kakao.com/v2/local/geo/coord2regioncode.json", params)
        if r.status_code == 200:
            docs = r.json().get("documents", [])
            if docs:
                d = docs[0]
                return {
                    "sido": d.get("region_1depth_name"),
                    "sigungu": d.get("region_2depth_name"),
                    "eup_myeon_dong": d.get("region_3depth_name"),
                    "road_address": None,
                    "address_name": d.get("address_name"),
                    "provider": "kakao-region",
                }
        time.sleep(sleep_s)

    return None
