#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
코트 빈자리 확인 봇.

GitHub Actions 로 실행될 때마다 아래 세 가지를 한 사이클에 처리한다.
  1) 텔레그램으로 들어온 설정 명령(/기간, /코트, /on ...)을 읽어 반영한다.
  2) 확인 대상 코트의 예약가능 날짜를 조회한다.
  3) 직전 사이클 대비 '새로 열린 자리'만 텔레그램으로 알린다.

사이클 간에 이어져야 하는 상태(설정 / 알림기록 / 텔레그램 offset)는
Upstash Redis(REST) 에 저장한다. 실행이 끝나면 메모리는 사라지므로
모든 지속 상태는 반드시 Upstash 에 넣고 뺀다.
"""

import os
import re
import json
import time
import random
import difflib
import datetime

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 이 사이트는 인증서 체인이 불완전해(중간 인증서 누락) 검증을 끄고 접속한다.
# 그래서 verify=False 로 요청하며, 그때 뜨는 경고 메시지를 여기서 눌러둔다.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 사이트가 순간적으로 느리거나 응답을 안 할 때(타임아웃/일시적 끊김)를 대비해
# 짧게 몇 번 재시도하는 세션. 대부분의 일시적 오류는 재시도에서 자동 회복된다.
_court_retry = Retry(
    total=3, connect=2, read=2, backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
)
COURT_SESSION = requests.Session()
COURT_SESSION.mount("https://", HTTPAdapter(max_retries=_court_retry))

# 사람이 버튼 누르듯, 코트 조회 요청 사이에 두는 간격(초 범위). 연속 요청이 튀지 않게 한다.
REQUEST_MIN_DELAY = 2.0
REQUEST_MAX_DELAY = 5.0


# ---------------------------------------------------------------------------
# 환경변수 (GitHub Secrets 로 주입)
# ---------------------------------------------------------------------------
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]          # 알림 받고 명령 보낼 대상
UPSTASH_URL = os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/")
UPSTASH_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]


# ---------------------------------------------------------------------------
# 확인 대상 고정값 (다른 시설로 쓰려면 이 상수들만 바꾸면 된다)
# ---------------------------------------------------------------------------
CENTER = "DJSISEOL11"        # 센터 코드
PART = "01"                  # 시설 코드
RENT_TYPE = "1001"           # 행사구분(체육행사)
STATE_AVAILABLE = "10"       # 예약가능 상태코드 (20 = 예약불가)

STATE_URL = "https://www.djsiseol.or.kr/res/rest/facilities/place_month_state_list"
RESERVE_PAGE = (
    "https://www.djsiseol.or.kr/res/www/121?action=list"
    "&center=DJSISEOL11&part=01&place={court}&rent_type=1001&base_date={base}"
)

# 빈자리가 없어도 이 간격마다 '확인 중' 생존 신고를 한 번 보낸다(하루 약 2회).
HEARTBEAT_INTERVAL_SEC = 12 * 3600

# 설정이 아직 없을 때 쓰는 기본값. courts 는 place_code(=코트 번호).
DEFAULT_CONFIG = {
    "enabled": True,
    "courts": [1, 2, 3, 4],
    "start_date": None,      # "YYYY-MM-DD" 또는 None(=기간 제한 없음)
    "end_date": None,
}

# 사용자가 칠 만한 표기를 표준 명령으로 모으는 별칭 표.
# 오타가 나도 difflib 근사 매칭으로 이 키들 중 가까운 것을 되묻는다.
COMMAND_ALIASES = {
    "기간": "period", "기한": "period", "period": "period",
    "코트": "court", "court": "court",
    "상태": "status", "현황": "status", "status": "status",
    "on": "on", "켜기": "on", "시작": "on",
    "off": "off", "끄기": "off", "정지": "off",
    "start": "help", "help": "help", "도움말": "help", "도움": "help",
}

HELP_TEXT = (
    "🎾 코트 빈자리 알림 봇\n\n"
    "사용 가능한 명령\n"
    "• /기간 20260801 20260831 — 확인할 날짜 기간 설정\n"
    "• /코트 1,3 — 확인할 코트 선택(생략 시 1~4 전체)\n"
    "• /상태 — 현재 상태 전반 보기\n"
    "• /on, /off — 알림 켜고 끄기\n\n"
    "날짜는 20260801, 2026-08-01, 8월1일, 8/1 아무 형식이나 됩니다.\n"
    "설정은 다음 조회 사이클(최대 10분 뒤)에 반영됩니다."
)


# ---------------------------------------------------------------------------
# Upstash Redis (REST) 헬퍼
#   POST 로 ["CMD", "arg", ...] 배열을 보내면 {"result": ...} 로 돌려준다.
#   서버리스라 연결을 붙들 필요 없이 매 호출이 독립적인 HTTP 요청이다.
# ---------------------------------------------------------------------------
def redis(*cmd):
    r = requests.post(
        UPSTASH_URL,
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        json=list(cmd),
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("result")


def load_json(key, default):
    """Upstash 에 저장된 JSON 문자열을 파이썬 객체로 되살린다."""
    raw = redis("GET", key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def save_json(key, value):
    redis("SET", key, json.dumps(value, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 텔레그램 헬퍼
# ---------------------------------------------------------------------------
def tg_send(text):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": False},
        timeout=15,
    )


def tg_get_updates(offset):
    """offset 이후로 봇에게 온 메시지를 한꺼번에 받아온다(롱폴링 없이 즉시 반환)."""
    r = requests.get(
        f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
        params={"offset": offset, "timeout": 0},
        timeout=20,
    )
    return r.json().get("result", [])


# ---------------------------------------------------------------------------
# 날짜 형식 정규화
#   20260801 / 2026-08-01 / 2026.8.1 / 8월1일 / 8/1 을 모두 "YYYY-MM-DD" 로.
#   연도가 없으면 default_year 를 붙인다.
# ---------------------------------------------------------------------------
def normalize_date(token, default_year):
    token = token.strip()

    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", token)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.fullmatch(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", token)
        if m:
            y, mo, d = m.groups()
        else:
            m = (re.fullmatch(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일?", token)
                 or re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})", token))
            if not m:
                return None
            mo, d = m.groups()
            y = default_year

    try:
        return datetime.date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def closest_command(raw):
    """오타 난 명령과 가장 비슷한 표준 별칭을 하나 찾아 되묻기용으로 돌려준다."""
    matches = difflib.get_close_matches(raw, list(COMMAND_ALIASES.keys()), n=1, cutoff=0.6)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# 텔레그램 명령 처리
# ---------------------------------------------------------------------------
def _fmt_ago(epoch):
    """epoch(UTC초)를 '__분 전 (MM/DD HH:MM)' 형태로. 시각은 KST(UTC+9)로 표기."""
    if not epoch:
        return "아직 없음"
    now = datetime.datetime.now(datetime.timezone.utc)
    then = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
    mins = int((now - then).total_seconds() // 60)
    when = (then + datetime.timedelta(hours=9)).strftime("%m/%d %H:%M")
    if mins < 1:
        ago = "방금"
    elif mins < 60:
        ago = f"{mins}분 전"
    else:
        ago = f"{mins // 60}시간 {mins % 60}분 전"
    return f"{ago} ({when})"


def status_text(config):
    """봇의 전반 상태(설정 + 현재 빈자리 + 마지막 확인 시각)를 한 메시지로 만든다."""
    onoff = "켜짐" if config["enabled"] else "꺼짐"
    courts = ", ".join(f"{c}코트" for c in config["courts"])

    if config["start_date"] and config["end_date"]:
        period = f"{config['start_date']} ~ {config['end_date']}"
        if is_expired(config):
            period += " (⚠️ 만료됨 · /기간 으로 재설정)"
    else:
        period = "제한 없음"

    # 현재 예약가능 자리는 마지막 사이클이 Upstash 에 남긴 기록에서 읽는다(사이트 재조회 X).
    slots = sorted(load_json("notified", []))
    if slots:
        lines = "\n".join(
            f"    - {s.split(':', 1)[0]}코트 {s.split(':', 1)[1]}" for s in slots
        )
        avail = f"{len(slots)}건\n{lines}"
    else:
        avail = "없음"

    return (
        "📋 봇 상태\n"
        f"• 알림: {onoff}\n"
        f"• 확인 코트: {courts}\n"
        f"• 확인 기간: {period}\n"
        f"• 현재 예약가능: {avail}\n"
        f"• 마지막 확인: {_fmt_ago(redis('GET', 'last_run'))}\n"
        "• 실행 주기: 약 10분 (외부 트리거)"
    )


def handle_command(cmd, args, config):
    """명령 하나를 config 에 반영한다. 설정이 바뀌면 True 를 돌려준다."""
    year = datetime.date.today().year

    if cmd == "help":
        tg_send(HELP_TEXT)
        return False

    if cmd == "status":
        tg_send(status_text(config))
        return False

    if cmd == "on":
        config["enabled"] = True
        tg_send("알림을 켰어요. 다음 조회부터 확인합니다.")
        return True

    if cmd == "off":
        config["enabled"] = False
        tg_send("알림을 껐어요.")
        return True

    if cmd == "period":
        if len(args) < 2:
            tg_send("사용법: /기간 20260801 20260831  (시작일 종료일)")
            return False
        s = normalize_date(args[0], year)
        e = normalize_date(args[1], year)
        if not s or not e:
            tg_send("날짜를 못 읽었어요. 예: /기간 2026-08-01 2026-08-31")
            return False
        if s > e:
            s, e = e, s
        config["start_date"], config["end_date"] = s, e
        tg_send(f"확인 기간을 {s} ~ {e} 로 설정했어요.")
        return True

    if cmd == "court":
        nums = sorted({int(n) for n in re.findall(r"[1-4]", " ".join(args))})
        if not nums:
            tg_send("1~4 사이 코트 번호를 알려주세요. 예: /코트 1,2")
            return False
        config["courts"] = nums
        tg_send("확인 코트를 " + ", ".join(f"{n}코트" for n in nums) + " 로 설정했어요.")
        return True

    return False


def process_commands(config):
    """밀린 텔레그램 명령을 순서대로 처리하고 offset 을 전진시킨다."""
    offset = int(redis("GET", "tg_offset") or 0)
    updates = tg_get_updates(offset)
    changed = False

    for up in updates:
        offset = up["update_id"] + 1
        msg = up.get("message") or up.get("edited_message")
        if not msg:
            continue
        # 허가된 사용자(내 chat_id)만 명령을 받아들인다.
        if str(msg.get("chat", {}).get("id")) != str(TG_CHAT_ID):
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        parts = text[1:].split()
        raw_cmd = parts[0].lower()
        # "/기간@봇이름" 형태로 올 수 있어 @ 뒤는 잘라낸다.
        raw_cmd = raw_cmd.split("@", 1)[0]
        args = parts[1:]

        cmd = COMMAND_ALIASES.get(raw_cmd)
        if cmd is None:
            suggestion = closest_command(raw_cmd)
            if suggestion:
                tg_send(
                    f"'/{raw_cmd}' 명령을 못 알아들었어요. 혹시 '/{suggestion}' 인가요?\n"
                    "/도움말 로 전체 명령을 볼 수 있어요."
                )
            else:
                tg_send("모르는 명령이에요. /도움말 로 사용법을 확인하세요.")
            continue

        changed = handle_command(cmd, args, config) or changed

    redis("SET", "tg_offset", str(offset))
    if changed:
        save_json("config", config)
    return config


# ---------------------------------------------------------------------------
# 코트 빈자리 조회
# ---------------------------------------------------------------------------
def fetch_court(court, base_date):
    """한 코트의 한 달치 상태를 받아 예약가능 날짜 목록(YYYY-MM-DD)만 돌려준다."""
    r = COURT_SESSION.post(
        STATE_URL,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.djsiseol.or.kr/res/www/121",
        },
        data={
            "company_code": CENTER,
            "part_code": PART,
            "place_code": str(court),
            "base_date": base_date,
            "rent_type": RENT_TYPE,
            "mem_no": "",
        },
        verify=False,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return [row["date"] for row in data if row.get("state_cd") == STATE_AVAILABLE]


def in_range(date_str, config):
    if config["start_date"] and date_str < config["start_date"]:
        return False
    if config["end_date"] and date_str > config["end_date"]:
        return False
    return True


def check_availability(config):
    """확인 대상 코트를 모두 조회해 현재 예약가능한 'court:date' 집합을 만든다."""
    base = datetime.date.today().strftime("%Y%m%d")
    current = set()
    for i, court in enumerate(config["courts"]):
        # 사람이 버튼 누르듯, 두 번째 코트부터는 요청 전에 잠깐 쉰다.
        if i > 0:
            time.sleep(random.uniform(REQUEST_MIN_DELAY, REQUEST_MAX_DELAY))
        try:
            for d in fetch_court(court, base):
                if in_range(d, config):
                    current.add(f"{court}:{d}")
        except Exception:  # 한 코트가 실패해도 나머지는 계속 조회
            tg_send(
                f"⚠️ {court}코트 조회를 잠시 건너뛰었어요 "
                "(사이트 응답 지연). 다음 확인 때 다시 시도합니다."
            )
    return current


def format_alert(slots):
    """새로 열린 자리들을 코트+날짜로 정리하고, 자리마다 맞는 예약 링크를 붙인다.

    링크의 place 는 그 자리의 코트로, base_date 는 그 자리의 날짜로 맞춘다.
    (예: 1코트 2026-07-22 → place=1, base_date=20260722)
    """
    lines = ["🎾 새로 예약가능한 자리가 생겼어요!", ""]
    for s in sorted(slots):
        court, date = s.split(":")
        base = date.replace("-", "")  # 2026-07-22 → 20260722
        url = RESERVE_PAGE.format(court=court, base=base)
        lines.append(f"• {court}코트 — {date}\n  예약: {url}")
    return "\n".join(lines)


def watching_text(config, current):
    """'확인 중' 메시지 본문을 만든다(생존 신고와 빈자리 소멸 통보가 공유)."""
    courts = ", ".join(f"{c}코트" for c in config["courts"])
    period = (
        f"{config['start_date']} ~ {config['end_date']}"
        if config["start_date"] and config["end_date"] else "제한 없음"
    )
    status = (
        f"현재 예약가능: {len(current)}건 (상세는 별도 알림)"
        if current else "현재 예약가능한 자리: 없음"
    )
    return (
        "🎾 확인 중입니다.\n"
        f"{status}\n"
        f"• 코트: {courts}\n"
        f"• 기간: {period}"
    )


def send_watching(config, current):
    """'확인 중' 메시지를 보내고, 생존 신고 타이머(last_heartbeat)를 갱신한다.

    타이머를 갱신하므로, 방금 이 메시지를 보냈다면 곧이어 12시간 생존 신고가
    중복으로 나가지 않는다.
    """
    tg_send(watching_text(config, current))
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    redis("SET", "last_heartbeat", str(now))


def maybe_heartbeat(config, current):
    """빈자리가 없어도 주기적으로 '확인 중' 상태를 보내 봇 생존을 확인시킨다.

    마지막 신고 시각을 Upstash 에 두고, HEARTBEAT_INTERVAL_SEC(12시간)이
    지났을 때만 한 번 보낸다. 첫 실행(last=0)에는 바로 한 번 나간다.
    """
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    last = redis("GET", "last_heartbeat")
    last = int(last) if last else 0
    if now - last < HEARTBEAT_INTERVAL_SEC:
        return
    send_watching(config, current)


def is_expired(config):
    """확인 종료일이 오늘보다 과거면 True. 기간 제한이 없으면 만료 개념이 없다."""
    end = config.get("end_date")
    if not end:
        return False
    return datetime.date.today().isoformat() > end


def notify_expiry_once(config):
    """확인 기간이 끝났음을 한 번만 안내한다.

    이미 안내한 종료일은 Upstash 에 기록해, 같은 만료로 매 사이클 반복 안내하지 않는다.
    사용자가 /기간 으로 새 기간을 잡으면 종료일이 달라져 다음 만료 때 다시 안내된다.
    """
    end = config.get("end_date")
    if redis("GET", "expiry_notified") == end:
        return
    tg_send(
        "⏰ 확인 기간이 종료되었습니다.\n"
        f"(설정된 기간: {config.get('start_date')} ~ {end})\n"
        "새로 확인하려면 /기간 20260801 20260831 처럼 다시 설정해주세요."
    )
    redis("SET", "expiry_notified", end)


# ---------------------------------------------------------------------------
# 메인 사이클
# ---------------------------------------------------------------------------
def main():
    config = load_json("config", dict(DEFAULT_CONFIG))
    for k, v in DEFAULT_CONFIG.items():  # 예전 설정에 빠진 키가 있으면 기본값으로 보정
        config.setdefault(k, v)

    # 1) 밀린 명령 먼저 반영 (예: 방금 /on 을 눌렀으면 이번 사이클부터 확인)
    process_commands(config)

    # 2) 알림이 꺼져 있으면 조회하지 않고 종료
    if not config.get("enabled"):
        return

    # 2-1) 확인 기간이 지났으면 1회 안내하고 이번 사이클은 확인 중단
    if is_expired(config):
        notify_expiry_once(config)
        return

    # 3) 현재 빈자리 조회
    current = check_availability(config)

    # 3-1) '마지막 확인 시각'을 남긴다(/상태 에서 봇 생존·최신성 확인용).
    redis("SET", "last_run", str(int(datetime.datetime.now(datetime.timezone.utc).timestamp())))

    # 4) 직전 기록과 비교
    notified = set(load_json("notified", []))
    new_slots = current - notified
    if new_slots:
        # 4-a) 새로 열린 자리가 있으면 상세 알림
        tg_send(format_alert(new_slots))
    elif notified and not current:
        # 4-b) 있던 자리가 모두 사라져 다시 빈자리 없음 → '확인 중' 통보
        send_watching(config, current)

    # 5) 지금 열려 있는 자리만 기록으로 남긴다.
    #    사라진 자리는 빠지므로, 나중에 다시 열리면 새 알림으로 잡힌다.
    save_json("notified", sorted(current))

    # 6) 빈자리가 없어도 하루 약 2회 '확인 중' 생존 신고를 보낸다.
    maybe_heartbeat(config, current)


if __name__ == "__main__":
    main()
