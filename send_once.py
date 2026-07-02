import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from market_alarm.env import load_dotenv
from market_alarm.app import MarketAlarmApp
from market_alarm.storage import Store


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent
    load_dotenv(str(project_root / ".env"))
    app = MarketAlarmApp(
        store=Store(str(project_root / "data" / "market_alarm.sqlite3")),
        project_root=project_root,
    )
    result = app.send_now(force=True)
    if not result.get("ok"):
        raise SystemExit(result.get("result", {}).get("detail") or result.get("error") or "send failed")
    print(result.get("result", {}).get("detail") or "sent")
