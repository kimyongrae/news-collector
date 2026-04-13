# news-collector

RSS 기반 뉴스 수집기 + Gemini AI 요약 + Google Sheets 저장 + GitHub Pages JSON 배포.
TubeAI 대시보드(`YoutubeProgram/TubeAI_app`)의 **RSS 뉴스 피드** 데이터 소스로 사용됩니다.

## 데이터 흐름

```
GitHub Actions (매일 KST 09:07)
  ↓
RSS 수집 (config/categories.yaml)
  ↓
Gemini AI 요약·감성·중요도 분석
  ↓
① Google Sheets 기록 (백업/뷰어용)
  ↓
② docs/data/ JSON 저장 (1차 저장소)
    ├── index.json                      전체 메타
    ├── 2026-04.json                    월별 원본 (구 호환)
    └── 2026/04/
        ├── index.json                  월 요약 (일별 수, 카테고리별)
        └── 2026-04-12.json             일별 상세 (TubeAI 가 읽는 파일)
  ↓
GitHub Pages 정적 배포
  ↓
TubeAI 대시보드가 fetch
```

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

⚠️ = 필수이지만 fallback 있음 · ⬜ = 선택

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
