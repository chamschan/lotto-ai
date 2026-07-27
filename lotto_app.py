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
# [1] 구글 스프레드시트 연동
# ==========================================
def get_gsheet_client():
    if "gcp_service_account" not in st.secrets or "sheet" not in st.secrets:
        return None
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    return gspread.authorize(creds)


def load_history():
    records = []
    try:
        gc = get_gsheet_client()
        if gc:
            sheet_url = st.secrets["sheet"]["url"]
            worksheet = gc.open_by_url(sheet_url).sheet1
            for row in worksheet.get_all_values():
                if len(row) >= 2:
                    try:
                        records.append({
                            "epsd": int(row[0]),
                            "games": json.loads(row[1]),
                        })
                    except (ValueError, json.JSONDecodeError):
                        continue
            return records
    except Exception as e:
        st.warning(f"구글 시트 불러오기 실패, 로컬 파일로 대체합니다. ({e})")

    if os.path.exists("lotto_history.jsonl"):
        with open("lotto_history.jsonl", "r", encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def save_history(epsd: int, games: list, retries: int = 3, retry_delay: float = 1.5) -> bool:
    gc = get_gsheet_client()
    if gc:
        sheet_url = st.secrets["sheet"]["url"]
        row_data = [epsd, json.dumps(games)]
        for attempt in range(1, retries + 1):
            try:
                worksheet = gc.open_by_url(sheet_url).sheet1
                worksheet.append_row(row_data)
                last_row = worksheet.get_all_values()[-1]
                if len(last_row) >= 2 and str(last_row[0]) == str(epsd):
                    return True
                raise ValueError(f"검증 실패: 저장된 회차({last_row[0]}) != 요청 회차({epsd})")
            except Exception as e:
                if attempt < retries:
                    time.sleep(retry_delay)
                else:
                    st.warning(f"구글 시트 저장 {retries}회 모두 실패, 로컬 파일에 저장합니다. (마지막 오류: {e})")

    with open("lotto_history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"epsd": epsd, "games": games}) + "\n")
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
        """5구간 중 2개 이상이 비어있는지 (분산 패턴)."""
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
) -> list:
    ai = LottoAI()

    if options["use_trend"]:
        trend_weights = ai.analyze_recent_trend(full_data, scope=15)
        extra = weight_percent / 100.0
        base_weights = [
            trend_weights.get(i, 1.0) + extra if trend_weights.get(i, 1.0) > 1.0
            else 1.0
            for i in range(1, 46)
        ]
    else:
        base_weights = [1.0] * 45

    # 제외 번호 가중치 0 처리
    final_weights = [
        0.0 if (i in excluded_nums) else base_weights[i - 1]
        for i in range(1, 46)
    ]

    pool         = [i for i in range(1, 46) if i not in excluded_nums and i not in fixed_nums]
    pool_weights = [final_weights[i - 1] for i in pool]
    needed       = 6 - len(fixed_nums)
    cold_numbers = ai.get_cold_numbers(full_data, scope=15)

    final_games = []
    relaxed_any = False

    if needed < 0:
        st.warning("고정 번호가 6개를 초과합니다. 고정 번호를 줄여주세요.")
        return [sorted(fixed_nums[:6])] * 5

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

            if attempts > 12_000:
                picks = random.sample(pool, min(needed, len(pool)))
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

            if active_options.get("use_omr")             and not ai.passes_omr_filter(candidate):       continue
            if active_options.get("use_cold")            and not ai.has_cold_number(candidate, cold_numbers): continue
            if active_options.get("use_end_digit")       and not ai.has_end_digit_pair(candidate):      continue
            if active_options.get("use_dead_zone")       and not ai.has_dead_zone(candidate):            continue
            if active_options.get("use_stats")           and not ai.passes_stat_filter(candidate):       continue
            if active_options.get("use_prime")           and not ai.passes_prime_filter(candidate):      continue
            if active_options.get("use_ac")              and not ai.passes_ac_filter(candidate):         continue
            if active_options.get("use_section_balance") and not ai.passes_section_balance(candidate):   continue
            if active_options.get("use_multiple")        and not ai.passes_multiple_filter(candidate):   continue
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
# [3] 데이터 가져오기
# ==========================================
@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_lotto_data_cached():
    url = "https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do?srchLtEpsd=all"
    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    res.raise_for_status()
    data = res.json().get("data", {}).get("list", [])
    if not data:
        raise ValueError("API 응답에 데이터가 없습니다.")
    return data


def fetch_lotto_data(count: int):
    try:
        all_list = _fetch_lotto_data_cached()
    except Exception as e:
        return None, str(e)

    all_list = sorted(all_list, key=lambda x: int(x.get("ltEpsd", 0)), reverse=True)

    full_data_flat = []
    for item in all_list:
        nums = [int(item.get(f"tm{i}WnNo", 0)) for i in range(1, 7)]
        full_data_flat.extend(nums)

    history_info = []
    for item in all_list[:count]:
        epsd  = int(item.get("ltEpsd", 0))
        nums  = [int(item.get(f"tm{i}WnNo", 0)) for i in range(1, 7)]
        bonus = int(item.get("bnusNo", 0))
        history_info.append((epsd, nums, bonus))

    return full_data_flat, history_info


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_prize_cached(epsd: int) -> dict:
    url = f"https://www.dhlottery.co.kr/common.do?method=getLottoNumber&drwNo={epsd}"
    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
    res.raise_for_status()
    data = res.json()
    if data.get("returnValue") == "success" and data.get("firstWinamnt", 0) > 0:
        return data
    raise ValueError("당첨금 정보가 아직 업데이트되지 않았습니다.")


def fetch_prize_info(epsd: int) -> dict:
    default_prizes = {1: None, 2: 50_000_000, 3: 1_500_000, 4: 50_000, 5: 5_000}
    try:
        data = _fetch_prize_cached(epsd)
        default_prizes[1] = data.get("firstWinamnt")
    except Exception:
        pass
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
             specs: str = "", highlight: bool = False):
    balls_html  = "".join(get_ball_html(n) for n in balls)
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
    <div style="display:flex;flex-wrap:nowrap;gap:2px;">{balls_html}</div>
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
.mobile-only-settings { display: none; }
@media (max-width: 768px) {
    .mobile-only-settings { display: block; }
}
</style>
""", unsafe_allow_html=True)


# ==========================================
# [6] 세션 상태 초기화
# ==========================================
for key, default in {
    "is_generating": False,
    "recent_generated_games": [],
    "last_save_to_sheet": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ==========================================
# [7] 사이드바 (PC)
# ==========================================
with st.sidebar:
    st.header("⚙️ 분석 설정")
    sb_count_val  = st.number_input("과거 분석 정보(회)", min_value=5, max_value=100, value=10, step=1, key="sb_count")
    st.write("흐름 가중치(%) — 높을수록 최근 번호 우선")
    sb_weight_val = st.number_input("가중치 입력", min_value=0, value=100, step=10, key="sb_weight")

    st.markdown("---")
    st.subheader("거르기 조건")
    sb_use_trend   = st.checkbox("🔥 흐름 가중치",       value=True, key="sb_trend")
    sb_use_cold    = st.checkbox("❄️ 미출수 부활",       value=True, key="sb_cold")
    sb_use_omr     = st.checkbox("📝 OMR 편중 차단",     value=True, key="sb_omr")
    sb_use_end     = st.checkbox("⚡ 끝자리 일치",       value=True, key="sb_end")
    sb_use_dead    = st.checkbox("☠️ 제외 구간",         value=True, key="sb_dead")
    sb_use_stats   = st.checkbox("📊 통계 정밀 거르기",  value=True, key="sb_stats")
    sb_use_consec  = st.checkbox("🔗 이어지는 번호",     value=True, key="sb_consec")
    sb_use_prime   = st.checkbox("🔢 소수 필터",         value=True, key="sb_prime")
    sb_use_ac      = st.checkbox("📐 AC값 필터",         value=True, key="sb_ac")
    sb_use_section = st.checkbox("⚖️ 구간 합 균형",      value=True, key="sb_section")
    sb_use_multi   = st.checkbox("✖️ 배수 편중 차단",    value=True, key="sb_multi")

    st.markdown("---")
    st.subheader("🎯 번호 고정 / 제외")
    sb_fixed    = st.multiselect("고정 번호 (반드시 포함)", list(range(1, 46)), key="sb_fixed")
    sb_excluded = st.multiselect("제외 번호 (절대 미포함)", list(range(1, 46)), key="sb_excluded")

    st.markdown("---")
    st.subheader("🔥 최근 핫넘버 TOP 5")
    hot_numbers_slot = st.empty()


# ==========================================
# [8] 데이터 로드
# ==========================================
full_data, history_info = fetch_lotto_data(sb_count_val)
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
    epsd_result_map = {e: (set(n), b) for e, n, b in history_info}

    st.title("인공지능 로또 분석기")
    tab_home, tab_stats, tab_history, tab_help = st.tabs([
        "🎯 분석기 홈", "📊 수익률/통계", "📋 생성 이력", "📖 설명서"
    ])

    # ==========================================
    # 탭 1: 분석기 홈
    # ==========================================
    with tab_home:

        # 모바일 전용 설정 패널
        st.markdown('<div class="mobile-only-settings">', unsafe_allow_html=True)
        with st.expander("⚙️ 분석 설정 (모바일 전용)", expanded=False):
            col_a, col_b = st.columns(2)
            with col_a:
                mb_count_val  = st.number_input("분석 회수(회)", min_value=5, max_value=100,
                                                value=sb_count_val, step=1, key="mb_count")
                mb_weight_val = st.number_input("흐름 가중치(%)", min_value=0,
                                                value=sb_weight_val, step=10, key="mb_weight")
            with col_b:
                mb_use_trend   = st.checkbox("🔥 흐름 가중치",     value=sb_use_trend,   key="mb_trend")
                mb_use_cold    = st.checkbox("❄️ 미출수 부활",     value=sb_use_cold,    key="mb_cold")
                mb_use_omr     = st.checkbox("📝 OMR 편중 차단",   value=sb_use_omr,     key="mb_omr")
                mb_use_end     = st.checkbox("⚡ 끝자리 일치",     value=sb_use_end,     key="mb_end")
                mb_use_dead    = st.checkbox("☠️ 제외 구간",       value=sb_use_dead,    key="mb_dead")
                mb_use_stats   = st.checkbox("📊 통계 거르기",     value=sb_use_stats,   key="mb_stats")
                mb_use_consec  = st.checkbox("🔗 이어지는 번호",   value=sb_use_consec,  key="mb_consec")
                mb_use_prime   = st.checkbox("🔢 소수 필터",       value=sb_use_prime,   key="mb_prime")
                mb_use_ac      = st.checkbox("📐 AC값 필터",       value=sb_use_ac,      key="mb_ac")
                mb_use_section = st.checkbox("⚖️ 구간 합 균형",    value=sb_use_section, key="mb_section")
                mb_use_multi   = st.checkbox("✖️ 배수 편중 차단",  value=sb_use_multi,   key="mb_multi")

            st.markdown("**🎯 번호 고정 / 제외**")
            mb_fixed    = st.multiselect("고정 번호", list(range(1, 46)), key="mb_fixed")
            mb_excluded = st.multiselect("제외 번호", list(range(1, 46)), key="mb_excluded")

            # 모바일 핫넘버 (mb_count_val 기준으로 별도 로드)
            st.markdown(f"**🔥 최근 핫넘버 TOP 5** (최근 {mb_count_val}회 기준)")
            mb_full_data, mb_history = fetch_lotto_data(mb_count_val)
            if mb_history:
                mb_nums = [n for _, nums, _ in mb_history for n in nums]
                mb_top5 = Counter(mb_nums).most_common(5)
                st.markdown("".join(
                    f"<div style='display:inline-block;margin-right:8px;'>{get_ball_html(num)}"
                    f"<span style='font-size:12px;color:#555;'> {freq}회</span></div>"
                    for num, freq in mb_top5
                ), unsafe_allow_html=True)

        st.markdown('</div>', unsafe_allow_html=True)

        # 실제 사용할 값 (모바일 expander 우선)
        weight_val  = mb_weight_val
        fixed_nums    = list(set(mb_fixed))
        excluded_nums = list(set(mb_excluded))
        options = {
            "use_trend":           mb_use_trend,
            "use_cold":            mb_use_cold,
            "use_omr":             mb_use_omr,
            "use_end_digit":       mb_use_end,
            "use_dead_zone":       mb_use_dead,
            "use_stats":           mb_use_stats,
            "use_consecutive":     mb_use_consec,
            "use_prime":           mb_use_prime,
            "use_ac":              mb_use_ac,
            "use_section_balance": mb_use_section,
            "use_multiple":        mb_use_multi,
        }

        # 고정/제외 충돌 검사
        conflict = set(fixed_nums) & set(excluded_nums)
        if conflict:
            st.error(f"고정 번호와 제외 번호가 겹칩니다: {sorted(conflict)} — 수정 후 다시 시도해주세요.")
        elif len(fixed_nums) > 5:
            st.error("고정 번호는 최대 5개까지만 설정할 수 있습니다.")
        else:
            # 고정/제외 미리보기
            if fixed_nums or excluded_nums:
                pc1, pc2 = st.columns(2)
                with pc1:
                    if fixed_nums:
                        st.markdown("**🎯 고정 번호**")
                        st.markdown(
                            "".join(get_ball_html(n) for n in sorted(fixed_nums)),
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
                            for n in sorted(excluded_nums)
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
                    games = generate_ai_games(full_data, weight_val, options, fixed_nums, excluded_nums)
                with st.spinner("구글 시트에 저장 중... (최대 3회 재시도)"):
                    saved_to_sheet = save_history(target_epsd, games)

                st.session_state.recent_generated_games = games
                st.session_state.last_save_to_sheet     = saved_to_sheet
                st.session_state.is_generating          = False
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

        with st.expander(f"📋 최근 {sb_count_val}회 당첨 결과 확인하기", expanded=True):
            for epsd, nums, _ in reversed(history_info):
                draw_row(f"{epsd}회", nums, is_header=True)

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
    이번 주 총 <span style="font-size:32px;color:#f1c40f;">{this_week_usage_count}</span> 게임의 분석이 진행되었습니다.
  </div>
</div>
""", unsafe_allow_html=True)

        # ROI
        st.subheader(f"📈 {latest_epsd}회차 투자 대비 수익률 (ROI)")
        if total_games_last_week == 0:
            st.info(f"아직 데이터베이스에 {latest_epsd}회차 생성 기록이 없습니다.")
        else:
            prizes      = fetch_prize_info(latest_epsd)
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
            first_prize_label = f"약 {first_prize // 100_000_000}억" if first_prize else "확인불가"

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
            with c1: st.markdown(stat_box(f"{prize_counts[1]:,} 회", f"1등 ({first_prize_label})", "#C0392B"), unsafe_allow_html=True)
            with c2: st.markdown(stat_box(f"{prize_counts[2]:,} 회", "2등 (약 5천만)",            "#8E44AD"), unsafe_allow_html=True)
            with c3: st.markdown(stat_box(f"{prize_counts[3]:,} 회", "3등 (약 150만)",             "#2980B9"), unsafe_allow_html=True)
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
            st.info("분석할 이력 데이터가 없습니다. 번호를 생성하면 누적 통계가 쌓입니다.")
        else:
            hit_rate = (all_time_total - all_time_prize_counts["fail"]) / all_time_total * 100
            st.markdown(f"""
<div style="background:#f8f9fa;border-radius:10px;padding:15px;margin-bottom:15px;border:1px solid #dee2e6;">
  <div style="font-size:14px;color:#555;margin-bottom:4px;">누적 분석 게임 수</div>
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
        st.subheader(f"📊 최근 {sb_count_val}회 번호별 출현 빈도")
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
                    f"🗓️ {epsd}회차 — {len(games_list)}게임{suffix}",
                    expanded=(epsd == target_epsd or has_upper_win),
                ):
                    if won_set:
                        st.markdown("**해당 회차 당첨 번호**")
                        draw_row("당첨", sorted(list(won_set)), is_header=True)
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
        st.subheader("💡 인공지능 분석 원리")
        st.write(
            "이 프로그램은 단순한 무작위 픽이 아닙니다. "
            "역대 당첨 번호의 통계적 사실을 바탕으로 당첨 확률이 극히 희박한 조합을 걸러내어 "
            "효율적인 번호를 추천합니다."
        )
        st.markdown("---")

        filters = [
            ("🔥 흐름 가중치 (Trend Weight)",          "info",
             "최근 15주 자주 나온 'Hot Number'가 당분간 계속 나오는 경향성을 반영하여 해당 번호의 뽑힐 확률을 높입니다."),
            ("❄️ 미출수 부활 (Cold Number)",            "success",
             "최근 15주간 단 한 번도 나오지 않은 '장기 미출수'를 강제로 1개 이상 포함시켜 회귀의 법칙을 적용합니다."),
            ("📝 OMR 편중 차단 (OMR Pattern)",          "error",
             "실제 로또 OMR 용지(5열 9행) 기준, 같은 가로줄이나 세로줄에 번호가 4개 이상 몰리는 비정상적인 패턴을 차단합니다."),
            ("⚡ 끝자리 일치 (End Digit Sync)",         "success",
             "역대 당첨 번호의 약 **85% 이상**은 끝자리가 같은 숫자가 최소 1쌍 이상 포함되어 있습니다."),
            ("☠️ 제외 구간 (Dead Zone)",                "error",
             "특정 번호대가 통째로 전멸하는 현상이 자주 발생합니다. 자연스러운 전멸 구간을 인위적으로 만듭니다."),
            ("📊 통계 정밀 거르기 (Statistical Filter)", "warning",
             "6개 번호의 합이 100~175 범위를 벗어나거나 홀수/짝수가 6개 몰리는 불량 조합을 원천 차단합니다."),
            ("🔗 이어지는 번호 (Consecutive Rule)",     "info",
             "실제로는 50% 이상의 회차에서 연속 번호가 등장합니다. 이 패턴을 일부러 포함시켜 당첨 효율을 높입니다."),
            ("🔢 소수 필터 (Prime Filter)",             "success",
             "1~45 중 소수는 14개입니다. 소수가 0개 또는 5개 이상 포함된 조합은 전체의 10% 미만으로 걸러냅니다."),
            ("📐 AC값 필터 (Arithmetic Complexity)",    "info",
             "6개 번호 간 차이값의 종류 수(AC값)가 7 미만인 조합은 번호들이 너무 규칙적으로 분포된 경우로 확률이 낮습니다."),
            ("⚖️ 구간 합 균형 (Section Balance)",       "success",
             "전반부(1~22) 합과 후반부(23~45) 합의 차이가 50 이상인 극단적 편중 조합을 걸러냅니다."),
            ("✖️ 배수 편중 차단 (Multiple Filter)",     "warning",
             "3의 배수가 4개 이상, 또는 5의 배수가 3개 이상 몰리는 조합은 극히 드뭅니다. 이런 편중 조합을 차단합니다."),
            ("🎯 번호 고정 / 제외",                     "info",
             "특정 번호를 반드시 포함하거나 완전히 제외하여 나만의 조합 전략을 세울 수 있습니다. 고정 번호는 최대 5개까지 설정 가능합니다."),
        ]

        for title, style, desc in filters:
            st.markdown(f"#### {title}")
            getattr(st, style)(desc)

        st.markdown("---")
        st.error(
            "### ⚠️ 꼭 읽어주세요 (면책 조항)\n"
            "이 프로그램은 불필요한 조합을 제외하고 수학적 확률을 높이기 위해 설계되었지만, "
            "**로또 번호 추첨은 독립 시행이며 궁극적으로 '운(Luck)'에 의해 결정됩니다.**\n\n"
            "아무리 뛰어난 인공지능이나 통계 기법을 사용하더라도 100% 당첨을 보장하는 방법은 "
            "이 세상에 존재하지 않습니다. 본 프로그램을 통해 생성된 번호로 발생한 결과에 대한 "
            "책임은 전적으로 사용자 본인에게 있습니다. "
            "**로또는 반드시 부담 없는 소액으로, 건전하고 즐거운 마음으로만 즐겨 주시기 바랍니다.**"
        )

else:
    st.error("서버에서 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
# 디버깅을 위해 추가할 코드 (로그 창에 출력하거나 화면에 바로 띄우기)
    print(f"상세 에러 원인: {e}") 
    st.write(f"상세 에러 원인: {e}")
