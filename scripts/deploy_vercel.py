"""Deploy the synthetic EKT demo using Vercel's REST API.

The access token is entered interactively and never saved. A fresh cart-signing
key is generated only when the project has no such environment variable.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "ekt-ai-assistant-demo"
API = "https://api.vercel.com"
FILES = [
    ".python-version",
    "app.py",
    "main.py",
    "agent.py",
    "ui_actions.py",
    "catalog.py",
    "cart_state.py",
    "catalog_demo.json",
    "purchase_conditions.txt",
    "system_prompt.txt",
    "requirements.txt",
    "vercel.json",
    "static/index.html",
    "static/app.js",
    "static/assets/logo.jpg",
    "logic/system_prompt.txt",
    "logic/purchase_conditions.txt",
    "logic/demo_questions.json",
    "logic/tools.json",
]


def api_request(token: str, method: str, path: str, payload: object | bytes | None = None,
                headers: dict[str, str] | None = None) -> dict:
    body = None
    request_headers = {"Authorization": "Bearer " + token}
    if payload is not None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/octet-stream" if isinstance(payload, bytes) else "application/json"
    request_headers.update(headers or {})
    request = Request(API + path, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except HTTPError as exc:
        try:
            error = json.load(exc).get("error", {})
            detail = f"{error.get('code', 'unknown')}: {error.get('message', 'API error')}"
        except (ValueError, AttributeError):
            detail = "API error"
        raise RuntimeError(f"Vercel API {method} {path} failed ({exc.code}): {detail}") from None


def main() -> None:
    token = getpass.getpass("Vercel access token: ").strip()
    if not token:
        raise RuntimeError("A Vercel access token is required")

    projects = api_request(token, "GET", "/v9/projects?limit=100").get("projects", [])
    project = next((item for item in projects if item.get("name") == PROJECT_NAME), None)
    if project is None:
        project = api_request(token, "POST", "/v11/projects", {"name": PROJECT_NAME, "framework": "fastapi"})
        print("Created Vercel project", PROJECT_NAME, flush=True)
    else:
        print("Using Vercel project", PROJECT_NAME, flush=True)
    project_id = project["id"]

    env_data = api_request(token, "GET", f"/v9/projects/{project_id}/env")
    variables = env_data.get("envs", []) if isinstance(env_data, dict) else env_data
    if not any(item.get("key") == "CART_SIGNING_KEY" for item in variables):
        api_request(token, "POST", f"/v10/projects/{project_id}/env", [{
            "key": "CART_SIGNING_KEY",
            "value": secrets.token_urlsafe(48),
            "type": "sensitive",
            "target": ["production"],
        }])
        print("Created a sensitive cart-signing key in Vercel", flush=True)
    if not any(item.get("key") == "DEMO_MODE" for item in variables):
        api_request(token, "POST", f"/v10/projects/{project_id}/env", [{
            "key": "DEMO_MODE",
            "value": "1",
            "type": "plain",
            "target": ["production"],
        }])
        print("Enabled synthetic catalog for production", flush=True)

    files = []
    for relative in FILES:
        content = (ROOT / relative).read_bytes()
        sha = hashlib.sha1(content).hexdigest()
        api_request(token, "POST", "/v2/now/files", content, {"x-vercel-digest": sha})
        files.append({"file": relative, "sha": sha, "size": len(content)})
    print(f"Uploaded {len(files)} source files", flush=True)

    deployment = api_request(token, "POST", "/v13/deployments", {
        "name": PROJECT_NAME,
        "project": project_id,
        "files": files,
        "projectSettings": {"framework": "fastapi"},
        "target": "production",
    })
    deployment_id = deployment["id"]
    print("Deployment ID:", deployment_id, flush=True)
    deadline = time.monotonic() + 600
    state = deployment.get("readyState")
    while state not in ("READY", "ERROR", "CANCELED") and time.monotonic() < deadline:
        time.sleep(10)
        deployment = api_request(token, "GET", f"/v13/deployments/{deployment_id}")
        new_state = deployment.get("readyState")
        if new_state != state:
            print("Build state:", new_state, flush=True)
        state = new_state

    if state != "READY":
        raise RuntimeError(f"Deployment ended in state {state}: {deployment.get('errorCode', '')} {deployment.get('errorMessage', '')}")
    print("Deployment URL:", "https://" + deployment["url"], flush=True)
    print("Production alias:", f"https://{PROJECT_NAME}.vercel.app", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
