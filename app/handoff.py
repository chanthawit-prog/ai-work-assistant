"""Hand-off: start the next step of the work from a result, e.g. a quotation from a meeting summary or from an
engineering answer, then an email that sends the quotation. The chosen output files are copied into the new job's
input/, the customer carries over, both jobs link to each other, and Claude is told where the files came from.
"""
from __future__ import annotations

from pathlib import Path

from .convert import prepare_inputs
from .jobtypes import JOB_TYPES
from .store import DONE, NEEDS_REVIEW, JobStore, now_iso

# meeting summaries need a transcript, and the follow-up job reads the whole follow-up list: neither starts from a result
HANDOFF_TYPES = ("proposal-draft", "sales-brief", "eng-qa", "general")
MAX_FILES = 10


def default_files(store: JobStore, job: dict) -> list[str]:
    """The job type's declared outputs that exist, else every output file."""
    job_type = JOB_TYPES.get(job["type"])
    declared = [o.split("/", 1)[1] for o in (job_type.outputs if job_type else ())]
    present = {f["path"] for f in store.list_files(job["id"], "output")}
    return [f for f in declared if f in present] or sorted(present)


def handoff(store: JobStore, job_id: str, type_id: str, instructions: str = "", title: str = "",
            customer: str | None = None, files: list[str] | None = None, inputs: list[str] | None = None,
            effort: str = "") -> dict:
    """Create the follow-on job with the chosen output files and, if asked, files attached to the original job
    (a nameplate photo, the transcript). Call it without awaiting in between (as the job form does) so the worker
    never picks the new job up before its files are in."""
    source = store.get(job_id)
    if source is None:
        raise KeyError(job_id)
    if source["status"] not in (NEEDS_REVIEW, DONE):
        raise ValueError("ส่งต่อได้เมื่องานนี้มีผลลัพธ์แล้ว (รอตรวจหรืออนุมัติแล้ว)")
    job_type = JOB_TYPES.get(type_id)
    if job_type is None or type_id not in HANDOFF_TYPES:
        labels = " ".join(JOB_TYPES[t].label for t in HANDOFF_TYPES)
        raise ValueError(f"ส่งต่อได้เป็นงานเหล่านี้: {labels}")
    chosen = list(dict.fromkeys(files or default_files(store, source)))
    originals = list(dict.fromkeys(inputs or []))
    if not chosen:
        raise ValueError("งานนี้ยังไม่มีไฟล์ผลลัพธ์ให้ส่งต่อ")
    if len(chosen) + len(originals) > MAX_FILES:
        raise ValueError(f"ส่งต่อได้ครั้งละไม่เกิน {MAX_FILES} ไฟล์")
    paths = {"output": [], "input": []}
    for area, rels in (("output", chosen), ("input", originals)):
        for rel in rels:
            path = store.file_path(job_id, area, rel)
            if not path.is_file():
                raise KeyError(rel)
            paths[area].append(path)

    params = source.get("params") or {}
    values = {"instructions": instructions.strip(),
              "customer": (params.get("customer", "") if customer is None else customer).strip()}
    for field in job_type.fields:
        if field.required and field.kind != "files" and field.name in values and not values[field.name]:
            raise ValueError(f"กรุณากรอก: {field.label}")
    # titles read "what this step does · what the work is about", and the subject stays that of the first job
    # in the chain (they also name the files in the library, so they must not grow with every step)
    subject = params.get("subject") or source["title"]
    title = (title.strip() or f"{job_type.label} · {subject}")[:150]
    new_params = {"customer": values["customer"]} if values["customer"] else {}
    if effort:
        new_params["effort"] = effort
    new_params.update(subject=subject, from_job=job_id, from_title=source["title"], from_type=source["type"],
                      from_approved=source["status"] == DONE)

    job = store.create(type_id, title, values["instructions"], new_params, source="handoff")
    names = {area: [store.copy_file(job["id"], "input", Path(p.name).name, p) for p in found]
             for area, found in paths.items()}
    warnings = prepare_inputs(store.area_dir(job["id"], "input"))
    job = store.update(job["id"], params={**new_params, "from_files": names["output"], "from_inputs": names["input"]},
                       warnings=warnings)
    with store.lock:
        current = store.get(job_id)
        if current is not None:
            current["handoffs"] = list(current.get("handoffs") or []) + [
                {"job_id": job["id"], "type": type_id, "at": now_iso()}]
            store.save(current)
    return job
