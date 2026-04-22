# news-collector

RSS 기반 뉴스 수집기 + Gemini AI 요약 + Google Sheets 저장 + GitHub Pages JSON 배포.
TubeAI 대시보드(`YoutubeProgram/TubeAI_app`)의 **RSS 뉴스 피드** 데이터 소스로 사용됩니다.

## 데이터 흐름 (v2 — 2026-04 개편)

```
GitHub Actions (매일 KST 09:07)
  ↓
RSS 수집 (config/categories.yaml)
  ↓
Gemini AI 요약·감성·중요도 분석
  ↓
Google Sheets 기록 (연단위 파일 · 월별 탭)
  ↓
TubeAI serve.py → Sheets API v4 로 직독
```

## v2 변경 요약

- **JSON 생성·GitHub Pages 배포 제거**
  - `docs/data/` 하위 JSON 파일을 더 이상 생성하지 않음
  - GitHub Pages 구독 불필요 (private repo 에서도 무료로 사용)
  - `save_json()` 함수 / JSON 분할 로직 완전 제거
- **GitHub Actions 단순화**
  - commit/push step 제거 → `permissions: contents: read` 로 축소
  - 수집·Sheets 기록만 수행 (더 빠르고 안전)
- **Sheets 컬럼 순서 변경 — 수집시간 첫 열**
  - 이전: `카테고리 · 제목 · 요약 · ... · 발행시간 · 수집시간`
  - 변경: `수집시간 · 발행시간 · 카테고리 · 제목 · 요약 · ...`
  - TubeAI 대시보드에서 수집시간 기반 필터링을 쉽게 하기 위함
- **TubeAI 는 serve.py 가 Sheets API v4 를 프록시**하여 대시보드에 전달
  - 브라우저가 직접 Sheets 를 호출하지 않아 CORS 이슈 없음
  - 서버 메모리 5분 캐시로 Sheets 호출 횟수 최소화
  - 읽기 무료 · 300 req/min 할당량 (월 수십 건 사용에 영향 없음)

## Sheet 공유 설정 (필수)

TubeAI 가 Sheets API 로 읽을 수 있게 하려면 연단위 파일을 **링크 있는 누구나 뷰어** 로 공유해야 합니다.

```
Google Drive 또는 해당 Sheet 열기
  → 우상단 [공유] 버튼
  → "제한됨" 을 "링크가 있는 모든 사용자" 로 변경
  → 권한: 뷰어 (쓰기 X)
  → [완료]
```

Sheet ID 는 URL 에서 추출합니다:
```
https://docs.google.com/spreadsheets/d/1WY5i45C058pcd7AD-5DKGep6-UdswPepI6_RQ1-g2TE/edit
                                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                       이 부분이 Sheet ID
```

이 ID 를 TubeAI 설정 → API 키 관리 → **RSS 뉴스 피드** 항목에 붙여넣으면 됩니다.

---

## 환경변수

모든 설정은 환경변수로 주입됩니다. 로컬은 `.env` 파일을 쓰고, GitHub Actions 는 repository secrets 를 씁니다.

| 변수 | 필수 | 설명 |
|---|---|---|
| `GOOGLE_OAUTH_TOKEN` | ✅ | OAuth refresh token 정보 (JSON 문자열 한 줄) |
| `GEMINI_API_KEY` | ⚠️ | Gemini API 키 (없으면 단순 추출로 fallback) |
| `GDRIVE_FOLDER_ID` | ⚠️ | 스프레드시트를 저장할 Drive 폴더 ID |
| `GDRIVE_SPREADSHEET_ID` | ⬜ | 특정 스프레드시트 직접 지정 (없으면 연도별 자동 생성) |
| `GOOGLE_OAUTH_CLIENT_ID` | ⬜ | `get_token.py` 에서만 사용 |
| `GOOGLE_OAUTH_CLIENT_SECRET` | ⬜ | `get_token.py` 에서만 사용 |
| `SKIP_GEMINI` | ⬜ | `1` 로 설정 시 Gemini 호출 전부 스킵 → 규칙 기반 fallback 만 사용 (쿼터 우회) |
| `AI_BATCH_SIZE` | ⬜ | 한 API 호출에 묶을 기사 수. 기본 `20` (호출 횟수 최소화) |
| `AI_CALL_DELAY` | ⬜ | API 호출 간 딜레이(초). 기본 `1.5` |

⚠️ = 필수이지만 fallback 있음 · ⬜ = 선택

### Gemini 쿼터 관리

Gemini 무료 티어는 **분당 5회 / 일일 20회** 로 타이트합니다. 소스가 많아지면 자동 처리:

- **배치 크기 20** 이 기본 — 20 기사 묶어 1 API 호출 → 일 400 기사까지 AI 로 커버 가능
- 실행 중 `429 Quota exceeded` 감지 시 **circuit breaker 작동** → 남은 모든 기사는 즉시
  규칙 기반 fallback 으로 전환 (재시도 시간 낭비 안 함)
- Gemini 를 아예 쓰고 싶지 않으면 `SKIP_GEMINI=1` 설정 → 처음부터 전체 fallback

### 로컬 개발 — `.env`

```bash
cp .env.example .env
# .env 파일을 편집하고 값을 채움
pip install -r requirements.txt
python src/collector.py config/categories.yaml
```

`python-dotenv` 가 설치돼 있으면 `collector.py` 가 실행 시 자동으로 `.env` 를 로드합니다.

### GitHub Actions — Secrets

GitHub 저장소에서:
```
Settings → Secrets and variables → Actions → New repository secret
```

등록할 시크릿:
- `GOOGLE_OAUTH_TOKEN` — `python get_token.py` 실행 후 출력된 JSON 전체
- `GEMINI_API_KEY` — https://aistudio.google.com/apikey
- `GDRIVE_FOLDER_ID` — Google Drive 폴더 URL 에서 추출 (선택)

---

## OAuth 토큰 발급

```bash
# 1. Google Cloud Console → APIs & Services → Credentials
#    → Create OAuth client ID → Application type: Desktop app
# 2. .env 에 client id/secret 추가:
#      GOOGLE_OAUTH_CLIENT_ID=...
#      GOOGLE_OAUTH_CLIENT_SECRET=...
# 3. 실행:
pip install google-auth-oauthlib python-dotenv
python get_token.py
# 4. 출력된 GOOGLE_OAUTH_TOKEN 을 .env 또는 GitHub Secrets 에 복사
```

필요한 OAuth 스코프:
- `https://www.googleapis.com/auth/spreadsheets`
- `https://www.googleapis.com/auth/drive`

---

## GitHub Actions 스케줄

`.github/workflows/collect.yml`:

```yaml
schedule:
  - cron: "7 0 * * *"   # UTC 00:07 = KST 09:07
```

### 왜 정각 00:00 이 아니라 00:07 인가

> **GitHub Actions 공식 문서** — The `schedule` event can be delayed during periods of high loads. **High load times include the start of every hour.** To decrease the chance of delay, schedule your workflow to run at a different time of the hour.

이전 설정 `"0 0 * * *"` (정시 정각) 는 전 세계 workflow 가 동시에 몰리는 시각이라 schedule 이 **누락·지연**되는 경우가 많습니다. 7분 뒤로 옮기면 부하가 완화돼 훨씬 안정적으로 실행됩니다.

### Actions 가 안 돌 때 체크리스트

1. **저장소 activity**: 60일 이상 커밋이 없으면 GitHub 이 자동으로 schedule 을 비활성화합니다. 수동으로 한 번 실행하면 다시 활성화됩니다.
2. **기본 브랜치**: `on.schedule` 은 default branch 에 있는 워크플로만 실행됩니다. `main` 이 아닌 브랜치에 있다면 옮기세요.
3. **시크릿 만료**: `refresh_token` 이 revoke 되거나 6개월 미사용 시 무효화됩니다. Actions 로그에 `invalid_grant` 가 보이면 재발급이 필요합니다.
4. **수동 테스트**: Actions 탭 → 뉴스 자동 수집 → Run workflow 로 즉시 실행 가능합니다.
5. **로그 확인**: 실행이 일어났다면 Actions 탭에서 상세 로그를 볼 수 있습니다. 실패 시 "🚨 실패 시 요약" step 에 체크리스트가 출력됩니다.

---

## 🚨 보안 경고 — 과거 버전의 유출된 자격증명

이전 버전에서는 `oauth_token.json` 과 `get_token.py` 에 OAuth client id/secret/refresh_token 이 평문으로 커밋돼 있었습니다.
이 값들은 **git 히스토리에 이미 영구 기록**되어 있어, 저장소가 public 이었거나 과거 한 번이라도 노출됐다면 **유출된 것으로 간주**해야 합니다.

### 조치 순서

1. **Google Cloud Console → APIs & Services → Credentials**
   → 기존 OAuth 클라이언트를 **삭제하거나 회전(rotate)** — 새 client id/secret 발급
2. 기존 refresh token 은 자동으로 무효화됨
3. 새 client 로 `python get_token.py` 재실행 → 새 `GOOGLE_OAUTH_TOKEN` 발급
4. GitHub Secrets 에 새 토큰 등록
5. (선택) git 히스토리에서 파일 제거:
   ```bash
   git rm --cached oauth_token.json   # 파일은 이미 로컬에서 삭제됨
   git commit -m "chore: stop tracking oauth_token.json"

   # 완전 제거하려면 git-filter-repo 또는 BFG 사용 (선택)
   # https://github.com/newren/git-filter-repo
   ```

현재 레포의 `.gitignore` 가 `oauth_token.json` 과 `.env` 를 제외하므로 향후 재발 위험은 없습니다.

---

## 폴더 구조

```
news-collector/
├── .env.example              ← 환경변수 템플릿 (복사해서 .env 만들기)
├── .gitignore                ← secrets 제외
├── .github/workflows/
│   └── collect.yml           ← GitHub Actions 스케줄
├── config/
│   └── categories.yaml       ← RSS 소스 · 카테고리 · 키워드 필터
├── src/
│   └── collector.py          ← 수집기 본체
├── docs/                     ← GitHub Pages 루트
│   ├── index.html
│   └── data/
│       ├── index.json
│       ├── 2026-04.json
│       └── 2026/04/*.json
├── get_token.py              ← OAuth 토큰 1회 발급 스크립트
├── requirements.txt
└── README.md
```
