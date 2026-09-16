"""HomeBrain - Lightweight Home Device Manual Manager.

A simple web application that helps you manage and search through device manuals.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from homebrain.config import settings
from homebrain.db import init_db
from homebrain.api.devices import router as devices_router
from homebrain.api.downloads import router as downloads_router
from homebrain.api.search import router as search_router
from homebrain.api.chat import router as chat_router
from homebrain.api.settings import router as settings_router
from homebrain.api.calendar import router as calendar_router
from homebrain.api.notifications import router as notifications_router
from homebrain.services.notifier import notification_loop

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting HomeBrain...")
    await init_db()
    logger.info(f"Data directory: {settings.DATA_DIR}")
    logger.info(f"LLM model: {settings.LLM_MODEL}")

    # Background due-event notifier (services/notifier.py). uvicorn runs a
    # single process, so one task per container is enough; it re-reads the
    # notify_* settings every tick and no-ops while NOTIFY_ENABLED is off.
    notifier_task = asyncio.create_task(notification_loop())

    yield

    notifier_task.cancel()
    try:
        await notifier_task
    except asyncio.CancelledError:
        pass

    # Shutdown
    logger.info("Shutting down HomeBrain...")


# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Lightweight home device manual manager with AI chat",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_html(request: Request, call_next):
    """Always revalidate HTML so a browser refresh picks up new frontend code.

    Without this, browsers may serve a stale cached index.html / app.js after
    an update (the JS/CSS are linked without cache-busting query strings).
    """
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/index.html") or path.startswith("/static/"):
        if path.endswith((".html", ".js", ".css")) or path == "/":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# Register API routers
app.include_router(devices_router, prefix="/api")
app.include_router(downloads_router, prefix="/api")
app.include_router(search_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(calendar_router, prefix="/api")
app.include_router(notifications_router, prefix="/api")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION
    }


@app.get("/")
async def root():
    """Serve the main frontend application."""
    # Try multiple paths for index.html depending on environment
    possible_paths = [
        "static/index.html",  # Docker container path
        "/app/static/index.html",  # Absolute Docker path
        "frontend/index.html",  # Local development path
    ]
    
    from pathlib import Path
    for path in possible_paths:
        if Path(path).exists():
            return FileResponse(path)
    
    # If no index found, return a simple message
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content="<h1>HomeBrain API is running</h1><p>Frontend not found.</p>")


# Mount static files
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception as e:
    logger.warning(f"Could not mount static files: {e}")


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "homebrain.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG
    )
