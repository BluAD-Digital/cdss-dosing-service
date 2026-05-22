import time
import uuid
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.routers.dosing import router as dosing_router
from app.cache.redis import close_redis, create_redis, get_cached
from app.config import settings
from app.db.postgres import close_pool, create_pool
from app.utils.logger import configure_logging, get_logger, set_request_id

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    app.state.redis = await create_redis()
    logger.info("service ready", environment=settings.ENVIRONMENT)
    yield
    await close_pool(app.state.pool)
    await close_redis(app.state.redis)
    logger.info("service shutdown complete")


app = FastAPI(
    title="CDSS Dosing Service",
    version="1.0.0",
    description="Clinical Decision Support System — drug dosing recommendations for Indian practitioners",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ── API Key middleware ──────────────────────────────────────────────────────────
@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    if request.url.path in ("/health", "/docs", "/redoc", "/openapi.json"):
        return await call_next(request)

    api_key = request.headers.get("X-API-Key")
    if not api_key or api_key != settings.API_KEY:
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "Missing or invalid API key"},
        )
    return await call_next(request)


# ── Request logging + request-id middleware ─────────────────────────────────────
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    set_request_id(request_id)

    t0 = time.perf_counter()
    response: Response = await call_next(request)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    drug_id = None
    if hasattr(request.state, "_body"):
        try:
            import json
            body = json.loads(request.state._body)
            drug_id = body.get("drug_id_1mg")
        except Exception:
            pass

    logger.info(
        "http request",
        method=request.method,
        path=request.url.path,
        drug_id_1mg=drug_id,
        status_code=response.status_code,
        latency_ms=latency_ms,
        request_id=request_id,
    )
    return response


# ── Exception handlers ──────────────────────────────────────────────────────────
_STATUS_DEFAULTS = {
    404: {"error": "not_found", "message": "The requested resource was not found"},
    422: {"error": "validation_error", "message": "Request validation failed"},
    500: {"error": "internal_error", "message": "An internal server error occurred"},
}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_STATUS_DEFAULTS.get(
            exc.status_code,
            {"error": str(exc.status_code), "message": str(exc.detail)},
        ),
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    logger.error("unhandled exception", error=str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content=_STATUS_DEFAULTS[500],
    )


# ── Health endpoint ─────────────────────────────────────────────────────────────
@app.get("/health", tags=["health"])
async def health(request: Request):
    db_status = "connected"
    cache_status = "connected"
    http_status = 200

    try:
        async with request.app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        logger.warning("health check DB failed", error=str(exc))
        db_status = "disconnected"
        http_status = 503

    try:
        await request.app.state.redis.ping()
    except Exception as exc:
        logger.warning("health check Redis failed", error=str(exc))
        cache_status = "disconnected"
        http_status = 503

    return JSONResponse(
        status_code=http_status,
        content={"status": "ok" if http_status == 200 else "degraded", "db": db_status, "cache": cache_status},
    )


# ── Routers ─────────────────────────────────────────────────────────────────────
app.include_router(dosing_router, prefix="/api/v1")


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["ApiKeyAuth"] = {
        "type": "apiKey", "in": "header", "name": "X-API-Key"
    }
    for path in schema.get("paths", {}).values():
        for operation in path.values():
            operation.setdefault("security", [{"ApiKeyAuth": []}])
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi
