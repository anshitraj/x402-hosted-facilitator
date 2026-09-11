#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def main() -> None:
    api_url = _env("OMNICLAW_OPENFGA_SEED_API_URL", "http://openfga:8080").rstrip("/")
    store_name = _env("OMNICLAW_OPENFGA_SEED_STORE_NAME", "omniclaw-facilitator")
    model_path = Path(
        _env("OMNICLAW_OPENFGA_SEED_MODEL_PATH", "/work/infra/auth-bootstrap/openfga/model.json")
    )
    operator_subject = _env("OMNICLAW_OPENFGA_SEED_OPERATOR_SUBJECT", "ops-alpha-admin")

    _wait_for_openfga(api_url)
    store_id = _ensure_store(api_url, store_name)
    model_id = _write_model(api_url, store_id, model_path)
    _write_tuple(
        api_url,
        store_id,
        model_id,
        {
            "user": f"user:{operator_subject}",
            "relation": "security_admin",
            "object": "ops_console:default",
        },
    )
    print(json.dumps({"storeId": store_id, "authorizationModelId": model_id}, sort_keys=True))


def _wait_for_openfga(api_url: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            _request("GET", f"{api_url}/stores")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("OpenFGA API did not become ready")


def _ensure_store(api_url: str, store_name: str) -> str:
    stores = _request("GET", f"{api_url}/stores").get("stores", [])
    for store in stores:
        if isinstance(store, dict) and store.get("name") == store_name and store.get("id"):
            return str(store["id"])
    created = _request("POST", f"{api_url}/stores", {"name": store_name})
    store_id = str(created.get("id") or "")
    if not store_id:
        raise RuntimeError("OpenFGA store creation did not return an id")
    return store_id


def _write_model(api_url: str, store_id: str, model_path: Path) -> str:
    model = json.loads(model_path.read_text(encoding="utf-8"))
    response = _request("POST", f"{api_url}/stores/{store_id}/authorization-models", model)
    model_id = str(response.get("authorization_model_id") or "")
    if not model_id:
        raise RuntimeError("OpenFGA authorization model creation did not return an id")
    return model_id


def _write_tuple(api_url: str, store_id: str, model_id: str, tuple_key: dict[str, str]) -> None:
    payload = {
        "authorization_model_id": model_id,
        "writes": {"tuple_keys": [tuple_key]},
    }
    try:
        _request("POST", f"{api_url}/stores/{store_id}/write", payload)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 409 or "already" in body.lower() or "duplicate" in body.lower():
            return
        raise RuntimeError(f"OpenFGA tuple write failed: {exc.code} {body}") from exc


def _request(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        response_body = response.read()
    return json.loads(response_body.decode("utf-8") or "{}")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


if __name__ == "__main__":
    main()
