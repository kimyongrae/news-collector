"""
OAuth 토큰 발급 스크립트 - 로컬에서 1회만 실행
발급된 refresh_token을 GitHub Secret에 저장하면 끝!

실행 방법:
  pip install google-auth-oauthlib
  python get_token.py
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

# ── 여기에 입력 ──────────────────────────────────────────────
CLIENT_ID     = "87249978372-aufoddb78fahqnubtv0k46uk0asnkoha.apps.googleusercontent.com"
CLIENT_SECRET = "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"
# ────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

client_config = {
    "installed": {
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=9090, open_browser=True)

result = {
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "refresh_token": creds.refresh_token,
    "token_uri":     "https://oauth2.googleapis.com/token",
}

print("\n" + "="*60)
print("✅ 아래 내용을 GitHub Secret 'GOOGLE_OAUTH_TOKEN' 에 저장하세요")
print("="*60)
print(json.dumps(result, indent=2))
print("="*60)

with open("oauth_token.json", "w") as f:
    json.dump(result, f, indent=2)
print("\n📁 oauth_token.json 파일로도 저장됐습니다.")
