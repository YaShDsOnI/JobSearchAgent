import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    applications,
    auth,
    catalog,
    cv,
    health,
    profile,
    reviews,
    submissions,
    tracker,
)
from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime_secrets()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token", "Authorization"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "Request validation failed",
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled request error",
        extra={"request_id": request.state.request_id, "error_type": type(exc).__name__},
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "Internal server error",
            "request_id": request.state.request_id,
        },
    )


app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(profile.router, prefix=settings.api_prefix)
app.include_router(catalog.router, prefix=settings.api_prefix)
app.include_router(reviews.router, prefix=settings.api_prefix)
app.include_router(cv.router, prefix=settings.api_prefix)
app.include_router(applications.router, prefix=settings.api_prefix)
app.include_router(tracker.router, prefix=settings.api_prefix)
app.include_router(submissions.router, prefix=settings.api_prefix)


@app.get("/", tags=["meta"])
def root():
    return {"service": settings.app_name, "version": "0.1.0", "phase": "application-review-package"}
