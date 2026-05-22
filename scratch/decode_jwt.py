import base64
import json
from datetime import datetime, timezone

token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzc5Mjk1Njk3LCJpYXQiOjE3NzkyMDkyOTcsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA0MDI0Mjc5In0.8WpPljLQElVeXfWur-mMhaXK6yDltBnZ_bG963uECFjnPk0sfNGRhk3IPiEN3FOw1ul8knoaDqu0iYVaER3P-A"
payload_part = token.split(".")[1]
# Standard padding fix
payload_part += "=" * ((4 - len(payload_part) % 4) % 4)
decoded = base64.b64decode(payload_part).decode("utf-8")
data = json.loads(decoded)
print("Payload:", data)

exp = data.get("exp")
if exp:
    exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
    print("Expiry Date (UTC):", exp_dt.isoformat())
    print("Current Date (UTC):", datetime.now(timezone.utc).isoformat())
    if exp_dt < datetime.now(timezone.utc):
        print("🔴 Token is EXPIRED!")
    else:
        print("🟢 Token is VALID!")
