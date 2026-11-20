from fastapi import APIRouter, Request, Response

from app.manifest import canonical
from app.modules.m8_supplement.archiving import replay_supplement, submit_supplement

router = APIRouter(prefix="/api", tags=["追加补裁"])


@router.post("/runs/{run_id}/supplements", status_code=201)
def create(run_id: str, payload: dict, request: Request, response: Response):
    manifest, reused = submit_supplement(request.app.state.archive, run_id, payload)
    if reused:
        response.status_code = 200
    return {"manifest": manifest, "reused": reused}


@router.get("/runs/{run_id}/supplements")
def listing(run_id: str, request: Request):
    return {"supplements": request.app.state.archive.supplements(run_id)}


@router.get("/supplements/{supplement_id}")
def archived(supplement_id: str, request: Request):
    return request.app.state.archive.read_supplement(supplement_id)


@router.post("/supplements/{supplement_id}/rerun")
def rerun(supplement_id: str, request: Request):
    manifest = replay_supplement(request.app.state.archive, supplement_id)
    return {"manifest": manifest, "verified": True}


@router.get("/supplements/{supplement_id}/export")
def download(supplement_id: str, request: Request):
    document = request.app.state.archive.read_supplement(supplement_id)
    return Response(canonical(document) + "\n", media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="fabric-supp-{supplement_id[:12]}.json"'})
