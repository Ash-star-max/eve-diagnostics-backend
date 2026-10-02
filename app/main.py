import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.config import get_settings
from app.database import Base, engine
from app.errors import register_exception_handlers
from app.logging_config import configure_logging, request_id_ctx
from app.routers import auth, bookings, catalog, centres, payments

logger = logging.getLogger("app.request")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # create_all is fine for an assignment; production would use Alembic migrations.
    Base.metadata.create_all(bind=engine)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description="Book diagnostic tests and pay for them through a simulated payment provider.",
        lifespan=lifespan,
    )
    register_exception_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request_completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                },
            )
            return response
        finally:
            request_id_ctx.reset(token)

    for router in (auth.router, centres.router, catalog.router, bookings.router, payments.router):
        app.include_router(router)

    @app.get("/health", tags=["meta"], summary="Liveness check")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
