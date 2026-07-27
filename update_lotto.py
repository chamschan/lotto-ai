import requests
import json
import subprocess
import os
from datetime import datetime

def fetch_and_update_github():
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

    # 2. JSON 파일 저장 (로컬 lotto-ai 폴더 내)
    file_path = "lotto_data.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ {file_path} 로컬 저장 완료! (총 {len(data)}회차)")

    # 3. Git Push 자동화
    print("🚀 GitHub 자동 푸시 시작...")
    try:
        # 커밋 메시지에 현재 시간 추가 (업데이트 확인용)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        commit_msg = f"Auto-update lotto data: {now_str}"

        # Git 명령어 순차 실행
        subprocess.run(["git", "pull", "origin", "main"], check=True) # 충돌 방지를 위해 먼저 Pull
        subprocess.run(["git", "add", file_path], check=True)
        subprocess.run(["git", "commit", "-m", commit_msg], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        
        print("🎉 GitHub 업데이트가 성공적으로 완료되었습니다!")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Git 처리 중 알림: 커밋할 변경 사항이 없거나(데이터 동일), 통신 오류일 수 있습니다.\n상세: {e}")

if __name__ == "__main__":
    # 작업 스케줄러 등에서 실행될 때 경로 꼬임을 방지하기 위해 
    # 현재 스크립트가 있는 폴더로 작업 디렉토리 강제 이동
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    fetch_and_update_github()