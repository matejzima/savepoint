from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.forward_auth import ForwardAuthMiddleware


def _make_client(header_name="X-Authentik-Username"):
    app = FastAPI()
    app.add_middleware(ForwardAuthMiddleware, header_name=header_name)

    @app.get("/")
    def index():
        return {"ok": True}

    return TestClient(app)


def test_request_with_header_present_succeeds():
    client = _make_client()
    r = client.get("/", headers={"X-Authentik-Username": "matej"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_request_missing_header_rejected():
    client = _make_client()
    r = client.get("/")
    assert r.status_code == 401


def test_request_with_empty_header_value_rejected():
    client = _make_client()
    r = client.get("/", headers={"X-Authentik-Username": ""})
    assert r.status_code == 401


def test_header_name_is_configurable():
    client = _make_client(header_name="Remote-User")
    assert client.get("/", headers={"Remote-User": "matej"}).status_code == 200
    assert client.get("/", headers={"X-Authentik-Username": "matej"}).status_code == 401
