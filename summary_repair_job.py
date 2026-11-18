"""In-process one-time recovery job coordinator.

Long model calls must not hold a browser/Cloudflare request open. This coordinator
does not run on startup and never retries on its own. Production should use a
single web worker for this in-process registry.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

_jobs = {}
_tasks = {}
_session_active = {}
_lock = asyncio.Lock()


def _public(job):
    return {key: job.get(key) for key in (
        "job_id", "session_id", "status", "stage", "created_at", "finished_at",
        "error", "snapshot_id", "generated_blocks", "summary_part_count", "a_start_round",
    )}


async def _run(job, parameters, generator):
    from summary_repair import execute
    job["status"] = "running"
    job["stage"] = "generating_four_blocks"
    try:
        result = await execute(
            parameters["session_id"], parameters["archive_id"],
            parameters["start_round"], parameters["preserved_parts"],
            parameters["x"], parameters["preview_token"],
            parameters["confirmation"], generator,
        )
        job.update(
            status="succeeded", stage="finished",
            snapshot_id=result["snapshot_id"],
            generated_blocks=result["generated_blocks"],
            summary_part_count=len(result["summary_parts"]),
            a_start_round=result["a_start_round"],
        )
    except Exception as exc:
        logging.exception("[summary-repair-job] failed job_id=%s", job["job_id"])
        detail = getattr(exc, "detail", None)
        job.update(
            status="failed", stage="finished",
            error=str(detail or type(exc).__name__)[:180],
        )
    finally:
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        async with _lock:
            if _session_active.get(job["session_id"]) == job["job_id"]:
                _session_active.pop(job["session_id"], None)
            _tasks.pop(job["job_id"], None)


async def start(parameters, generator):
    """Idempotent by active session; a duplicate click reuses the same job."""
    sid = parameters["session_id"]
    async with _lock:
        active_id = _session_active.get(sid)
        if active_id:
            return _public(_jobs[active_id])
        jid = uuid.uuid4().hex
        job = dict(
            job_id=jid, session_id=sid, status="queued", stage="queued",
            created_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None, error=None, snapshot_id=None,
            generated_blocks=0, summary_part_count=None,
            a_start_round=None,
        )
        _jobs[jid] = job
        _session_active[sid] = jid
        task = asyncio.create_task(_run(job, parameters, generator))
        _tasks[jid] = task
        # Only keep the last 20 completed job records, never remove running tasks.
        completed = [key for key, val in _jobs.items() if val["status"] in ("succeeded", "failed")]
        for key in completed[:-20]:
            _jobs.pop(key, None)
        return _public(job)


def status(session_id, job_id):
    job = _jobs.get(job_id)
    if not job or job["session_id"] != session_id:
        return None
    return _public(job)


def is_active(session_id: str) -> bool:
    """True only while this process is generating or committing a recovery."""
    job_id = _session_active.get(session_id)
    job = _jobs.get(job_id) if job_id else None
    return bool(job and job.get("status") in ("queued", "running"))
