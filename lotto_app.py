import streamlit as st
import requests
import random
import os
import json
import time
import pandas as pd
from collections import Counter
import streamlit.components.v1 as components
import base64

import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# [0] PWA 설치형 앱 설정
# ==========================================
_PWA_MANIFEST = """
{
  "name": "인공지능 로또",
  "short_name": "AI로또",
  "theme_color": "#2980B9",
  "background_color": "#ffffff",
  "display": "standalone",
  "start_url": "/",
  "icons": [
    {
      "src": "https://cdn-icons-png.flaticon.com/512/3063/3063822.png",
      "sizes": "512x512",
      "type": "image/png"
    }
  ]
}
"""
_PWA_MANIFEST_B64 = base64.b64encode(_PWA_MANIFEST.encode()).decode()

components.html(f"""
<script>
    if (!window.parent.document.getElementById('pwa-manifest')) {{
        const manifest = window.parent.document.createElement('link');
        manifest.id = 'pwa-manifest';
        manifest.rel = 'manifest';
        manifest.href = 'data:application/manifest+json;base64,{_PWA_MANIFEST_B64}';
        window.parent.document.head.appendChild(manifest);
    }}
</script>
""", width=0, height=0)


# ==========================================
# [1] 구글 스프레드시트 연동 (서버 저장/로드)
# ==========================================
@st.cache_resource(show_spinner=False)
def get_gsheet_client():
    """구글 서비스 계정 인증 클라이언트를 캐싱하여 반환합니다."""
    try:
        if "gcp_service_account" not in st.secrets or "sheet" not in st.secrets:
            return None
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        sa_info = dict(st.secrets["gcp_service_account"])
        if "private_key" in sa_info and isinstance(sa_info["private_key"], str):
            sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        st.session_state["gsheet_init_error"] = str(e)
        return None


def _parse_games_cell(raw_val):
    """시트 셀에 저장된 JSON 문자열을 안전하게 게임 리스트([[int, ...], ...])로 파싱합니다."""
    if isinstance(raw_val, list):
        data = raw_val
    elif isinstance(raw_val, str):
        raw_val = raw_val.strip()
        if not raw_val:
            return []
        data = json.loads(raw_val)
    else:
        return []

    if not isinstance(data, list):
        return []

    # 단일 게임([1, 2, 3, 4, 5, 6]) 형태로 저장된 경우 리스트로 감싸기
    if data and all(isinstance(x, (int, float)) for x in data):
        return [[int(x) for x in data]]

    valid_games = []
    for g in data:
        if isinstance(g, list) and len(g) > 0:
            try:
                valid_games.append(sorted(int(x) for x in g))
            except (ValueError, TypeError):
                continue
    return valid_games


@st.cache_data(ttl=30, show_spinner=False)
def _load_sheet_records_cached() -> tuple[list, str | None]:
    """구글 시트에서 이력을 읽어오되, 30초 캐싱으로 API Quota 초과(429 에러)를 방지합니다."""
    gc = get_gsheet_client()
    if not gc:
        err = st.session_state.get("gsheet_init_error")
        return [], err

    sheet_url = st.secrets["sheet"].get("url", "").strip()
    if not sheet_url:
        return [], "st.secrets['sheet']['url'] 설정이 비어 있습니다."

    worksheet = gc.open_by_url(sheet_url).sheet1
    rows = worksheet.get_all_values()
    records = []
    for row in rows:
        if len(row) >= 2:
            try:
                epsd_str = str(row[0]).strip()
                if not epsd_str or not epsd_str.lstrip("-").isdigit():
                    continue
                epsd = int(epsd_str)
                games = _parse_games_cell(row[1])
                if games:
                    records.append({"epsd": epsd, "games": games})
            except (ValueError, json.JSONDecodeError):
                continue
    return records, None


def load_history() -> list:
    """구글 시트(서버) 및 로컬 백업 파일에서 번호 생성 이력을 불러옵니다."""
    records = []
    sheet_loaded = False

    try:
        sheet_records, err_msg = _load_sheet_records_cached()
        if err_msg:
            st.warning(f"구글 시트 연결 설정 확인 필요, 로컬 데이터를 함께 사용합니다. ({err_msg})")
        elif get_gsheet_client() is not None:
            records.extend(sheet_records)
            sheet_loaded = True
    except Exception as e:
        st.warning(f"구글 시트 불러오기 실패, 로컬 파일로 대체합니다. ({e})")

    # 시트 미연결 또는 실패 시(혹은 로컬에만 저장된 데이터 보완) 로컬 파일 로드
    if not sheet_loaded and os.path.exists("lotto_history.jsonl"):
        with open("lotto_history.jsonl", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    epsd = int(item.get("epsd", 0))
                    games = _parse_games_cell(item.get("games", []))
                    if epsd > 0 and games:
                        records.append({"epsd": epsd, "games": games})
                except (ValueError, json.JSONDecodeError):
                    continue

    return records


def save_history(epsd: int, games: list, retries: int = 3, retry_delay: float = 1.5) -> bool:
    """생성된 번호를 구글 시트(서버)에 실시간 저장하고 캐시를 즉시 갱신합니다."""
    last_error = None
    try:
        gc = get_gsheet_client()
    except Exception as e:
        gc = None
        last_error = e

    if gc and "sheet" in st.secrets and st.secrets["sheet"].get("url"):
        sheet_url = st.secrets["sheet"]["url"].strip()
        row_data = [int(epsd), json.dumps(games, ensure_ascii=False)]

        for attempt in range(1, retries + 1):
            try:
                worksheet = gc.open_by_url(sheet_url).sheet1
                res = worksheet.append_row(
                    row_data,
                    value_input_option="RAW",
                    insert_data_option="INSERT_ROWS",
                )
                # API 응답 또는 마지막 행으로 정상 반영 확인 (불필요한 전체 재조회 및 중복 삽입 방지)
                updates = res.get("updates", {}) if isinstance(res, dict) else {}
                updated_rows = updates.get("updatedRows", 1)
                if updated_rows >= 1:
                    _load_sheet_records_cached.clear()
                    return True
                raise ValueError(f"구글 시트 행 추가 응답 확인 실패: {res}")
            except Exception as e:
                last_error = e
                if attempt < retries:
                    time.sleep(retry_delay)
                else:
                    st.warning(
                        f"구글 시트 저장 {retries}회 모두 실패, 로컬 파일에 백업합니다. (마지막 오류: {e})"
                    )
    elif last_error:
        st.warning(f"구글 시트 인증 오류로 로컬 파일에 저장합니다. ({last_error})")

    # 로컬 백업 파일 저장
    try:
        with open("lotto_history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"epsd": int(epsd), "games": games}, ensure_ascii=False) + "\n")
    except Exception as e:
        st.error(f"로컬 파일 저장 중 오류가 발생했습니다: {e}")

    return False


# ==========================================
# [2] AI 분석 엔진
# ==========================================
PRIMES = {2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43}

# 실제 로또 OMR 구조: 1~45를 5열 9행으로 배치
# 행: (n-1) // 5  →  0~8행
# 열: (n-1) %  5  →  0~4열
OMR_ROWS = 9
OMR_COLS = 5


class LottoAI:

    def analyze_recent_trend(self, data: list, scope: int = 15) -> dict:
        """최근 scope 회차 번호의 출현 빈도를 가중치로 반환."""
        recent = data[:scope * 6]
        counts = Counter(recent)
        weights = {i: 1.0 for i in range(1, 46)}
        for num, freq in counts.items():
            if 1 <= num <= 45:
                weights[num] += freq * 0.5
        return weights

    def get_cold_numbers(self, data: list, scope: int = 15) -> set:
        """최근 scope 회차 동안 한 번도 나오지 않은 미출수 반환."""
        appeared = set(data[:scope * 6])
        return set(range(1, 46)) - appeared

    def has_cold_number(self, numbers: list, cold_set: set) -> bool:
        """미출수가 1개 이상 포함되어 있는지 확인. cold_set이 비어있으면 통과."""
        if not cold_set:
            return True
        return any(n in cold_set for n in numbers)

    def passes_omr_filter(self, numbers: list) -> bool:
        """실제 OMR 구조(5열 9행) 기준, 같은 행/열에 4개 이상 몰리는 패턴 차단."""
        rows = [(n - 1) // OMR_COLS for n in numbers]
        cols = [(n - 1) %  OMR_COLS for n in numbers]
        if any(c >= 4 for c in Counter(rows).values()):
            return False
        if any(c >= 4 for c in Counter(cols).values()):
            return False
        return True

    def has_end_digit_pair(self, numbers: list) -> bool:
        """끝자리가 같은 번호가 1쌍 이상."""
        end_digits = [n % 10 for n in numbers]
        return any(c >= 2 for c in Counter(end_digits).values())

    def has_dead_zone(self, numbers: list) -> bool:
        """9구간(5번호 단위) 중 2개 이상이 비어있는지 (분산 패턴)."""
        zones = [0] * 9
        for n in numbers:
            zones[(n - 1) // 5] = 1
        return zones.count(0) >= 2

    def passes_stat_filter(self, numbers: list) -> bool:
        """합계, 홀짝, 고저 분포 통계 기준."""
        total = sum(numbers)
        if not (100 <= total <= 175):
            return False
        odd_count = sum(1 for n in numbers if n % 2 != 0)
        if odd_count in (0, 6):
            return False
        low_count = sum(1 for n in numbers if n <= 22)
        if low_count in (0, 6):
            return False
        return True

    def has_consecutive(self, numbers: list) -> bool:
        """연속 번호가 1쌍 이상."""
        s = sorted(numbers)
        return any(s[i + 1] == s[i] + 1 for i in range(len(s) - 1))

    def passes_prime_filter(self, numbers: list) -> bool:
        """소수 개수가 1~4개 (0개 또는 5개 이상은 희박)."""
        prime_count = sum(1 for n in numbers if n in PRIMES)
        return 1 <= prime_count <= 4

    def passes_ac_filter(self, numbers: list) -> bool:
        """AC값(번호 간 차이의 종류 수)이 7 이상."""
        s = sorted(numbers)
        diffs = set()
        for i in range(len(s)):
            for j in range(i + 1, len(s)):
                diffs.add(s[j] - s[i])
        return len(diffs) >= 7

    def passes_section_balance(self, numbers: list) -> bool:
        """전반부(1~22)와 후반부(23~45) 합의 차이가 50 미만."""
        low_sum  = sum(n for n in numbers if n <= 22)
        high_sum = sum(n for n in numbers if n > 22)
        return abs(low_sum - high_sum) < 50

    def passes_multiple_filter(self, numbers: list) -> bool:
        """3의 배수 4개 이상, 또는 5의 배수 3개 이상 편중 차단."""
        if sum(1 for n in numbers if n % 3 == 0) >= 4:
            return False
        if sum(1 for n in numbers if n % 5 == 0) >= 3:
            return False
        return True

    def get_popularity_bias_weights(self, strength: float = 0.35) -> list:
        """
        [주의] 당첨 '확률'과는 무관합니다. 확률은 어떤 조합이든 동일합니다.
        로또645는 1등 상금을 당첨자 수만큼 나눠 갖는 방식(parimutuel)이라,
        생일수(1~31)에 사람들이 몰려 선택하는 경향(실증적으로 확인된 사실)을
        피하면 '당첨됐을 때 나눠 가질 사람 수'가 줄어 기대 배당금이 커집니다.
        32~45번에 소폭의 가중치를 더해 그 쏠림을 피하도록 유도합니다.
        """
        weights = []
        for n in range(1, 46):
            if n <= 31:
                weights.append(1.0)
            else:
                weights.append(1.0 + strength)
        return weights

    def passes_popularity_filter(self, numbers: list) -> bool:
        """
        확률이 아니라 '당첨금 분산 방지'용 필터.
        - 6개 전부 1~31(생일수)에 몰린 조합: 수동 선택자들이 압도적으로 선호하는
          패턴이라 당첨 시 나눠 가질 사람이 많아질 가능성이 높음 → 배제.
        - 완전 등차수열(예: 3,9,15,21,27,33): 사람이 '규칙적이라 좋아 보여서'
          고르는 대표적 패턴 → 배제.
        """
        if all(n <= 31 for n in numbers):
            return False
        s = sorted(numbers)
        diffs = {s[i + 1] - s[i] for i in range(len(s) - 1)}
        if len(diffs) == 1:
            return False
        return True

    def get_specs(self, numbers: list) -> str:
        """번호 조합의 주요 스펙 요약 문자열 반환."""
        total     = sum(numbers)
        odd       = sum(1 for n in numbers if n % 2 != 0)
        low       = sum(1 for n in numbers if n <= 22)
        s         = sorted(numbers)
        diffs     = set()
        for i in range(len(s)):
            for j in range(i + 1, len(s)):
                diffs.add(s[j] - s[i])
        ac = len(diffs) - 5
        return f"합:{total} | 홀짝 {odd}:{6-odd} | 고저 {low}:{6-low} | AC:{ac}"


def generate_ai_games(
    full_data: list,
    weight_percent: int,
    options: dict,
    fixed_nums: list,
    excluded_nums: list,
    scope: int = 15,
) -> list:
    ai = LottoAI()

    if options.get("use_trend"):
        trend_weights = ai.analyze_recent_trend(full_data, scope=scope)
        extra = weight_percent / 100.0
        base_weights = [
            trend_weights.get(i, 1.0) + extra if trend_weights.get(i, 1.0) > 1.0
            else 1.0
            for i in range(1, 46)
        ]
    else:
        base_weights = [1.0] * 45

    # 확률 자체와는 무관. 당첨금 분산(공동 당첨자 수) 축소 목적의 가중치.
    if options.get("use_popularity"):
        pop_weights = ai.get_popularity_bias_weights()
        base_weights = [bw * pw for bw, pw in zip(base_weights, pop_weights)]

    # 제외 번호 가중치 0 처리
    final_weights = [
        0.0 if (i in excluded_nums) else base_weights[i - 1]
        for i in range(1, 46)
    ]

    pool         = [i for i in range(1, 46) if i not in excluded_nums and i not in fixed_nums]
    pool_weights = [final_weights[i - 1] for i in pool]
    if sum(pool_weights) <= 0 and pool:
        pool_weights = [1.0] * len(pool)

    needed       = 6 - len(fixed_nums)
    cold_numbers = (ai.get_cold_numbers(full_data, scope=scope) - set(excluded_nums))

    final_games = []
    relaxed_any = False

    if needed < 0:
        st.warning("고정 번호가 6개를 초과합니다. 고정 번호를 줄여주세요.")
        return [sorted(fixed_nums[:6])] * 5

    if len(pool) < needed:
        st.error(f"선택 가능한 번호({len(pool)}개)가 필요한 번호 수({needed}개)보다 적습니다. 제외 번호를 줄여주세요.")
        return []

    while len(final_games) < 5:
        active_options = options.copy()
        attempts = 0

        while True:
            attempts += 1

            # 단계별 조건 완화 (덜 중요한 순서대로)
            if attempts ==  2_000: active_options["use_omr"]             = False; relaxed_any = True
            if attempts ==  3_000: active_options["use_dead_zone"]       = False; relaxed_any = True
            if attempts ==  4_000: active_options["use_section_balance"] = False; relaxed_any = True
            if attempts ==  5_000: active_options["use_multiple"]        = False; relaxed_any = True
            if attempts ==  6_000: active_options["use_consecutive"]     = False; relaxed_any = True
            if attempts ==  7_000: active_options["use_cold"]            = False; relaxed_any = True
            if attempts ==  8_000: active_options["use_prime"]           = False; relaxed_any = True
            if attempts ==  9_000: active_options["use_ac"]              = False; relaxed_any = True
            if attempts == 10_000: active_options["use_stats"]           = False; relaxed_any = True
            if attempts == 11_000: active_options["use_end_digit"]       = False; relaxed_any = True
            if attempts == 11_500: active_options["use_popularity"]      = False; relaxed_any = True

            if attempts > 12_000:
                picks = random.sample(pool, needed)
                final_games.append(sorted(fixed_nums + picks))
                relaxed_any = True
                break

            if needed == 0:
                candidate = sorted(fixed_nums)
            else:
                picks = random.choices(pool, weights=pool_weights, k=needed)
                if len(set(picks)) < needed:
                    continue
                candidate = sorted(fixed_nums + picks)

            if active_options.get("use_omr")             and not ai.passes_omr_filter(candidate):             continue
            if active_options.get("use_cold")            and not ai.has_cold_number(candidate, cold_numbers): continue
            if active_options.get("use_end_digit")       and not ai.has_end_digit_pair(candidate):            continue
            if active_options.get("use_dead_zone")       and not ai.has_dead_zone(candidate):                 continue
            if active_options.get("use_stats")           and not ai.passes_stat_filter(candidate):            continue
            if active_options.get("use_prime")           and not ai.passes_prime_filter(candidate):           continue
            if active_options.get("use_ac")              and not ai.passes_ac_filter(candidate):              continue
            if active_options.get("use_section_balance") and not ai.passes_section_balance(candidate):        continue
            if active_options.get("use_multiple")        and not ai.passes_multiple_filter(candidate):        continue
            if active_options.get("use_popularity")      and not ai.passes_popularity_filter(candidate):      continue
            if active_options.get("use_consecutive"):
                if len(final_games) < 3 and not ai.has_consecutive(candidate):
                    if random.random() < 0.7:
                        continue

            final_games.append(candidate)
            break

    if relaxed_any:
        st.info("💡 일부 필터 조합이 까다로워 AI가 조건을 단계적으로 완화하여 번호를 생성했습니다.")

    return final_games


# ==========================================
# [3] 실시간 당첨 데이터 및 당첨금 가져오기
# ==========================================
DHLOTTERY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.dhlottery.co.kr/lt645/result",
}


def _normalize_draw_item(item: dict) -> dict | None:
    """
    동행복권 신규 API(selectPstLt645Info.do), 구 API(getLottoNumber),
    GitHub JSON(sckimhub / smok95) 등 다양한 응답 포맷을 단일 스키마로 정규화합니다.
    """
    if not isinstance(item, dict):
        return None

    # 1) 회차 번호 추출
    epsd = (
        item.get("ltEpsd")
        or item.get("drwNo")
        or item.get("draw_no")
        or item.get("epsd")
    )
    if not epsd:
        return None
    try:
        epsd = int(epsd)
    except (ValueError, TypeError):
        return None

    # 2) 당첨 번호 6개 추출
    nums = []
    if "tm1WnNo" in item:
        nums = [int(item.get(f"tm{i}WnNo", 0)) for i in range(1, 7)]
    elif "drwtNo1" in item:
        nums = [int(item.get(f"drwtNo{i}", 0)) for i in range(1, 7)]
    elif "numbers" in item and isinstance(item["numbers"], list) and len(item["numbers"]) >= 6:
        nums = [int(x) for x in item["numbers"][:6]]

    if len(nums) != 6 or any(n < 1 or n > 45 for n in nums):
        return None

    # 3) 보너스 번호 추출 (신규 API: bnsWnNo / 구 API: bnusNo / 미러: bonus_no)
    bonus_raw = (
        item.get("bnsWnNo")
        if item.get("bnsWnNo") is not None
        else (
            item.get("bnusNo")
            if item.get("bnusNo") is not None
            else item.get("bonus_no", 0)
        )
    )
    try:
        bonus = int(bonus_raw)
    except (ValueError, TypeError):
        bonus = 0

    # 4) 등수별 1게임당 당첨금 추출 (신규 API: rnk1WnAmt~rnk5WnAmt / 구 API: firstWinamnt)
    def _to_int_or_none(v):
        try:
            val = int(v)
            return val if val > 0 else None
        except (ValueError, TypeError):
            return None

    p1 = _to_int_or_none(item.get("rnk1WnAmt") or item.get("firstWinamnt"))
    p2 = _to_int_or_none(item.get("rnk2WnAmt"))
    p3 = _to_int_or_none(item.get("rnk3WnAmt"))
    p4 = _to_int_or_none(item.get("rnk4WnAmt"))
    p5 = _to_int_or_none(item.get("rnk5WnAmt"))

    if "divisions" in item and isinstance(item["divisions"], list):
        divs = item["divisions"]
        if len(divs) >= 1 and isinstance(divs[0], dict) and p1 is None:
            p1 = _to_int_or_none(divs[0].get("prize"))
        if len(divs) >= 2 and isinstance(divs[1], dict) and p2 is None:
            p2 = _to_int_or_none(divs[1].get("prize"))
        if len(divs) >= 3 and isinstance(divs[2], dict) and p3 is None:
            p3 = _to_int_or_none(divs[2].get("prize"))

    return {
        "ltEpsd": epsd,
        "nums": sorted(nums),
        "bnsWnNo": bonus,
        "prizes": {
            1: p1,
            2: p2 if p2 is not None else 50_000_000,
            3: p3 if p3 is not None else 1_500_000,
            4: p4 if p4 is not None else 50_000,
            5: p5 if p5 is not None else 5_000,
        },
    }


def _extract_raw_list(payload) -> list:
    """API 응답 JSON에서 회차 리스트를 추출합니다."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # 동행복권 신규 API: {"data": {"list": [...]}}
        if "data" in payload and isinstance(payload["data"], dict):
            inner = payload["data"].get("list")
            if isinstance(inner, list):
                return inner
        if "list" in payload and isinstance(payload["list"], list):
            return payload["list"]
        if "ltEpsd" in payload or "drwNo" in payload:
            return [payload]
    return []


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_lotto_data_cached() -> list[dict]:
    """
    실시간 동행복권 통합 API 및 GitHub 데이터 소스를 순차적으로 시도하여
    정규화된 전체 회차 리스트(최신순)를 반환합니다.
    """
    ts = int(time.time() * 1000)
    sources = [
        (
            "동행복권 실시간 API",
            f"https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do?srchLtEpsd=all&_={ts}",
            DHLOTTERY_HEADERS,
        ),
        (
            "동행복권 실시간 API (보조)",
            f"https://dhlottery.co.kr/lt645/selectPstLt645Info.do?srchLtEpsd=all&_={ts}",
            DHLOTTERY_HEADERS,
        ),
        (
            "GitHub 데이터 저장소 (sckimhub)",
            "https://raw.githubusercontent.com/sckimhub/lotto-ai/main/lotto_data.json",
            {"User-Agent": "Mozilla/5.0"},
        ),
        (
            "GitHub 공개 미러 (smok95)",
            "https://smok95.github.io/lotto/results/all.json",
            {"User-Agent": "Mozilla/5.0"},
        ),
    ]

    errors = []
    for source_name, url, headers in sources:
        try:
            res = requests.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            payload = res.json()
            raw_list = _extract_raw_list(payload)
            normalized = []
            seen_epsd = set()
            for item in raw_list:
                norm = _normalize_draw_item(item)
                if norm and norm["ltEpsd"] not in seen_epsd:
                    seen_epsd.add(norm["ltEpsd"])
                    normalized.append(norm)

            if normalized:
                normalized.sort(key=lambda x: x["ltEpsd"], reverse=True)
                return normalized
            errors.append(f"{source_name}: 유효한 회차 데이터가 없습니다.")
        except Exception as e:
            errors.append(f"{source_name}: {e}")

    raise RuntimeError(" / ".join(errors))


def fetch_lotto_data(count: int):
    """
    전체 회차 데이터와 최근 count개 회차 정보를 반환합니다.
    반환값: (full_data_flat, recent_history_info, all_epsd_result_map, all_epsd_prize_map)
    실패 시: (None, error_message, {}, {})
    """
    try:
        all_list = _fetch_lotto_data_cached()
    except Exception as e:
        return None, str(e), {}, {}

    full_data_flat = []
    all_epsd_result_map = {}
    all_epsd_prize_map = {}

    for item in all_list:
        epsd   = item["ltEpsd"]
        nums   = item["nums"]
        bonus  = item["bnsWnNo"]
        prizes = item["prizes"]

        full_data_flat.extend(nums)
        all_epsd_result_map[epsd] = (set(nums), bonus)
        all_epsd_prize_map[epsd]  = prizes

    history_info = [
        (item["ltEpsd"], item["nums"], item["bnsWnNo"])
        for item in all_list[:count]
    ]

    return full_data_flat, history_info, all_epsd_result_map, all_epsd_prize_map


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_prize_cached(epsd: int) -> dict:
    """특정 회차의 당첨금 정보를 동행복권 신규 API(및 구 API 폴백)로 조회합니다."""
    ts = int(time.time() * 1000)
    new_url = f"https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do?srchLtEpsd={epsd}&_={ts}"
    try:
        res = requests.get(new_url, headers=DHLOTTERY_HEADERS, timeout=6)
        res.raise_for_status()
        raw_list = _extract_raw_list(res.json())
        if raw_list:
            norm = _normalize_draw_item(raw_list[0])
            if norm and norm["prizes"].get(1):
                return norm["prizes"]
    except Exception:
        pass

    # 구 API 폴백
    old_url = f"https://www.dhlottery.co.kr/common.do?method=getLottoNumber&drwNo={epsd}"
    res = requests.get(old_url, headers=DHLOTTERY_HEADERS, timeout=5)
    res.raise_for_status()
    data = res.json()
    if data.get("returnValue") == "success" and data.get("firstWinamnt", 0) > 0:
        return {
            1: int(data["firstWinamnt"]),
            2: 50_000_000,
            3: 1_500_000,
            4: 50_000,
            5: 5_000,
        }
    raise ValueError("당첨금 정보가 아직 업데이트되지 않았습니다.")


def fetch_prize_info(epsd: int, prize_map: dict | None = None) -> dict:
    default_prizes = {1: None, 2: 50_000_000, 3: 1_500_000, 4: 50_000, 5: 5_000}
    if prize_map and epsd in prize_map:
        cached = prize_map[epsd]
        if cached.get(1) is not None:
            return cached
    try:
        return _fetch_prize_cached(epsd)
    except Exception:
        return default_prizes


# ==========================================
# [4] UI 헬퍼
# ==========================================
BALL_COLORS = {
    (1, 10):  "#F39C12",
    (11, 20): "#3498DB",
    (21, 30): "#E74C3C",
    (31, 40): "#7F8C8D",
    (41, 45): "#27AE60",
}

def get_ball_color(num: int) -> str:
    for (lo, hi), color in BALL_COLORS.items():
        if lo <= num <= hi:
            return color
    return "#27AE60"

def get_ball_html(num: int, size: int = 32, fsize: int = 13) -> str:
    color = get_ball_color(num)
    return (
        f'<div style="display:inline-flex;justify-content:center;align-items:center;'
        f'width:{size}px;height:{size}px;border-radius:50%;background-color:{color};'
        f'color:white;font-weight:bold;font-size:{fsize}px;margin-right:3px;'
        f'flex-shrink:0;box-shadow:1px 1px 2px rgba(0,0,0,0.3);">{num}</div>'
    )

def draw_row(label: str, balls: list, is_header: bool = False,
             specs: str = "", highlight: bool = False, bonus: int | None = None):
    balls_html = "".join(get_ball_html(n) for n in balls)
    if bonus is not None and bonus > 0:
        balls_html += (
            '<span style="margin:0 6px;font-weight:bold;color:#7f8c8d;font-size:14px;">+</span>'
            + get_ball_html(bonus)
        )
    label_color = "#2980B9" if is_header else "#333"
    bg_color    = "#fffbe6" if highlight else "white"
    border      = "2px solid #f1c40f" if highlight else "1px solid #ddd"
    specs_html  = (
        f'<div style="font-size:11px;color:#7f8c8d;text-align:right;margin-top:5px;">{specs}</div>'
        if specs else ""
    )
    st.markdown(f"""
<div style="background-color:{bg_color};padding:10px;border-radius:8px;margin-bottom:8px;
            border:{border};display:flex;flex-direction:column;overflow-x:auto;">
  <div style="display:flex;align-items:center;">
    <div style="font-weight:800;color:{label_color};font-size:14px;min-width:60px;
                margin-right:10px;white-space:nowrap;flex-shrink:0;text-align:center;
                padding:5px;border-radius:5px;">{label}</div>
    <div style="display:flex;flex-wrap:nowrap;align-items:center;gap:2px;">{balls_html}</div>
  </div>
  {specs_html}
</div>
""", unsafe_allow_html=True)

def stat_box(value: str, title: str, color: str = "#333") -> str:
    return (
        f'<div class="stat-box">'
        f'<div class="stat-number" style="color:{color};">{value}</div>'
        f'<div class="stat-title">{title}</div>'
        f'</div>'
    )

def get_prize_label(match: int, has_bonus: bool) -> tuple[str, bool]:
    """(등수 레이블, 하이라이트 여부) 반환."""
    if   match == 6:               return "🎉 1등 당첨!", True
    elif match == 5 and has_bonus: return "✨ 2등 당첨!", True
    elif match == 5:               return "👍 3등 당첨",  True
    elif match == 4:               return "4등",          False
    elif match == 3:               return "5등",          False
    else:                          return "낙첨",         False


# ==========================================
# [5] 페이지 설정 및 스타일
# ==========================================
st.set_page_config(page_title="인공지능 로또 분석기", page_icon="🎱")

st.markdown("""
<style>
html, body, [class*="css"] { font-family: "Malgun Gothic", sans-serif; }
.block-container { padding-top: 1.5rem; padding-bottom: 3rem; }
@media (max-width: 600px) {
    .block-container { padding-left: 0.5rem; padding-right: 0.5rem; }
}
.stat-box {
    background-color: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 10px;
    padding: 15px;
    text-align: center;
    margin-bottom: 10px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.05);
}
.stat-number { font-size: 22px; font-weight: bold; }
.stat-title  { font-size: 13px; color: #666; margin-top: 5px; }
[data-testid="stToolbar"] { visibility: hidden !important; display: none !important; }
header { visibility: hidden !important; }
footer { visibility: hidden !important; }
</style>
""", unsafe_allow_html=True)


# ==========================================
# [6] 세션 상태 초기화 및 PC/모바일 양방향 동기화
# ==========================================
SETTING_DEFAULTS = {
    "count":   10,
    "weight":  100,
    "trend":   True,
    "cold":    True,
    "omr":     True,
    "end":     True,
    "dead":    True,
    "stats":   True,
    "consec":  True,
    "prime":   True,
    "ac":      True,
    "section": True,
    "multi":   True,
    "pop":     True,
    "fixed":    [],
    "excluded": [],
}

for name, default_val in SETTING_DEFAULTS.items():
    cfg_key = f"cfg_{name}"
    sb_key  = f"sb_{name}"
    mb_key  = f"mb_{name}"
    if cfg_key not in st.session_state:
        st.session_state[cfg_key] = list(default_val) if isinstance(default_val, list) else default_val
    if sb_key not in st.session_state:
        st.session_state[sb_key] = list(st.session_state[cfg_key]) if isinstance(default_val, list) else st.session_state[cfg_key]
    if mb_key not in st.session_state:
        st.session_state[mb_key] = list(st.session_state[cfg_key]) if isinstance(default_val, list) else st.session_state[cfg_key]

for key, default in {
    "is_generating": False,
    "recent_generated_games": [],
    "last_save_to_sheet": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def sync_setting(name: str, source_prefix: str):
    """PC 사이드바(sb_*)와 모바일 설정창(mb_*)의 값을 실시간으로 양방향 동기화합니다."""
    src_key = f"{source_prefix}_{name}"
    dst_prefix = "mb" if source_prefix == "sb" else "sb"
    dst_key = f"{dst_prefix}_{name}"
    cfg_key = f"cfg_{name}"

    val = st.session_state[src_key]
    st.session_state[cfg_key] = list(val) if isinstance(val, list) else val
    st.session_state[dst_key] = list(val) if isinstance(val, list) else val


def refresh_all_data():
    """당첨 데이터 및 구글 시트 이력 캐시를 초기화하여 서버와 즉시 재동기화합니다."""
    _fetch_lotto_data_cached.clear()
    _fetch_prize_cached.clear()
    _load_sheet_records_cached.clear()


# ==========================================
# [7] 사이드바 (PC)
# ==========================================
with st.sidebar:
    st.header("⚙️ 분석 설정")
    st.button(
        "🔄 실시간 데이터 새로고침",
        use_container_width=True,
        on_click=refresh_all_data,
        help="동행복권 최신 당첨 결과와 구글 시트 서버 저장 이력을 다시 불러옵니다.",
    )
    st.number_input(
        "과거 분석 정보(회)", min_value=5, max_value=100, step=1,
        key="sb_count", on_change=sync_setting, args=("count", "sb")
    )
    st.write("흐름 가중치(%) — 높을수록 최근 번호 우선")
    st.number_input(
        "가중치 입력", min_value=0, step=10,
        key="sb_weight", on_change=sync_setting, args=("weight", "sb")
    )

    st.markdown("---")
    st.subheader("거르기 조건")
    st.checkbox("🔥 흐름 가중치",      key="sb_trend",   on_change=sync_setting, args=("trend", "sb"))
    st.checkbox("❄️ 미출수 부활",      key="sb_cold",    on_change=sync_setting, args=("cold", "sb"))
    st.checkbox("📝 OMR 편중 차단",    key="sb_omr",     on_change=sync_setting, args=("omr", "sb"))
    st.checkbox("⚡ 끝자리 일치",      key="sb_end",     on_change=sync_setting, args=("end", "sb"))
    st.checkbox("☠️ 제외 구간",        key="sb_dead",    on_change=sync_setting, args=("dead", "sb"))
    st.checkbox("📊 통계 정밀 거르기", key="sb_stats",   on_change=sync_setting, args=("stats", "sb"))
    st.checkbox("🔗 이어지는 번호",    key="sb_consec",  on_change=sync_setting, args=("consec", "sb"))
    st.checkbox("🔢 소수 필터",        key="sb_prime",   on_change=sync_setting, args=("prime", "sb"))
    st.checkbox("📐 AC값 필터",        key="sb_ac",      on_change=sync_setting, args=("ac", "sb"))
    st.checkbox("⚖️ 구간 합 균형",     key="sb_section", on_change=sync_setting, args=("section", "sb"))
    st.checkbox("✖️ 배수 편중 차단",   key="sb_multi",   on_change=sync_setting, args=("multi", "sb"))
    st.checkbox(
        "💰 당첨금 분산 방지 (생일수 쏠림 회피)",
        key="sb_pop",
        on_change=sync_setting,
        args=("pop", "sb"),
        help="확률을 높이는 기능이 아닙니다. 1등 당첨 시 상금을 나눠 갖는 인원을 줄이기 위해, "
             "사람들이 몰려 선택하는 1~31 생일수 위주 조합·등차수열 패턴을 피합니다."
    )

    st.markdown("---")
    st.subheader("🎯 번호 고정 / 제외")
    st.multiselect(
        "고정 번호 (반드시 포함)", list(range(1, 46)),
        key="sb_fixed", on_change=sync_setting, args=("fixed", "sb")
    )
    st.multiselect(
        "제외 번호 (절대 미포함)", list(range(1, 46)),
        key="sb_excluded", on_change=sync_setting, args=("excluded", "sb")
    )

    st.markdown("---")
    st.subheader("🔥 최근 핫넘버 TOP 5")
    hot_numbers_slot = st.empty()


# ==========================================
# [8] 데이터 로드
# ==========================================
active_count = int(st.session_state["cfg_count"])
full_data, history_info, epsd_result_map, epsd_prize_map = fetch_lotto_data(active_count)
ai_engine = LottoAI()

if full_data and history_info:
    recent_nums = [n for _, nums, _ in history_info for n in nums]
    top5 = Counter(recent_nums).most_common(5)
    hot_numbers_slot.markdown("".join(
        f"<div style='margin-bottom:5px;'>{get_ball_html(num)}"
        f" <span style='font-size:14px;font-weight:bold;color:#555;'>({freq}회 출현)</span></div>"
        for num, freq in top5
    ), unsafe_allow_html=True)

    latest_epsd     = history_info[0][0]
    target_epsd     = latest_epsd + 1
    history_records = load_history()

    st.title("인공지능 로또 분석기")
    tab_home, tab_stats, tab_history, tab_help = st.tabs([
        "🎯 분석기 홈", "📊 수익률/통계", "📋 생성 이력", "📖 설명서"
    ])

    # ==========================================
    # 탭 1: 분석기 홈
    # ==========================================
    with tab_home:

        with st.expander("⚙️ 분석 설정 (모바일 / 빠른 설정)", expanded=False):
            col_a, col_b = st.columns(2)
            with col_a:
                st.number_input(
                    "분석 회수(회)", min_value=5, max_value=100, step=1,
                    key="mb_count", on_change=sync_setting, args=("count", "mb")
                )
                st.number_input(
                    "흐름 가중치(%)", min_value=0, step=10,
                    key="mb_weight", on_change=sync_setting, args=("weight", "mb")
                )
                st.button(
                    "🔄 데이터 새로고침",
                    key="mb_refresh_btn",
                    use_container_width=True,
                    on_click=refresh_all_data,
                )
            with col_b:
                st.checkbox("🔥 흐름 가중치",      key="mb_trend",   on_change=sync_setting, args=("trend", "mb"))
                st.checkbox("❄️ 미출수 부활",      key="mb_cold",    on_change=sync_setting, args=("cold", "mb"))
                st.checkbox("📝 OMR 편중 차단",    key="mb_omr",     on_change=sync_setting, args=("omr", "mb"))
                st.checkbox("⚡ 끝자리 일치",      key="mb_end",     on_change=sync_setting, args=("end", "mb"))
                st.checkbox("☠️ 제외 구간",        key="mb_dead",    on_change=sync_setting, args=("dead", "mb"))
                st.checkbox("📊 통계 거르기",      key="mb_stats",   on_change=sync_setting, args=("stats", "mb"))
                st.checkbox("🔗 이어지는 번호",    key="mb_consec",  on_change=sync_setting, args=("consec", "mb"))
                st.checkbox("🔢 소수 필터",        key="mb_prime",   on_change=sync_setting, args=("prime", "mb"))
                st.checkbox("📐 AC값 필터",        key="mb_ac",      on_change=sync_setting, args=("ac", "mb"))
                st.checkbox("⚖️ 구간 합 균형",     key="mb_section", on_change=sync_setting, args=("section", "mb"))
                st.checkbox("✖️ 배수 편중 차단",   key="mb_multi",   on_change=sync_setting, args=("multi", "mb"))
                st.checkbox("💰 당첨금 분산 방지", key="mb_pop",     on_change=sync_setting, args=("pop", "mb"))

            st.markdown("**🎯 번호 고정 / 제외**")
            st.multiselect(
                "고정 번호", list(range(1, 46)),
                key="mb_fixed", on_change=sync_setting, args=("fixed", "mb")
            )
            st.multiselect(
                "제외 번호", list(range(1, 46)),
                key="mb_excluded", on_change=sync_setting, args=("excluded", "mb")
            )

            st.markdown(f"**🔥 최근 핫넘버 TOP 5** (최근 {active_count}회 기준)")
            if top5:
                st.markdown("".join(
                    f"<div style='display:inline-block;margin-right:8px;'>{get_ball_html(num)}"
                    f"<span style='font-size:12px;color:#555;'> {freq}회</span></div>"
                    for num, freq in top5
                ), unsafe_allow_html=True)

        # 동기화된 설정값 읽기
        weight_val    = int(st.session_state["cfg_weight"])
        fixed_nums    = sorted(set(st.session_state["cfg_fixed"]))
        excluded_nums = sorted(set(st.session_state["cfg_excluded"]))
        options = {
            "use_trend":           bool(st.session_state["cfg_trend"]),
            "use_cold":            bool(st.session_state["cfg_cold"]),
            "use_omr":             bool(st.session_state["cfg_omr"]),
            "use_end_digit":       bool(st.session_state["cfg_end"]),
            "use_dead_zone":       bool(st.session_state["cfg_dead"]),
            "use_stats":           bool(st.session_state["cfg_stats"]),
            "use_consecutive":     bool(st.session_state["cfg_consec"]),
            "use_prime":           bool(st.session_state["cfg_prime"]),
            "use_ac":              bool(st.session_state["cfg_ac"]),
            "use_section_balance": bool(st.session_state["cfg_section"]),
            "use_multiple":        bool(st.session_state["cfg_multi"]),
            "use_popularity":      bool(st.session_state["cfg_pop"]),
        }

        # 고정/제외 충돌 및 유효성 검사
        conflict = set(fixed_nums) & set(excluded_nums)
        available_pool_size = 45 - len(set(fixed_nums) | set(excluded_nums))
        needed_count = 6 - len(fixed_nums)

        if conflict:
            st.error(f"고정 번호와 제외 번호가 겹칩니다: {sorted(conflict)} — 수정 후 다시 시도해주세요.")
        elif len(fixed_nums) > 5:
            st.error("고정 번호는 최대 5개까지만 설정할 수 있습니다.")
        elif available_pool_size < needed_count:
            st.error(
                f"제외 번호가 너무 많아 6개를 조합할 수 없습니다. "
                f"(현재 선택 가능 번호: {available_pool_size}개 / 필요 번호: {needed_count}개)"
            )
        else:
            # 고정/제외 미리보기
            if fixed_nums or excluded_nums:
                pc1, pc2 = st.columns(2)
                with pc1:
                    if fixed_nums:
                        st.markdown("**🎯 고정 번호**")
                        st.markdown(
                            "".join(get_ball_html(n) for n in fixed_nums),
                            unsafe_allow_html=True,
                        )
                with pc2:
                    if excluded_nums:
                        st.markdown("**🚫 제외 번호**")
                        st.markdown("".join(
                            f'<div style="display:inline-flex;justify-content:center;align-items:center;'
                            f'width:32px;height:32px;border-radius:50%;background-color:#ccc;'
                            f'color:#666;font-weight:bold;font-size:13px;margin-right:3px;'
                            f'text-decoration:line-through;">{n}</div>'
                            for n in excluded_nums
                        ), unsafe_allow_html=True)
                st.markdown("")

            st.button(
                f"🚀 {target_epsd}회차 번호 뽑기 시작",
                type="primary",
                use_container_width=True,
                disabled=st.session_state.is_generating,
                on_click=lambda: st.session_state.update(is_generating=True),
            )
            st.markdown("---")

            if st.session_state.is_generating:
                with st.spinner("최적의 번호를 계산 중입니다..."):
                    games = generate_ai_games(
                        full_data,
                        weight_val,
                        options,
                        fixed_nums,
                        excluded_nums,
                        scope=active_count,
                    )
                if games:
                    with st.spinner("구글 시트에 저장 중... (최대 3회 재시도)"):
                        saved_to_sheet = save_history(target_epsd, games)
                    st.session_state.recent_generated_games = games
                    st.session_state.last_save_to_sheet     = saved_to_sheet
                st.session_state.is_generating = False
                st.rerun()

            if st.session_state.recent_generated_games and not st.session_state.is_generating:
                st.markdown(f"### ✨ 새로 뽑힌 추천 번호 ({target_epsd}회차용)")
                for i, game in enumerate(st.session_state.recent_generated_games):
                    draw_row(f"세트 {i + 1}", game, specs=ai_engine.get_specs(game))
                if st.session_state.get("last_save_to_sheet"):
                    st.success("생성 및 구글 시트 저장 완료! 최신 데이터가 통계 탭에 반영되었습니다. 🍀")
                else:
                    st.warning("번호 생성 완료. 구글 시트 저장에 실패하여 로컬 파일에 저장했습니다. 📁")
                st.markdown("<br>", unsafe_allow_html=True)

        with st.expander(f"📋 최근 {active_count}회 당첨 결과 확인하기", expanded=True):
            for epsd, nums, bonus in history_info:
                draw_row(f"{epsd}회", nums, is_header=True, bonus=bonus)

    # ==========================================
    # 탭 2: 수익률/통계 + 전체 이력 분석 + 빈도 차트
    # ==========================================
    with tab_stats:
        latest_nums  = set(history_info[0][1])
        latest_bonus = history_info[0][2]

        total_games_last_week = 0
        this_week_usage_count = 0
        prize_counts  = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, "fail": 0}
        winning_games = []

        all_time_prize_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, "fail": 0}
        all_time_total = 0

        for record in history_records:
            rec_epsd  = record.get("epsd")
            rec_games = record.get("games", [])

            if rec_epsd == target_epsd:
                this_week_usage_count += len(rec_games)

            if rec_epsd == latest_epsd:
                for game in rec_games:
                    total_games_last_week += 1
                    match     = len(set(game) & latest_nums)
                    has_bonus = latest_bonus in game
                    label, _  = get_prize_label(match, has_bonus)
                    if   match == 6:               prize_counts[1] += 1; winning_games.append((label, game))
                    elif match == 5 and has_bonus: prize_counts[2] += 1; winning_games.append((label, game))
                    elif match == 5:               prize_counts[3] += 1; winning_games.append((label, game))
                    elif match == 4:               prize_counts[4] += 1
                    elif match == 3:               prize_counts[5] += 1
                    else:                          prize_counts["fail"] += 1

            # 전체 회차 당첨 결과 맵(epsd_result_map)과 대조하여 과거 전체 누적 이력 집계
            if rec_epsd in epsd_result_map:
                w_nums, w_bonus = epsd_result_map[rec_epsd]
                for game in rec_games:
                    all_time_total += 1
                    match     = len(set(game) & w_nums)
                    has_bonus = w_bonus in game
                    if   match == 6:               all_time_prize_counts[1] += 1
                    elif match == 5 and has_bonus: all_time_prize_counts[2] += 1
                    elif match == 5:               all_time_prize_counts[3] += 1
                    elif match == 4:               all_time_prize_counts[4] += 1
                    elif match == 3:               all_time_prize_counts[5] += 1
                    else:                          all_time_prize_counts["fail"] += 1

        # 이번 주 배너
        st.markdown(f"""
<div style="background:linear-gradient(135deg,#2c3e50 0%,#3498db 100%);padding:20px;
            border-radius:10px;text-align:center;color:white;margin-bottom:20px;">
  <div style="font-size:15px;opacity:0.9;margin-bottom:5px;">현재 준비 중인 {target_epsd}회차 대비</div>
  <div style="font-size:24px;font-weight:bold;">
    이번 주 총 <span style="font-size:32px;color:#f1c40f;">{this_week_usage_count:,}</span> 게임의 분석이 진행되었습니다.
  </div>
</div>
""", unsafe_allow_html=True)

        # ROI
        st.subheader(f"📈 {latest_epsd}회차 투자 대비 수익률 (ROI)")
        if total_games_last_week == 0:
            st.info(f"아직 데이터베이스에 {latest_epsd}회차 생성 기록이 없습니다.")
        else:
            prizes      = fetch_prize_info(latest_epsd, epsd_prize_map)
            total_spent = total_games_last_week * 1_000
            first_prize = prizes[1] if prizes[1] is not None else 0
            total_won   = (
                prize_counts[1] * first_prize +
                prize_counts[2] * prizes[2] +
                prize_counts[3] * prizes[3] +
                prize_counts[4] * prizes[4] +
                prize_counts[5] * prizes[5]
            )
            roi = (total_won / total_spent * 100) if total_spent > 0 else 0
            first_prize_label = f"약 {first_prize / 100_000_000:.1f}억" if first_prize else "확인불가"
            second_prize_label = f"약 {prizes[2] // 10_000:,}만" if prizes.get(2) else "약 5천만"
            third_prize_label  = f"약 {prizes[3] // 10_000:,}만" if prizes.get(3) else "약 150만"

            st.markdown(f"""
<div style="display:flex;flex-direction:row;justify-content:space-around;
            background-color:#f1f3f5;padding:20px;border-radius:10px;margin-bottom:20px;">
  <div style="text-align:center;">
    <div style="font-size:14px;color:#555;">총 투자 금액</div>
    <div style="font-size:22px;font-weight:bold;color:#333;">{total_spent:,} 원</div>
  </div>
  <div style="text-align:center;">
    <div style="font-size:14px;color:#555;">총 당첨 금액</div>
    <div style="font-size:22px;font-weight:bold;color:#E74C3C;">{total_won:,} 원</div>
  </div>
  <div style="text-align:center;">
    <div style="font-size:14px;color:#555;">프로그램 수익률 (ROI)</div>
    <div style="font-size:22px;font-weight:bold;color:#2980B9;">{roi:,.1f} %</div>
  </div>
</div>
""", unsafe_allow_html=True)

            st.markdown(f"**총 {total_games_last_week:,}게임 중 당첨 내역**")
            c1, c2, c3 = st.columns(3)
            with c1: st.markdown(stat_box(f"{prize_counts[1]:,} 회", f"1등 ({first_prize_label})",  "#C0392B"), unsafe_allow_html=True)
            with c2: st.markdown(stat_box(f"{prize_counts[2]:,} 회", f"2등 ({second_prize_label})", "#8E44AD"), unsafe_allow_html=True)
            with c3: st.markdown(stat_box(f"{prize_counts[3]:,} 회", f"3등 ({third_prize_label})",  "#2980B9"), unsafe_allow_html=True)
            c4, c5, c6 = st.columns(3)
            with c4: st.markdown(stat_box(f"{prize_counts[4]:,} 회", "4등 (5만 원)",  "#F39C12"), unsafe_allow_html=True)
            with c5: st.markdown(stat_box(f"{prize_counts[5]:,} 회", "5등 (5천 원)",  "#27AE60"), unsafe_allow_html=True)
            with c6: st.markdown(stat_box(f"{prize_counts['fail']:,} 회", "낙첨",     "#7F8C8D"), unsafe_allow_html=True)

            if winning_games:
                st.markdown("---")
                st.markdown("#### ✨ 축하합니다! 상위권 당첨 번호")
                for label, game in winning_games:
                    draw_row(label, game, highlight=True)

        # 전체 이력 필터 효과 분석
        st.markdown("---")
        st.subheader("🔬 전체 이력 기반 필터 효과 분석")
        if all_time_total == 0:
            st.info("추첨 완료된 회차에 대한 분석 이력 데이터가 아직 없습니다. 번호를 생성하면 누적 통계가 쌓입니다.")
        else:
            hit_rate = (all_time_total - all_time_prize_counts["fail"]) / all_time_total * 100
            st.markdown(f"""
<div style="background:#f8f9fa;border-radius:10px;padding:15px;margin-bottom:15px;border:1px solid #dee2e6;">
  <div style="font-size:14px;color:#555;margin-bottom:4px;">누적 분석 게임 수 (추첨 완료 회차 기준)</div>
  <div style="font-size:28px;font-weight:bold;color:#2c3e50;">{all_time_total:,} 게임</div>
  <div style="font-size:14px;color:#27AE60;margin-top:4px;">전체 적중률 (5등 이상): <b>{hit_rate:.2f}%</b></div>
</div>
""", unsafe_allow_html=True)
            ca, cb, cc = st.columns(3)
            with ca: st.markdown(stat_box(f"{all_time_prize_counts[1]:,}", "누적 1등", "#C0392B"), unsafe_allow_html=True)
            with cb: st.markdown(stat_box(f"{all_time_prize_counts[2]:,}", "누적 2등", "#8E44AD"), unsafe_allow_html=True)
            with cc: st.markdown(stat_box(f"{all_time_prize_counts[3]:,}", "누적 3등", "#2980B9"), unsafe_allow_html=True)
            cd, ce, cf = st.columns(3)
            with cd: st.markdown(stat_box(f"{all_time_prize_counts[4]:,}", "누적 4등", "#F39C12"), unsafe_allow_html=True)
            with ce: st.markdown(stat_box(f"{all_time_prize_counts[5]:,}", "누적 5등", "#27AE60"), unsafe_allow_html=True)
            with cf: st.markdown(stat_box(f"{all_time_prize_counts['fail']:,}", "누적 낙첨", "#7F8C8D"), unsafe_allow_html=True)

        # 번호별 출현 빈도 바 차트
        st.markdown("---")
        st.subheader(f"📊 최근 {active_count}회 번호별 출현 빈도")
        freq_dict = Counter(recent_nums)
        df_chart = pd.DataFrame({
            "출현 횟수": [freq_dict.get(i, 0) for i in range(1, 46)]
        }, index=[f"{i}번" for i in range(1, 46)])
        st.bar_chart(df_chart, color="#2980B9")

    # ==========================================
    # 탭 3: 번호 생성 이력
    # ==========================================
    with tab_history:
        st.subheader("📋 번호 생성 전체 이력")
        show_only_wins = st.checkbox("🏆 3등 이상 당첨 이력만 모아보기", value=False)

        if not history_records:
            st.info("아직 생성된 번호 이력이 없습니다.")
        else:
            epsd_groups: dict = {}
            for record in history_records:
                e = record.get("epsd")
                if e not in epsd_groups:
                    epsd_groups[e] = []
                epsd_groups[e].extend(record.get("games", []))

            for epsd in sorted(epsd_groups.keys(), reverse=True):
                games_list = epsd_groups[epsd]
                won_set, won_bonus = epsd_result_map.get(epsd, (None, None))

                # 3등 이상 당첨 여부 판별
                has_upper_win = any(
                    len(set(g) & won_set) >= 5
                    for g in games_list
                ) if won_set else False

                if show_only_wins and not has_upper_win:
                    continue

                suffix = ""
                if epsd == target_epsd:   suffix = " ← 이번 주 준비 중"
                elif epsd == latest_epsd: suffix = " ← 지난 주"

                with st.expander(
                    f"🗓️ {epsd}회차 — {len(games_list):,}게임{suffix}",
                    expanded=(epsd == target_epsd or has_upper_win),
                ):
                    if won_set:
                        st.markdown("**해당 회차 당첨 번호**")
                        draw_row("당첨", sorted(list(won_set)), is_header=True, bonus=won_bonus)
                        st.markdown("---")

                    for i, game in enumerate(games_list):
                        specs_str = ai_engine.get_specs(game)
                        if won_set:
                            match       = len(set(game) & won_set)
                            has_bonus   = won_bonus in game
                            lbl, hilite = get_prize_label(match, has_bonus)
                            draw_row(f"#{i+1} {lbl}", game, specs=specs_str, highlight=hilite)
                        else:
                            draw_row(f"#{i+1}", game, specs=specs_str)

    # ==========================================
    # 탭 4: 설명서
    # ==========================================
    with tab_help:
        st.subheader("💡 원리와 솔직한 한계")
        st.write(
            "먼저 분명히 말씀드립니다 — **6개 번호 조합은 어떤 것이든 당첨 확률이 정확히 동일합니다.** "
            "로또 추첨은 매회 독립적인 사건이라 과거 데이터로 다음 회차의 '더 잘 맞을 조합'을 골라낼 방법은 "
            "수학적으로 존재하지 않습니다. 아래 필터들 대부분은 무작위 조합도 대체로 자연히 만족하는 "
            "범위(합계, 홀짝, 소수 개수 등)를 확인해 극단적으로 튀는 조합만 걸러내는 것으로, "
            "**당첨 확률을 높이는 기능이 아니라 '이상해 보이는 조합을 피하는' 정도의 의미**입니다.\n\n"
            "다만 한 가지는 실질적인 효과가 있습니다. 로또645 1등 상금은 당첨자 수만큼 나눠 갖는 방식이라, "
            "사람들이 유독 몰려서 고르는 패턴(생일수 1~31, 등차수열 등)을 피하면 — 맞힐 확률 자체는 그대로여도 — "
            "**맞혔을 때 나눠 가질 사람이 줄어 기대 배당금은 올라갑니다.** '💰 당첨금 분산 방지' 필터가 그 목적입니다."
        )
        st.markdown("---")

        filters = [
            ("💰 당첨금 분산 방지 (Popularity Avoidance)", "success",
             "**유일하게 기대값에 실질적 영향을 주는 필터입니다.** 당첨 확률이 아니라, 당첨 시 상금을 나눠 갖는 "
             "인원을 줄이는 목적입니다. 6개 전부 1~31(생일수) 조합이나 완전 등차수열처럼 사람들이 몰려서 "
             "고르는 패턴을 피합니다."),
            ("🔥 흐름 가중치 (Trend Weight)",          "info",
             "최근 분석 회차에서 자주 나온 번호에 약간의 가중치를 둡니다. 참고: 로또는 독립 시행이라 과거 출현 빈도가 "
             "다음 회차 확률에 영향을 준다는 근거는 없습니다. 재미 요소로 생각해 주세요."),
            ("❄️ 미출수 부활 (Cold Number)",            "info",
             "최근 분석 회차 동안 나오지 않은 번호를 1개 이상 포함시킵니다. '회귀의 법칙'은 통계적으로 검증된 개념이 "
             "아니며, 이 역시 확률을 바꾸지는 않습니다."),
            ("📝 OMR 편중 차단 (OMR Pattern)",          "warning",
             "실제 로또 OMR 용지(5열 9행) 기준, 같은 가로줄이나 세로줄에 번호가 4개 이상 몰리는 조합을 "
             "제외합니다. 무작위 조합 대부분이 자연히 만족하는 범위라 확률에는 영향이 없습니다."),
            ("⚡ 끝자리 일치 (End Digit Sync)",         "warning",
             "역대 당첨 번호 중 끝자리가 같은 숫자가 1쌍 이상 포함된 비율이 높다는 통계적 사실을 반영한 "
             "필터입니다. 무작위 조합도 대부분 이 조건을 만족하므로 실질적 배제 효과는 크지 않습니다."),
            ("☠️ 제외 구간 (Dead Zone)",                "warning",
             "9개 구간 중 2개 이상이 비어 있는 분산 패턴을 강제합니다. 확률에는 영향이 없는 취향성 필터입니다."),
            ("📊 통계 정밀 거르기 (Statistical Filter)", "warning",
             "6개 번호의 합이 100~175 범위를 벗어나거나 홀수/짝수가 6개 몰리는 극단적 조합을 제외합니다."),
            ("🔗 이어지는 번호 (Consecutive Rule)",     "info",
             "실제 당첨 번호 중 연속 번호가 포함된 비율이 절반 이상이라는 통계에 맞춰 이 패턴을 일부 포함시킵니다. "
             "확률을 높이는 것이 아니라 과거 분포와 비슷한 모양을 만드는 것입니다."),
            ("🔢 소수 필터 (Prime Filter)",             "warning",
             "소수가 0개 또는 5개 이상인 극단적 조합만 제외합니다."),
            ("📐 AC값 필터 (Arithmetic Complexity)",    "warning",
             "번호 간 차이값의 종류 수가 너무 적어 지나치게 규칙적인 조합을 제외합니다."),
            ("⚖️ 구간 합 균형 (Section Balance)",       "warning",
             "전반부(1~22)와 후반부(23~45) 합 차이가 50 이상인 극단적 편중 조합을 제외합니다."),
            ("✖️ 배수 편중 차단 (Multiple Filter)",     "warning",
             "3의 배수 4개 이상, 5의 배수 3개 이상 몰린 조합을 제외합니다."),
            ("🎯 번호 고정 / 제외",                     "info",
             "특정 번호를 반드시 포함하거나 완전히 제외하여 나만의 조합 전략을 세울 수 있습니다. 고정 번호는 최대 5개까지 설정 가능합니다."),
        ]

        for title, style, desc in filters:
            st.markdown(f"#### {title}")
            getattr(st, style)(desc)

        st.markdown("---")
        st.error(
            "### ⚠️ 꼭 읽어주세요 (면책 조항)\n"
            "**이 프로그램의 어떤 필터도 당첨 확률을 높이지 않습니다.** 6/45 조합은 모두 확률이 동일하며, "
            "로또 추첨은 완전히 독립적인 시행이라 과거 데이터로 다음 회차를 예측할 수 없습니다. "
            "'💰 당첨금 분산 방지' 필터만 예외적으로, 당첨 시 나눠 가질 인원을 줄여 기대 배당금을 "
            "높이는 방향으로 작동하지만 — 이것도 당첨 자체를 보장하거나 확률을 바꾸지는 못합니다.\n\n"
            "본 프로그램을 통해 생성된 번호로 발생한 결과에 대한 책임은 전적으로 사용자 본인에게 있습니다. "
            "**로또는 반드시 부담 없는 소액으로, 건전하고 즐거운 마음으로만 즐겨 주시기 바랍니다.**"
        )

else:
    st.error("서버에서 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
    st.warning(f"디버그 에러 로그: {history_info}")
    if st.button("🔄 다시 시도"):
        refresh_all_data()
        st.rerun()
