import requests
import json
import os

def fetch_lotto_data():
    # 1. 동행복권 데이터 수집
    url = "https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do?srchLtEpsd=all"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    print("🌐 동행복권 데이터 수집 시작...")
    try:
        res = requests.get(url, headers=headers, timeout=15)
        res.raise_for_status()
        data = res.json().get("data", {}).get("list", [])
    except Exception as e:
        print(f"❌ 데이터 수집 실패: {e}")
        return

    if not data:
        print("❌ API 응답에 데이터가 없습니다.")
        return

    # 2. JSON 파일 저장 (로컬 및 클라우드 lotto-ai 폴더 내)
    file_path = "lotto_data.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ {file_path} 저장 완료! (총 {len(data)}회차)")

if __name__ == "__main__":
    # 작업 디렉토리 강제 이동
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    fetch_lotto_data()