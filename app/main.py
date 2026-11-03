import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routers import admin, auth, public, webhooks

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    app = FastAPI(title="rohit-portfolio-api", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        max_age=86400,
    )

    # One error shape everywhere: {"error": "<message>"} (+ "fields" for validation errors)
    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        fields = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": "Invalid request", "fields": fields}, status_code=422)

    app.include_router(public.router)
    app.include_router(webhooks.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    return app


app = create_app()
