import os
import secrets

import docker
from docker.errors import NotFound
from fastapi import FastAPI, Header, HTTPException

# Only these compose services can be acted on, regardless of what a caller asks for.
ALLOWED_SERVICES = {"worker", "nginx"}

API_KEY = os.environ["TERMINATOR_API_KEY"]

app = FastAPI(title="Terminator")
client = docker.from_env()


def _require_api_key(x_terminator_key: str | None) -> None:
    if not x_terminator_key or not secrets.compare_digest(x_terminator_key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def _containers_for_service(service: str):
    containers = client.containers.list(all=True, filters={"label": f"com.docker.compose.service={service}"})
    if not containers:
        raise HTTPException(status_code=404, detail=f"no running container found for service '{service}'")
    return containers


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/services/{service}/restart")
def restart_service(service: str, x_terminator_key: str | None = Header(default=None)) -> dict:
    _require_api_key(x_terminator_key)
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=403, detail=f"service '{service}' is not in the allowlist")

    restarted = []
    for container in _containers_for_service(service):
        try:
            container.restart(timeout=10)
            restarted.append(container.name)
        except NotFound:
            continue
    return {"restarted": restarted}
