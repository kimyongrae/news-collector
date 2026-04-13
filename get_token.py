#!/usr/bin/env python3
"""
OAuth refresh token 1회 발급 스크립트.

사용법
------
1. Google Cloud Console 에서 OAuth 클라이언트 (Desktop 유형) 생성
2. .env 에 client id/secret 기입 (또는 환경변수로 export):
     GOOGLE_OAUTH_CLIENT_ID=...
     GOOGLE_OAUTH_CLIENT_SECRET=...
3. 의존성 설치:
     pip install google-auth-oauthlib python-dotenv
4. 실행:
     python get_token.py
5. 발급된 JSON 전체를 한 줄로 복사
6. .env 의 GOOGLE_OAUTH_TOKEN 에 붙여넣기
   또는 GitHub Secrets 의 GOOGLE_OAUTH_TOKEN 에 등록

주의
----
· 과거 버전의 이 스크립트는 client id/secret 을 코드에 하드코딩했고
  결과로 생성된 oauth_token.json 도 레포에 커밋돼 있었습니다.
  그 값들은 이미 유출된 것으로 간주하고 Google Cloud Console 에서
  OAuth 클라이언트를 재발급하세요.
"""

import json
import os
import sys

# .env 로드 (선택)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("[오류] google-auth-oauthlib 가 설치돼 있지 않습니다.", file=sys.stderr)
    print("       pip install google-auth-oauthlib", file=sys.stderr)
    sys.exit(1)


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def main():
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print(
            "[오류] 환경변수가 설정되지 않았습니다.\n"
            "       .env 파일에 다음을 추가하세요:\n"
            "         GOOGLE_OAUTH_CLIENT_ID=...\n"
            "         GOOGLE_OAUTH_CLIENT_SECRET=...\n"
            "       Google Cloud Console → APIs & Services → Credentials\n"
            "       → Create OAuth client ID (Desktop app)",
            file=sys.stderr,
        )
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id":     client_id,
            "client_secret": client_secret,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=9090, open_browser=True)

    result = {
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": creds.refresh_token,
        "token_uri":     "https://oauth2.googleapis.com/token",
    }

    print("\n" + "=" * 64)
    print(" ✅ 발급 완료. 아래 JSON 을 복사해서 저장하세요.")
    print("=" * 64)
    print("\n① 로컬 .env 파일에 넣을 때 (한 줄):")
    print()
    print(f"GOOGLE_OAUTH_TOKEN={json.dumps(result, ensure_ascii=False)}")
    print()
    print("② GitHub Secrets 에 넣을 때 (pretty JSON 도 허용):")
    print()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print()
    print("=" * 64)
    print(" 주의: 이 출력물은 민감정보입니다. 터미널 스크롤백을 지우거나")
    print("       공용 환경이면 외부 노출을 조심하세요.")
    print("=" * 64)


if __name__ == "__main__":
    main()
