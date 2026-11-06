"""HTTP API + live event stream + static UI."""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs, sandbox

app = FastAPI(title="Sealvix")
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")
RATE = int(os.environ.get("JOBS_PER_HOUR_PER_IP", "6"))
_hits: dict[str, deque] = defaultdict(deque)


class JobIn(BaseModel):
    source: str              # https://github.com/owner/repo  or  demo:<name>
    branch: str | None = None


def _client_ip(req: Request) -> str:
    return req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "").split(",")[0].strip() \
        or (req.client.host if req.client else "?")


@app.get("/api/health")
def health():
    return {"ok": True, "runtime": sandbox.RUNTIME, "docker": sandbox.docker_available()}


@app.get("/api/config")
def config():
    return {"demos": jobs.demo_names(), "containment": sandbox.containment_profile()}


@app.post("/api/jobs")
def create(body: JobIn, req: Request):
    ip = _client_ip(req)
    q, now = _hits[ip], time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE:
        raise HTTPException(429, "rate limit: try again later")
    q.append(now)
    src = body.source.strip()
    if not (src.startswith("demo:") or jobs.GITHUB_RE.match(src)):
        raise HTTPException(400, "use a public https://github.com/<owner>/<repo> URL or a demo")
    job = jobs.create_job(src, body.branch)
    return {"id": job.id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.JOBS.get(job_id)
    if job:
        return job.public()
    path = os.path.join(jobs.DATA_DIR, "receipts", os.path.basename(job_id) + ".json")
    if os.path.exists(path):
        return json.load(open(path))
    raise HTTPException(404, "no such job")


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str, request: Request):
    job = jobs.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")

    async def gen():
        i = 0
        while True:
            if await request.is_disconnected():
                return
            while i < len(job.events):
                ev = job.events[i]
                i += 1
                yield f"data: {json.dumps(ev)}\n\n"
                if ev["type"] == "done":
                    return
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
