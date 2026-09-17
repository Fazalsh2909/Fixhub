"""Demo FastAPI app."""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.auth.middleware import auth_middleware

app = FastAPI()


@app.middleware("http")
async def _auth(request, call_next):
    try:
        return await auth_middleware(request, call_next)
    except Exception:
        # framework-level fallback currently maps everything to 500 (part of the bug story)
        return JSONResponse({"detail": "internal error"}, status_code=500)


@app.get("/me")
def me():
    return {"ok": True}
