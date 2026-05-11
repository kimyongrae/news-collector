# news-collector 사용 가이드

RSS 기반 뉴스 수집기. Gemini AI 로 요약·감성·중요도 분석을 거쳐 **Google Sheets** 와
**Supabase** 두 곳에 저장합니다. GitHub Actions 가 매일 KST 09:07 에 자동 실행합니다.

이 문서는 **처음 셋업 → 로컬 실행 → 자동화 → 트러블슈팅** 흐름을 한 번에 정리합니다.

---

## 1. 한눈에 보는 데이터 흐름

```
GitHub Actions (매일 KST 09:07)
        │
        ▼
   RSS 수집 (config/categories.yaml)
        │
        ▼
   본문 크롤링 (crawl_body: true 인 소스)
        │
        ▼
   Gemini AI 분석 — 요약 · 감성 · 중요도 (배치 20개 단위)
        │
        ├─► Google Sheets (연단위 파일 · 월별 탭)         ← TubeAI 대시보드가 읽음
        └─► Supabase (365일 retention · 빠른 쿼리)        ← 분석·통계용
```

데이터 소비는 `YoutubeProgram/TubeAI_app` 의 **RSS 뉴스 피드** 가 담당합니다.

---

## 2. 사전 준비물

| 항목 | 발급처 | 용도 |
|---|---|---|
| Google OAuth Client | Google Cloud Console → APIs & Services → Credentials | Sheets / Drive 접근 |
| Gemini API Key | https://aistudio.google.com/apikey | AI 요약·분석 |
| Google Drive 폴더 | drive.google.com 에서 폴더 생성 후 URL 의 `folders/{ID}` | 결과 시트 저장 위치 |
| Supabase 프로젝트 | https://supabase.com (선택) | 365일 retention 저장소 |
| Python 3.11+ | https://www.python.org/ | 로컬 실행 |

> Gemini / Supabase 는 **선택**입니다. 키가 없으면 각각 규칙 기반 fallback / Sheets 단독 저장으로 동작합니다.

---

## 3. 최초 셋업 (로컬)

### 3-1. 저장소 클론 & 의존성 설치

```bash
git clone <this-repo>
cd news-collector
python -m venv .venv && source .venv/bin/activate     # 권장
pip install -r requirements.txt
```

### 3-2. `.env` 파일 만들기

```bash
cp .env.example .env
# 에디터로 .env 열어서 값 채우기
```

`.env` 는 `.gitignore` 로 제외돼 있습니다. **절대 커밋하지 마세요.**

### 3-3. Google OAuth 토큰 발급 (1회)

`.env` 에 먼저 client id/secret 만 넣고:

```bash
# .env 에 추가
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
```

토큰 발급 스크립트 실행:

```bash
pip install google-auth-oauthlib python-dotenv
python get_token.py
```

브라우저가 열리고 동의하면 콘솔에 `GOOGLE_OAUTH_TOKEN=...` 형태의 한 줄 JSON 이 출력됩니다.
**그 한 줄을 `.env` 에 복사** 하면 끝.

필요 OAuth 스코프:
- `https://www.googleapis.com/auth/spreadsheets`
- `https://www.googleapis.com/auth/drive`

### 3-4. 채워야 할 환경변수 요약

| 변수 | 필수 | 설명 |
|---|---|---|
| `GOOGLE_OAUTH_TOKEN` | ✅ | `get_token.py` 출력값 (JSON 한 줄) |
| `GDRIVE_FOLDER_ID` | ⚠️ | 결과 시트가 저장될 Drive 폴더 ID |
| `GEMINI_API_KEY` | ⚠️ | 없으면 fallback (단순 추출) |
| `GDRIVE_SPREADSHEET_ID` | ⬜ | 특정 시트에 강제 저장하고 싶을 때 |
| `SUPABASE_URL` | ⬜ | Supabase 동시 저장 (선택) |
| `SUPABASE_SERVICE_KEY` | ⬜ | service_role 키 — 절대 커밋 금지 |
| `SUPABASE_RETENTION_DAYS` | ⬜ | 기본 90, GitHub Actions 는 365 사용 |
| `SKIP_GEMINI` | ⬜ | `1` 설정 시 AI 호출 전부 스킵 (쿼터 우회) |
| `AI_BATCH_SIZE` | ⬜ | 한 호출에 묶을 기사 수 (기본 20) |
| `AI_CALL_DELAY` | ⬜ | 호출 간 딜레이 초 (기본 1.5) |

⚠️ = 필수지만 fallback 있음 · ⬜ = 선택

---

## 4. 실행 방법

### 4-1. 로컬 수동 실행

```bash
python src/collector.py config/categories.yaml
```

처음 실행하면:
1. 지정한 Drive 폴더에 `news-{년도}.xlsx` 형태로 시트가 자동 생성됨
2. 월 이름(`2026-05`) 탭이 생성되고 헤더 행이 작성됨
3. 각 카테고리/소스를 순회하며 RSS 파싱 → 본문 크롤 → AI 분석 → 행 추가
4. (Supabase 키가 있으면) 동시에 Supabase 에도 upsert

### 4-2. Supabase 백필 (선택, 1회)

기존 시트에 쌓여 있던 과거 기사를 Supabase 로 한 번에 옮길 때:

```bash
python scripts/backfill_to_supabase.py 90        # 최근 90일
python scripts/backfill_to_supabase.py           # 기본 90일
```

`url_hash` unique 제약으로 중복은 자동 스킵됩니다.

### 4-3. GitHub Actions 로 자동화

GitHub 저장소에서 **Settings → Secrets and variables → Actions** 에 `.env` 와 동일한
키들을 등록합니다. 등록할 secrets:

- `GOOGLE_OAUTH_TOKEN` (필수)
- `GEMINI_API_KEY` (권장)
- `GDRIVE_FOLDER_ID` (선택, 없으면 `categories.yaml` 의 `folder_id` 사용)
- `GDRIVE_SPREADSHEET_ID` (선택)
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (Supabase 사용 시)
- `SUPABASE_RETENTION_DAYS` (선택, 미설정 시 워크플로가 365 주입)

이후 매일 KST 09:07 에 `.github/workflows/collect.yml` 이 자동 실행됩니다.

**수동 실행**: 저장소 Actions 탭 → `뉴스 자동 수집` → Run workflow.

---

## 5. 수집 대상 편집 — `config/categories.yaml`

```yaml
folder_id: "..."            # 환경변수 없을 때 fallback
spreadsheet_id: ""
limit_per_source: 6         # 소스당 기본 수집 건수

categories:
  - name: 경제
    sources:
      - type: rss
        url: https://www.hankyung.com/feed/economy
        label: 한국경제_경제
        crawl_body: true    # 본문까지 크롤
```

추가/삭제 시:
- `label` 은 시트의 `소스` 컬럼에 그대로 기록됩니다 (중복 안 되게)
- `crawl_body: false` 면 RSS 의 `description` 만 사용 (빠르지만 본문 부실)
- 카테고리 단위로 `limit` 을 다르게 지정 가능

변경 후 로컬에서 `python src/collector.py config/categories.yaml` 로 한 번 돌려보고 커밋하는 것을 권장.

---

## 6. 출력 데이터

### 6-1. Google Sheets

- 파일명: `news-{년도}.xlsx` (예: `news-2026.xlsx`)
- 탭: 월 단위 — `2026-05`, `2026-06`, ...
- 컬럼 순서 (v2 기준):
  ```
  수집시간 · 발행시간 · 카테고리 · 제목 · 요약 · 감성 · 중요도 · ...
  ```
- **TubeAI 가 읽을 수 있게 공유 필수**: 시트 → 공유 → 링크가 있는 모든 사용자 (뷰어).

### 6-2. Supabase

- 테이블: `news_articles` (Supabase 콘솔에서 사전에 만들어 둬야 함)
- `url_hash` 가 unique key → 중복 자동 스킵
- 매 실행마다 `SUPABASE_RETENTION_DAYS` 이전 데이터 자동 cleanup
- 기본값 365일 (GitHub Actions) / 90일 (로컬 `.env` 기본)

---

## 7. Gemini 쿼터 관리

무료 티어는 **분당 5회 / 일일 20회** 로 타이트합니다. 다음 자동 보호장치가 있습니다:

| 동작 | 설명 |
|---|---|
| 배치 크기 20 | 한 번의 API 호출에 20개 기사 묶어 처리. 일 400 기사까지 AI 커버. |
| Circuit breaker | `429 Quota exceeded` 감지 시 남은 기사는 즉시 fallback 으로 전환. |
| `SKIP_GEMINI=1` | 처음부터 AI 호출 스킵, 전부 규칙 기반 fallback. |

운영 팁: 일일 한도가 자주 터지면 `AI_BATCH_SIZE=30` 으로 늘려 호출 횟수를 더 줄이세요.

---

## 8. 트러블슈팅

### Actions 가 자동 실행되지 않을 때

1. **60일 무커밋 → schedule 비활성화**
   - 워크플로의 keepalive step 이 `.github/LAST_RUN.txt` 를 매일 커밋해 방지하지만,
     비활성화됐다면 Actions 탭에서 한 번 수동 실행으로 재활성화.
2. **기본 브랜치 확인** — `on.schedule` 은 default branch (`main`) 의 워크플로만 실행.
3. **`invalid_grant` 에러** — `GOOGLE_OAUTH_TOKEN` 의 refresh_token 이 6개월 미사용 또는 revoke 됨. `get_token.py` 재실행 → secrets 갱신.
4. **시크릿 누락** — Actions 로그 첫 step "🔐 시크릿 존재 여부 점검" 에서 확인.

### `429 Quota exceeded` 가 자주 보일 때

- `AI_BATCH_SIZE` 를 20 → 30 으로 올려 호출 횟수 감소
- `AI_CALL_DELAY` 를 1.5 → 3 으로 올려 분당 호출 분산
- 임시: `SKIP_GEMINI=1` 로 우회하고 fallback 으로 동작

### Sheets 에 행이 안 쌓일 때

- `GDRIVE_FOLDER_ID` 가 본인 계정에서 접근 가능한 폴더인지
- OAuth 스코프에 `spreadsheets` 와 `drive` 둘 다 포함됐는지 (`get_token.py` 재실행)
- 로그에 "이미 수집됨 (skip)" 만 잔뜩 보이면 정상 — 어제 수집한 URL 은 중복 차단됨

### Supabase 에 안 쌓일 때

- `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` 둘 다 들어가 있어야 활성화
- service_role 키여야 함 (anon 키는 RLS 때문에 쓰기 거부됨)
- 테이블 `news_articles` 가 사전에 생성돼 있어야 함

---

## 9. 폴더 구조

```
news-collector/
├── .env.example              ← 환경변수 템플릿
├── .gitignore
├── .github/
│   ├── workflows/collect.yml ← GitHub Actions 스케줄
│   └── LAST_RUN.txt          ← keepalive 타임스탬프 (자동 커밋)
├── config/
│   └── categories.yaml       ← RSS 소스 · 카테고리 정의
├── src/
│   ├── collector.py          ← 수집기 본체
│   └── supabase_client.py    ← Supabase 저장·cleanup 로직
├── scripts/
│   └── backfill_to_supabase.py  ← 시트 → Supabase 1회 백필
├── docs/                     ← (구) GitHub Pages 자산 — v2 부터 미사용
├── doc/
│   └── USAGE.md              ← 이 문서
├── get_token.py              ← OAuth 토큰 1회 발급
├── requirements.txt
└── README.md                 ← 프로젝트 개요
```

---

## 10. 일상 운영 체크리스트

| 빈도 | 할 일 |
|---|---|
| 매일 | (선택) Actions 탭에서 마지막 실행 상태 초록인지 확인 |
| 주 1회 | Sheets 에서 당일 행이 정상적으로 추가됐는지 |
| 월 1회 | 새 월 탭이 자동 생성됐는지, 헤더 정상인지 |
| 분기 1회 | Drive 폴더에 새 연단위 파일이 필요한지 (연 바뀌면 자동 생성) |
| 6개월 1회 | OAuth refresh_token 만료 임박 — 한 번 더 실행돼 active 유지되는지 |

---

## 11. 참고

- 프로젝트 개요 / 변경 이력: [`README.md`](../README.md)
- 워크플로 정의: [`.github/workflows/collect.yml`](../.github/workflows/collect.yml)
- 수집 대상: [`config/categories.yaml`](../config/categories.yaml)
- 데이터 소비처: `YoutubeProgram/TubeAI_app` 의 RSS 뉴스 피드 모듈
