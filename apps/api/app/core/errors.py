"""RFC 7807 Problem Details 形式のエラー。`detail` はユーザーが読める日本語にする。"""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.money import InvariantViolation, MoneyError

log = logging.getLogger("warilink")


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str, **extra: object) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def not_found() -> ApiError:
    # 権限のないリソースは存在自体を隠す
    return ApiError(404, "NOT_FOUND", "見つかりませんでした")


def _problem(request: Request, status: int, code: str, detail: str, **extra: object) -> JSONResponse:
    body = {
        "type": f"https://warilink.invalid/problems/{code.lower()}",
        "title": code,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "request_id": request.headers.get("x-request-id") or str(uuid.uuid4()),
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json",
                        headers={"Cache-Control": "no-store"})


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError):
        return _problem(request, exc.status, exc.code, exc.detail, **exc.extra)

    @app.exception_handler(InvariantViolation)
    async def _inv(request: Request, exc: InvariantViolation):
        # 不変条件違反は内部不整合。確定処理はトランザクションごと中止されている。
        log.error("invariant violation path=%s msg=%s", request.url.path, exc.message)
        return _problem(request, 500, "CALCULATION_FAILED", "金額の検算に失敗したため処理を中止しました")

    @app.exception_handler(MoneyError)
    async def _money(request: Request, exc: MoneyError):
        return _problem(request, 422, exc.code, exc.message, errors=_jsonable(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _val(request: Request, exc: RequestValidationError):
        errs = [{"loc": list(e.get("loc", [])), "type": e.get("type")} for e in exc.errors()]
        return _problem(request, 422, "VALIDATION_ERROR", "入力内容に誤りがあります", errors=errs)


def _jsonable(obj: object) -> object:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, str | int | float | bool) or obj is None:
        return obj
    return str(obj)
