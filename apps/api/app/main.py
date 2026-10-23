from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core import errors
from app.core.config import get_settings
from app.modules.expenses.router import router as expenses_router
from app.modules.groups.router import router as groups_router
from app.modules.identity.router import router as identity_router
from app.modules.meta.router import router as meta_router
from app.modules.payment_destinations.router import router as destinations_router
from app.modules.receipts.router import router as receipts_router
from app.modules.settlement.router import router as settlement_router

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    s = get_settings()
    # 本番の危険な設定は get_settings() の検証で起動前に拒否される(config.production_problems)
    prod = s.environment == "production"
    app = FastAPI(title="WariLink API", version="0.1.0",
                  openapi_url=None if prod else f"{API_PREFIX}/openapi.json",
                  docs_url=None if prod else f"{API_PREFIX}/docs", redoc_url=None if prod else "/redoc")
    app.add_middleware(
        CORSMiddleware, allow_origins=s.allowed_origins, allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["content-type", "idempotency-key", "if-match", "x-warilink-csrf"],
    )

    @app.middleware("http")
    async def private_headers(request: Request, call_next):
        response = await call_next(request)
        # グループ・支出・精算は非公開。検索エンジン・共有キャッシュに載せない。
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    errors.install(app)
    for r in (identity_router, groups_router, expenses_router, settlement_router, meta_router,
              receipts_router, destinations_router):
        app.include_router(r, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "environment": s.environment}

    return app


app = create_app()
