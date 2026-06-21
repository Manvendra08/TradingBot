import httpx
from fastapi.testclient import TestClient
from fastapi import FastAPI

app = FastAPI()

_orig_client_init = httpx.Client.__init__

def _patched_client_init(self, *args, **kwargs):
    print("PATCH CALLED! kwargs:", kwargs)
    app = kwargs.pop("app", None)
    if app is not None and "transport" not in kwargs:
        try:
            kwargs["transport"] = httpx.ASGITransport(app=app)
            print("Successfully set transport to ASGITransport")
        except AttributeError as e:
            print("AttributeError raised:", e)
            kwargs["app"] = app
    _orig_client_init(self, *args, **kwargs)

httpx.Client.__init__ = _patched_client_init

try:
    client = TestClient(app)
    print("TestClient created successfully!")
except Exception as e:
    import traceback
    traceback.print_exc()
