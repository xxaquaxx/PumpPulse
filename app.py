from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

terminal = None
autopilot = None

try:
    import terminal
except Exception as e:
    print("TERMINAL NOT LOADED:", repr(e))

try:
    import autopilot
except Exception as e:
    print("AUTOPILOT NOT LOADED:", repr(e))


@asynccontextmanager
async def lifespan(app):
    if autopilot:
        autopilot.start()
    yield


app = FastAPI(lifespan=lifespan)

if terminal:
    app.include_router(terminal.router)
if autopilot:
    app.include_router(autopilot.router)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=/terminal">')
