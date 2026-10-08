from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import auth, audit, reports, callbacks, devices, merchants, providers, shops, tellers, tills, transactions, users

app = FastAPI(title="PayPulse", version="0.1.0")

# Local dev origins for the Vite frontend. Tighten this to your real deployed
# frontend origin(s) before this is anywhere near production — "*" is never
# safe once real auth tokens are involved.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(transactions.router)
app.include_router(callbacks.router)
app.include_router(merchants.router)
app.include_router(providers.router)
app.include_router(users.router)
app.include_router(audit.router)
app.include_router(reports.router)
app.include_router(shops.router)
app.include_router(tills.router)
app.include_router(tellers.router)
app.include_router(devices.router)
app.include_router(devices.admin_router)
app.include_router(devices.device_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
