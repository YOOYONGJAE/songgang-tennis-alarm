# 코트 빈자리 알림 봇

온라인 예약 사이트에서 코트의 예약가능 자리를 주기적으로 확인해,
새로 열린 자리가 생기면 텔레그램으로 알려주는 봇입니다.

- **실행**: GitHub Actions — 내 PC 는 꺼져 있어도 됨
- **주기 트리거**: cron-job.org (외부에서 약 10분마다 실행을 깨움)
- **상태 저장**: Upstash Redis (무료)
- **알림·설정**: 텔레그램 봇
- **비용**: 공개(public) 리포 + 무료 등급으로 월 0원

---

## 동작 방식

cron-job.org 가 약 10분마다 GitHub 워크플로를 깨우고, `checker.py` 가 한 사이클에 아래를 처리합니다.

1. 텔레그램으로 들어온 설정 명령(`/기간`, `/코트`, `/on` ...)을 읽어 반영
2. 대상 코트에서 예약가능한 날짜를 먼저 찾고, 그 날짜에만 들어가 예약가능한 **시간(회차)** 까지 확인 (요청 사이에 사람처럼 간격을 둠)
3. 직전 사이클 대비 **새로 열린 자리만** 코트·날짜·시간과 예약 링크로 알림

이 밖에 아래 상황도 텔레그램으로 알려줍니다.

- **자리가 모두 사라져 다시 0이 되면** — 빈자리 없음 상태를 한 번 통보
- **일정 주기(기본 12시간)마다** — 봇이 살아 있음을 알리는 상태 메시지
- **확인 기간이 지나면** — 기간이 끝났으니 다시 설정하라고 1회 안내

같은 자리로 매 사이클 반복 알림이 오지 않도록, 이미 알린 자리는 Upstash 에 기록해 두고
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

### 5. 외부 트리거(cron-job.org) 연결

주기 실행을 담당하는 부분입니다. GitHub 자체 예약(cron)은 불규칙해 쓰지 않고,
cron-job.org 가 약 10분마다 GitHub 워크플로를 깨웁니다.

1. **GitHub Personal Access Token(fine-grained) 발급**
   - Repository access: **이 리포만** 선택
   - Permissions → **Actions: Read and write**
2. **cron-job.org 에서 cronjob 생성**
   - URL: `https://api.github.com/repos/<owner>/<repo>/actions/workflows/check.yml/dispatches`
   - Method: **POST**
   - Headers:
     - `Authorization: Bearer <위에서 만든 PAT>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
   - Body: `{"ref":"main"}`
   - Schedule: **약 10분마다**
   - job 을 **Active(활성)** 로 켜기 (계정 이메일 인증이 필요할 수 있음)
   - 정상 응답 코드는 **`204`** (204 = 요청 접수)

### 6. 가동 확인

- **Actions** 탭에 실행이 주기적으로 생기는지 확인
- 텔레그램에서 봇에게 `/상태` 를 보내고 다음 사이클(최대 10분) 뒤 응답이 오면 정상

---

## 텔레그램 명령어

| 명령 | 설명 |
|------|------|
| `/기간 20260801 20260831` | 확인할 날짜 기간 설정 |
| `/코트 1,3` | 확인할 코트 선택 (생략 시 1~4 전체) |
| `/상태` | 현재 상태 전반 보기 |
| `/on`, `/off` | 알림 켜고 끄기 |
| `/도움말` | 명령 목록 |

날짜는 `20260801`, `2026-08-01`, `8월1일`, `8/1` 아무 형식이나 됩니다.
명령과 알림은 즉시가 아니라 다음 실행 사이클(최대 10분 뒤)에 처리됩니다.

---

## 알아둘 점

- 실행 주기는 cron-job.org 설정 주기(약 10분)를 따릅니다. GitHub 자체 예약(cron)은 불규칙해 사용하지 않습니다.
- 명령 응답과 알림은 즉시가 아니라 다음 실행 사이클에 처리됩니다(최대 10분).
- 조회 엔드포인트는 기준일부터 약 6주치 날짜만 내주므로, 그보다 먼 미래 날짜는 시간이 지나며 잡힙니다.
- 대상 시설·코트는 `checker.py` 상단 상수(`CENTER`, `PART`, `RENT_TYPE`, 코트 번호)로 지정합니다. 다른 시설로 쓰려면 그 값만 바꾸세요.
