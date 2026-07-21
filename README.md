# 송강실내테니스장 빈자리 알림 봇

대전시설관리공단 송강실내테니스장 1~4 코트의 예약가능 자리를 5분마다 감시해,
새로 열린 자리가 생기면 텔레그램으로 알려주는 봇입니다.

- **실행**: GitHub Actions (5분 cron) — 내 PC 는 꺼져 있어도 됨
- **상태 저장**: Upstash Redis (무료)
- **알림·설정**: 텔레그램 봇
- **비용**: 공개(public) 리포로 만들면 월 0원

---

## 동작 방식

5분마다 GitHub Actions 가 `checker.py` 를 실행하고, 한 사이클에 아래를 처리합니다.

1. 텔레그램으로 들어온 설정 명령(`/기간`, `/코트`, `/on` ...)을 읽어 반영
2. 감시 대상 코트의 예약가능 날짜를 조회 (`state_cd=10` 만)
3. 직전 사이클 대비 **새로 열린 자리만** 텔레그램으로 알림

같은 자리로 5분마다 반복 알림이 오지 않도록, 이미 알린 자리는 Upstash 에 기록해 두고
그 자리가 사라지면 기록에서 지웁니다(다시 열리면 재알림).

---

## 설정 순서

### 1. 텔레그램 봇 만들기

1. 텔레그램에서 **@BotFather** 에게 `/newbot` → 봇 이름 지정 → **봇 토큰** 확보
2. 방금 만든 내 봇에게 아무 메시지나 한 번 보냄
3. 브라우저에서 아래 주소 접속 (`<토큰>` 자리 교체) 해서 `chat.id` 확보
   ```
   https://api.telegram.org/bot<토큰>/getUpdates
   ```
   응답 JSON 의 `"chat":{"id": 123456789 ...}` 숫자가 **chat_id** 입니다.

### 2. Upstash Redis 만들기

1. https://upstash.com 가입 → **Create Database** (Region 은 아무거나)
2. 대시보드의 **REST API** 항목에서 두 값 확보
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`

### 3. GitHub 리포 만들고 올리기

- **반드시 public 리포로 생성** (private 는 Actions 무료 시간을 초과해 요금 발생)
- 이 폴더의 파일들을 리포에 push

### 4. GitHub Secrets 등록

리포 → **Settings → Secrets and variables → Actions → New repository secret** 에서 4개 등록

| 이름 | 값 |
|------|-----|
| `TELEGRAM_BOT_TOKEN` | BotFather 토큰 |
| `TELEGRAM_CHAT_ID` | 위에서 구한 chat_id |
| `UPSTASH_REDIS_REST_URL` | Upstash REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash REST 토큰 |

### 5. 가동 확인

- 리포 → **Actions** 탭 → `court-check` → **Run workflow** 로 한 번 수동 실행
- 텔레그램에서 봇에게 `/상태` 를 보내고 5분 뒤 응답이 오면 정상

---

## 텔레그램 명령어

| 명령 | 설명 |
|------|------|
| `/기간 20260801 20260831` | 감시할 날짜 기간 설정 |
| `/코트 1,3` | 감시할 코트 선택 (생략 시 1~4 전체) |
| `/상태` | 현재 설정 보기 |
| `/on`, `/off` | 알림 켜고 끄기 |
| `/도움말` | 명령 목록 |

날짜는 `20260801`, `2026-08-01`, `8월1일`, `8/1` 아무 형식이나 됩니다.
설정 변경은 다음 조회 사이클(최대 5분 뒤)에 반영됩니다.

---

## 알아둘 점

- GitHub Actions 의 5분 cron 은 최소 간격일 뿐, 서버 부하 시 몇 분 늦거나 한 사이클 건너뛸 수 있습니다.
- 조회 엔드포인트는 기준일부터 약 6주치 달력만 내주므로, 그보다 먼 미래 날짜는 시간이 지나며 잡힙니다.
- 리포에 60일간 아무 커밋이 없으면 GitHub 이 예약 실행을 자동 정지합니다(실사용 중엔 무관).
- 대상 시설이 고정값으로 박혀 있습니다: 송강실내테니스장(`center=DJSISEOL11`, `part=01`, `rent_type=1001`). 다른 시설로 쓰려면 `checker.py` 상단 상수를 바꾸세요.
