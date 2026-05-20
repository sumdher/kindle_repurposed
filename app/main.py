"""
Kindle e-ink landscape dashboard — FastAPI entry point.

Physical display: 1264×1680px portrait. Effective browser viewport: 1264×1465px.
The Kindle is used rotated 90° clockwise (power button right), so the viewer sees
a landscape 1465×1264px area. We achieve this by designing a 1465×1264 .page div
and rotating it with CSS:  transform: translateY(1465px) rotate(-90deg).

Layout inside .page (landscape, 1465×1264):
  Padding: 16px all sides → content: 1433×1232px
  Left panel (420px wide) : current conditions, daily summary
  Gap (20px)
  Right panel (993px wide): hourly chart + 48h graphs
"""

from __future__ import annotations

import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime

import pytz
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.docker_client import get_containers
from app.store import WeatherSample, store
from app.svg import hourly_chart, line_graph, weather_icon_svg, wmo_icon_key
from app.weather import (
    CurrentWeather,
    DailyForecast,
    HourlyPoint,
    fetch_current_and_hourly,
    fetch_historical_samples,
    wind_direction_text,
    wmo_label,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ROME_TZ = pytz.timezone("Europe/Rome")
PORT      = 8080   # container-internal
HOST_PORT = 8888   # host port mapped in docker-compose

# ---------------------------------------------------------------------------
# Landscape SVG dimensions
#
# Right panel is 993px wide, 1232px tall. Vertical split:
#   26 (hourly label) + 6 + 325 (hourly chart) + 14 + 26 (graph label) + 6 + 829 = 1232
# ---------------------------------------------------------------------------
RIGHT_W   = 1088
HOURLY_H  = 325
# Graphs are stacked vertically (full right-panel width each).
# Heights: 1232 - 26(hourly title) - 5 - 325(hourly) - 14 - 26(graph title) - 5 = 831px
# Two graphs stacked with 10px gap: (831 - 10) / 2 = 410px each.
GRAPH_W   = RIGHT_W   # 893 — full right-panel width
GRAPH_H   = 410

# ---------------------------------------------------------------------------
# In-memory cache for last successful fetch
# ---------------------------------------------------------------------------
_cache: dict = {
    "current": None,
    "daily":   None,
    "hourly":  [],
    "stale":   False,
}


# ---------------------------------------------------------------------------
# Scheduler jobs
# ---------------------------------------------------------------------------

async def _collect_weather() -> None:
    current, daily, hourly = await fetch_current_and_hourly()
    if current is not None:
        store.append(WeatherSample(
            timestamp=current.updated_at,
            pressure_hpa=current.pressure,
            wind_speed_kmh=current.wind_speed,
        ))
        _cache.update({"current": current, "daily": daily, "hourly": hourly, "stale": False})
        logger.info(
            "Weather: %.1f°C, %.1f hPa, %.1f km/h",
            current.temperature, current.pressure, current.wind_speed,
        )
    else:
        _cache["stale"] = True
        logger.warning("Weather fetch failed — serving stale data")


async def _backfill() -> None:
    historical = await fetch_historical_samples()
    two_hourly = [s for s in historical if s[0].hour % 2 == 0]
    for ts, pressure, wind in two_hourly[-24:]:
        store.append(WeatherSample(timestamp=ts, pressure_hpa=pressure, wind_speed_kmh=wind))
    logger.info("Backfilled store with %d samples", len(store.get_all()))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler(timezone=ROME_TZ)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _backfill()
    await _collect_weather()

    scheduler.add_job(_collect_weather, "interval", hours=2, id="weather")
    scheduler.start()

    ip = os.environ.get("ANNOUNCE_IP") or _local_ip()
    print(f"\n  Open on Kindle:  http://{ip}:{HOST_PORT}/weather\n", flush=True)

    yield
    scheduler.shutdown()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/weather", response_class=HTMLResponse)
async def weather_page(request: Request):
    current, daily, hourly = await fetch_current_and_hourly()
    if current is None:
        current = _cache.get("current")
        daily   = _cache.get("daily")
        hourly  = _cache.get("hourly") or []
        stale   = True
    else:
        _cache.update({"current": current, "daily": daily, "hourly": hourly, "stale": False})
        stale = False
        # NOTE: do NOT write to store here. store is written only by _collect_weather()
        # (APScheduler, every 2h). Writing on every page-load would flood the deque with
        # 5-min-interval samples, making the 48h graphs useless.

    samples = store.get_all()

    pressure_svg = line_graph(samples, "pressure_hpa", GRAPH_W, GRAPH_H,
                              "Atmospheric Pressure (48h)", "hPa")
    wind_svg     = line_graph(samples, "wind_speed_kmh", GRAPH_W, GRAPH_H,
                              "Wind Speed (48h)", "km/h")
    hourly_svg   = hourly_chart(hourly, RIGHT_W, HOURLY_H)

    ctx = {
        "current":         current,
        "daily":           daily,
        "stale":           stale,
        "condition_label": wmo_label(current.weather_code) if current else "—",
        "condition_icon":  weather_icon_svg(wmo_icon_key(current.weather_code), 100) if current else "",
        "wind_dir":        wind_direction_text(current.wind_direction) if current else "",
        "pressure_svg":    pressure_svg,
        "wind_svg":        wind_svg,
        "hourly_svg":      hourly_svg,
        "now":             datetime.now(ROME_TZ).strftime("%H:%M, %d %b %Y"),
        "today_date":      datetime.now(ROME_TZ).strftime("%A, %d %B %Y"),
    }
    return templates.TemplateResponse(request, "weather.html", ctx)


@app.get("/docker", response_class=HTMLResponse)
async def docker_page(request: Request):
    containers, error = get_containers()
    ctx = {
        "containers": containers,
        "error":      error,
        "now":        datetime.now(ROME_TZ).strftime("%H:%M:%S, %d %b %Y"),
    }
    return templates.TemplateResponse(request, "docker.html", ctx)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=False)
