import requests
import json
import os

def fetch_and_save():
    url = "https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do?srchLtEpsd=all"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    print("동행복권 데이터 수집 시작...")
    res = requests.get(url, headers=headers, timeout=15)
    res.raise_for_status()
    
    data = res.json().get("data", {}).get("list", [])
    
    # 데이터를 lotto_data.json 파일로 저장
    with open("lotto_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("lotto_data.json 저장 완료!")

if __name__ == "__main__":
    fetch_and_save()