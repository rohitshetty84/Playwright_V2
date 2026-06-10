import asyncio
import json
import uuid
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.batch_manager import (
    batch_to_workbook,
    create_batch_from_excel,
    load_batch,
    load_batches,
    parse_workbook_bytes,
    save_batch,
)
from services.github import dispatch_github_workflow, find_latest_run_for_golden_id

router = APIRouter()

# Tracks the asyncio Task running synthesis for each row so it can be cancelled mid-flight.
_ROW_TASKS: Dict[str, "asyncio.Task"] = {}    # key = f"{batch_id}:{row_id}"
# Rows that have been cancel-requested but not yet reached by the loop.
_CANCEL_ROWS: Dict[str, set] = {}             # key = batch_id → set of row_ids
# Top-level background task for each batch (synthesize + dispatch).
_BATCH_TASKS: Dict[str, "asyncio.Task"] = {}  # key = batch_id


class BatchTriggerRequest(BaseModel):
    workflow_label: Optional[str] = None
    workers: int = 2


async def _synthesize_batch_rows(batch: dict, workers: int = 2) -> tuple[List[str], List[Dict]]:
    """Worker-pool synthesis: N rows run concurrently via asyncio.Queue."""
    from server import SynthesizeRequest, synthesize_with_validation, save_json, GOLDEN_DIR, ts_now

    batch_id = batch["id"]
    created_ids: list[str] = []
    errors: list[dict] = []
    lock = asyncio.Lock()

    # Pre-screen: mark pre-cancelled rows, enqueue the rest
    queue: asyncio.Queue = asyncio.Queue()
    for row in batch.get("rows", []):
        row_id = row.get("rowId", "")
        if row.get("golden_id") or row.get("status") not in ("pending", "failed"):
            continue
        if row_id in _CANCEL_ROWS.get(batch_id, set()):
            row["status"] = "cancelled"
            row["workflowStatus"] = "cancelled"
            row["result"] = {"message": "Cancelled by user", "conclusion": "cancelled"}
            async with lock:
                save_batch(batch)
            continue
        await queue.put(row)

    async def _worker(worker_idx: int):
        while True:
            try:
                row = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            row_id = row.get("rowId", "")

            # Stop if the whole batch was cancelled
            try:
                current = load_batch(batch_id)
                if current.get("status") == "cancelled":
                    break
            except Exception:
                break

            # Re-check per-row cancel (may have arrived after queue was built)
            if row_id in _CANCEL_ROWS.get(batch_id, set()):
                row["status"] = "cancelled"
                row["workflowStatus"] = "cancelled"
                row["result"] = {"message": "Cancelled by user", "conclusion": "cancelled"}
                async with lock:
                    save_batch(batch)
                continue

            row["status"] = "running"
            row["workflowStatus"] = "pending"
            row["result"] = {"message": f"Worker {worker_idx}: generating golden…", "conclusion": "pending"}
            async with lock:
                save_batch(batch)

            row_key = f"{batch_id}:{row_id}"
            synthesis = None
            try:
                synth_req = SynthesizeRequest(test_case=row["test_case"])
                task = asyncio.ensure_future(
                    asyncio.wait_for(synthesize_with_validation(synth_req), timeout=600)
                )
                _ROW_TASKS[row_key] = task
                try:
                    synthesis = await task
                except asyncio.CancelledError:
                    row["status"] = "cancelled"
                    row["workflowStatus"] = "cancelled"
                    row["result"] = {"message": "Cancelled by user", "conclusion": "cancelled"}
                    async with lock:
                        errors.append({"rowId": row_id, "error": "Cancelled by user"})
                        save_batch(batch)
                    continue
                finally:
                    _ROW_TASKS.pop(row_key, None)
            except asyncio.TimeoutError:
                msg = "Synthesis timed out after 10 minutes"
                row["status"] = "failed"
                row["workflowStatus"] = "failed"
                row["result"] = {"message": msg, "conclusion": "failed"}
                async with lock:
                    errors.append({"rowId": row_id, "error": msg})
                    save_batch(batch)
                continue
            except HTTPException as exc:
                row["status"] = "failed"
                row["workflowStatus"] = "failed"
                row["result"] = {"message": exc.detail or "Synthesis failed", "conclusion": "failed"}
                async with lock:
                    errors.append({"rowId": row_id, "error": exc.detail})
                    save_batch(batch)
                continue
            except Exception as exc:
                row["status"] = "failed"
                row["workflowStatus"] = "failed"
                row["result"] = {"message": str(exc), "conclusion": "failed"}
                async with lock:
                    errors.append({"rowId": row_id, "error": str(exc)})
                    save_batch(batch)
                continue

            if not synthesis:
                continue  # was cancelled mid-task

            if synthesis.get("error"):
                row["status"] = "failed"
                row["workflowStatus"] = "failed"
                row["result"] = {"message": synthesis["error"], "conclusion": "failed"}
                async with lock:
                    errors.append({"rowId": row_id, "error": synthesis["error"]})
                    save_batch(batch)
                continue

            code = synthesis.get("tunedCode") or synthesis.get("generatedCode") or ""
            golden_id = str(uuid.uuid4())[:8]
            golden = {
                "id": golden_id,
                "name": row.get("golden_name", f"Golden {golden_id}"),
                "description": row.get("test_case", ""),
                "code": code,
                "browsers": [row.get("browser", "msedge")],
                "analysis": {
                    "phase1Pass": synthesis.get("phase1Pass"),
                    "phase1Message": synthesis.get("phase1Message"),
                    "phase2Updated": synthesis.get("phase2Updated"),
                    "phase2Changes": synthesis.get("phase2Changes", []),
                    "phase3Pass": synthesis.get("phase3Pass"),
                    "phase3Message": synthesis.get("phase3Message"),
                    "recommendation": synthesis.get("recommendation"),
                },
                "createdAt": ts_now(),
                "healCount": 0,
                "lastHealed": None,
                "status": "active",
                "steps": 5,
            }
            save_json(GOLDEN_DIR, golden_id, golden)

            row["golden_id"] = golden_id
            row["status"] = "ready"
            row["workflowStatus"] = "pending"
            row["result"] = {
                "message": f"Worker {worker_idx}: golden generated",
                "conclusion": "generated",
                "created_at": ts_now(),
                "analysis": golden["analysis"],
            }
            async with lock:
                created_ids.append(golden_id)
                save_batch(batch)

    # Launch N workers (capped at 4)
    n = max(1, min(int(workers), 4))
    await asyncio.gather(*[asyncio.ensure_future(_worker(i + 1)) for i in range(n)])

    return created_ids, errors


@router.post("/upload")
async def upload_batch(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing file name")
    contents = await file.read()
    try:
        rows = parse_workbook_bytes(contents)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    batch = create_batch_from_excel(file.filename, rows)
    return {
        "batchId": batch["id"],
        "rowCount": len(batch["rows"]),
        "status": batch["status"],
        "createdAt": batch["createdAt"],
    }


@router.get("/")
async def list_batches():
    return load_batches()


@router.get("/{batch_id}")
async def get_batch(batch_id: str):
    try:
        return load_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Batch not found")


@router.get("/{batch_id}/download")
async def download_batch(batch_id: str):
    try:
        batch = load_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Batch not found")

    workbook_bytes = batch_to_workbook(batch)
    filename = f"batch-{batch_id}.xlsx"
    return StreamingResponse(
        BytesIO(workbook_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"",
        },
    )


async def _synthesize_batch_bg(batch_id: str, workers: int = 2):
    """Background task: synthesize batch rows."""
    _BATCH_TASKS[batch_id] = asyncio.current_task()
    try:
        batch = load_batch(batch_id)
    except FileNotFoundError:
        _BATCH_TASKS.pop(batch_id, None)
        return
    try:
        created_ids, errors = await _synthesize_batch_rows(batch, workers=workers)
        batch = load_batch(batch_id)  # Reload — may have been cancelled
        if batch.get("status") != "cancelled":
            batch["status"] = "ready" if created_ids else "failed"
            if errors and not created_ids:
                batch["status"] = "failed"
            save_batch(batch)
    except asyncio.CancelledError:
        pass  # Batch cancel set status already; just exit cleanly
    except Exception as e:
        batch["status"] = "failed"
        batch["error"] = str(e)
        save_batch(batch)
    finally:
        _BATCH_TASKS.pop(batch_id, None)

@router.post("/{batch_id}/synthesize")
async def synthesize_batch(batch_id: str, body: Optional[BatchTriggerRequest] = None):
    try:
        batch = load_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Batch not found")

    if batch.get("status") == "running":
        raise HTTPException(status_code=409, detail="Batch is already running")

    workers = max(1, min(int(body.workers if body else 2), 4))
    batch["status"] = "running"
    batch["workers"] = workers
    save_batch(batch)

    asyncio.create_task(_synthesize_batch_bg(batch_id, workers=workers))

    return {
        "batchId": batch_id,
        "status": "running",
        "workers": workers,
        "message": f"Synthesis queued in background ({workers} worker{'s' if workers > 1 else ''})",
    }


@router.post("/{batch_id}/refresh")
async def refresh_batch(batch_id: str):
    try:
        batch = load_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Batch not found")

    updated = 0
    for row in batch.get("rows", []):
        golden_id = row.get("golden_id")
        if not golden_id:
            continue

        run = find_latest_run_for_golden_id(golden_id)
        if not run:
            continue

        status = run.get("status", "unknown")
        conclusion = run.get("conclusion") or ""
        row["workflowStatus"] = status
        if status == "completed":
            row["status"] = "complete" if conclusion == "success" else "failed"
        elif status in ("queued", "waiting"):
            row["status"] = "queued"
        else:
            row["status"] = "running"

        row["result"] = {
            "message": f"Workflow {status}",
            "conclusion": conclusion,
            "run_url": run.get("html_url", ""),
            "updated_at": run.get("updated_at"),
        }
        updated += 1

    if updated:
        save_batch(batch)

    return {"batchId": batch_id, "updatedRows": updated}


async def _trigger_batch_bg(batch_id: str, workflow_label: Optional[str] = None, workers: int = 2):
    """Background task: synthesize batch rows and dispatch to GitHub."""
    _BATCH_TASKS[batch_id] = asyncio.current_task()
    try:
        batch = load_batch(batch_id)
    except FileNotFoundError:
        _BATCH_TASKS.pop(batch_id, None)
        return

    try:
        created_ids, errors = await _synthesize_batch_rows(batch, workers=workers)
        golden_ids = [row["golden_id"] for row in batch.get("rows", []) if row.get("golden_id")]
        
        if not golden_ids:
            batch["status"] = "failed"
            batch["error"] = "No golden artifacts created for batch"
            save_batch(batch)
            return

        from server import git_sync_goldens
        sync_result = git_sync_goldens(f"Batch {batch_id} generated {len(created_ids)} golden(s)")

        # Only pass inputs that the workflow actually declares — batch_id is internal only
        inputs: dict = {"golden_ids": ",".join(golden_ids)}
        if workflow_label:
            inputs["workflow_label"] = workflow_label

        try:
            result = dispatch_github_workflow(inputs)
            batch["status"] = "dispatched"
            batch["dispatchedAt"] = result.get("dispatchedAt") or batch.get("dispatchedAt") or ""
            batch["goldenIds"] = golden_ids
            batch["syncResult"] = sync_result
        except Exception as exc:
            # Goldens are ready even if dispatch failed — mark as ready so user can retry dispatch
            batch["status"] = "ready"
            batch["error"] = f"Dispatch failed (goldens are ready): {exc}"
        
        save_batch(batch)
    except asyncio.CancelledError:
        pass  # Batch cancel set status already; just exit cleanly
    except Exception as e:
        batch["status"] = "failed"
        batch["error"] = str(e)
        save_batch(batch)
    finally:
        _BATCH_TASKS.pop(batch_id, None)

@router.post("/{batch_id}/trigger")
async def trigger_batch(batch_id: str, body: Optional[BatchTriggerRequest] = None):
    try:
        batch = load_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Batch not found")

    if batch.get("status") == "running":
        raise HTTPException(status_code=409, detail="Batch is already running")

    workers = max(1, min(int(body.workers if body else 2), 4))
    workflow_label = body.workflow_label if body else None
    batch["status"] = "running"
    batch["workers"] = workers
    save_batch(batch)

    asyncio.create_task(_trigger_batch_bg(batch_id, workflow_label, workers=workers))

    return {
        "batchId": batch_id,
        "status": "running",
        "workers": workers,
        "message": f"Synthesis and dispatch queued ({workers} worker{'s' if workers > 1 else ''})",
    }


@router.get("/{batch_id}/stream")
async def stream_batch_status(request: Request, batch_id: str):
    """SSE stream: emits row_update / batch_update events as synthesis and CI progress."""

    async def generator():
        try:
            batch = load_batch(batch_id)
        except FileNotFoundError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Batch not found'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'snapshot', 'batch': batch})}\n\n"

        prev_row_states = {
            r["rowId"]: (r.get("status"), r.get("workflowStatus"))
            for r in batch.get("rows", [])
        }
        prev_batch_status = batch.get("status")
        tick = 0

        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(1.5)
            tick += 1

            try:
                batch = load_batch(batch_id)
            except Exception:
                break

            # Row-level changes
            for row in batch.get("rows", []):
                rid = row.get("rowId", "")
                curr = (row.get("status"), row.get("workflowStatus"))
                if prev_row_states.get(rid) != curr:
                    prev_row_states[rid] = curr
                    yield f"data: {json.dumps({'type': 'row_update', 'row': row})}\n\n"

            # Batch-level status change
            curr_status = batch.get("status", "")
            if curr_status != prev_batch_status:
                prev_batch_status = curr_status
                yield f"data: {json.dumps({'type': 'batch_update', 'status': curr_status, 'batch': batch})}\n\n"

            # Heartbeat every ~15 ticks (~22 s)
            if tick % 10 == 0:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

            # Terminal check: all rows done and batch no longer active
            rows = batch.get("rows", [])
            all_done = rows and all(
                r.get("status") in ("complete", "failed", "cancelled")
                for r in rows
            )
            if all_done and curr_status not in ("running", "pending", "dispatched"):
                yield f"data: {json.dumps({'type': 'done', 'batch': batch})}\n\n"
                break

            # Safety timeout: 45 minutes
            if tick > 1800:
                break

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/{batch_id}/rows/{row_id}/cancel")
async def cancel_batch_row(batch_id: str, row_id: str):
    """Cancel a single row — works whether it is pending or actively synthesizing."""
    try:
        batch = load_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Batch not found")

    row = next((r for r in batch.get("rows", []) if r.get("rowId") == row_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")

    status = row.get("status", "pending")
    if status not in ("pending", "running", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Row is already '{status}' — cannot cancel",
        )

    # Mark for skip if the loop hasn't reached it yet
    _CANCEL_ROWS.setdefault(batch_id, set()).add(row_id)

    # If synthesis is actively running, interrupt the asyncio task
    task = _ROW_TASKS.get(f"{batch_id}:{row_id}")
    if task and not task.done():
        task.cancel()

    # Update JSON immediately so the SSE stream picks it up
    row["status"] = "cancelled"
    row["workflowStatus"] = "cancelled"
    row["result"] = {"message": "Cancelled by user", "conclusion": "cancelled"}
    save_batch(batch)

    return {"rowId": row_id, "status": "cancelled", "message": "Row cancelled"}


@router.post("/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    """Cancel a running batch — kills the background task and all active row tasks."""
    try:
        batch = load_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Batch not found")

    status = batch.get("status")
    if status not in ("running", "pending"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel batch in '{status}' state.",
        )

    # 1. Write cancelled status first so workers detect it on their next loop iteration
    batch["status"] = "cancelled"
    batch["cancelledAt"] = datetime.now().isoformat()
    for row in batch.get("rows", []):
        if row.get("status") in ("running", "pending"):
            row["status"] = "cancelled"
            row["workflowStatus"] = "cancelled"
            row["result"] = {"message": "Batch cancelled by user", "conclusion": "cancelled"}
    save_batch(batch)

    # 2. Cancel every active per-row synthesis task for this batch
    for key in list(_ROW_TASKS.keys()):
        if key.startswith(f"{batch_id}:"):
            t = _ROW_TASKS.get(key)
            if t and not t.done():
                t.cancel()

    # 3. Cancel the top-level batch background task (stops workers from picking up new rows)
    batch_task = _BATCH_TASKS.get(batch_id)
    if batch_task and not batch_task.done():
        batch_task.cancel()

    return {
        "batchId": batch_id,
        "status": "cancelled",
        "message": "Batch cancelled — all active synthesis tasks interrupted",
    }
