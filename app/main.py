"""
Kindle e-ink landscape dashboard — FastAPI entry point.

Physical display: 1264×1680px portrait. Effective browser viewport: 1264×1465px.
Landscape via CSS: .page is 1680×1264px rotated -90° with transform-origin: 0 0.

KINDLE_SERVICE env var controls which APScheduler jobs run in this process:
  weather  → weather backfill + 2h collection job
  ups      → UPS 5-min history collection job
  docker   → no background jobs (stats fetched per request)
  esp32    → no background jobs (InfluxDB queried per request)
  all      → all jobs (single-container dev mode)
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime

import pytz
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.docker_client import get_containers
from app.esp32_client import fetch_latest as fetch_esp32_latest
from app.esp32_client import fetch_history as fetch_esp32_history
from app.store import WeatherSample, store
from app.svg import docker_bar_chart, hourly_chart, line_graph, weather_icon_svg, wmo_icon_key
from app.ups_client import UpsSample, _ups_cache, get_ups_reading, ups_store
from app.weather import (
    fetch_current_and_hourly, fetch_historical_samples,
    wind_direction_text, wmo_label,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

class _SuppressReloadSignal(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/reload-signal" not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(_SuppressReloadSignal())

ROME_TZ  = pytz.timezone("Europe/Rome")
PORT      = 8080   # container-internal port
HOST_PORT = 8888   # default host port (k_weathermon)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

SERVICE      = os.environ.get("KINDLE_SERVICE", "all")   # weather|docker|ups|esp32|all
ANNOUNCE_IP  = os.environ.get("ANNOUNCE_IP", "")
LANDING_URL  = os.environ.get("LANDING_URL", "/")        # back-button target

# Cross-service navigation URLs (set in docker-compose for multi-container mode)
WEATHER_URL  = os.environ.get("WEATHER_URL",  "/weather")
DOCKER_URL   = os.environ.get("DOCKER_URL",   "/docker")
UPS_URL      = os.environ.get("UPS_URL",      "/ups")
ESP32_URL    = os.environ.get("ESP32_URL",    "/esp32")

# NUT (UPS) settings
NUT_HOST     = os.environ.get("NUT_HOST",  "127.0.0.1")
NUT_PORT     = int(os.environ.get("NUT_PORT",  "3493"))
UPS_NAME_CFG = os.environ.get("UPS_NAME",  "greencell")

# InfluxDB (ESP32) settings
INFLUX_URL    = os.environ.get("INFLUX_URL",    "")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN",  "")
INFLUX_ORG    = os.environ.get("INFLUX_ORG",    "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "esp32")

# ---------------------------------------------------------------------------
# SVG layout constants
# ---------------------------------------------------------------------------

# Right panel: 1648 - 520(left) - 40(gap) = 1088px wide, 1232px tall
RIGHT_W  = 1088
HOURLY_H = 325
GRAPH_W  = RIGHT_W
GRAPH_H  = 410

# UPS / ESP32 right panel: 3 graphs stacked
# 1232 - 3×(22+5)(titles) - 2×10(gaps) = 1232 - 81 - 20 = 1131 → 377 each
AUX_GRAPH_W = 1088
AUX_GRAPH_H = 374

# ---------------------------------------------------------------------------
# In-memory caches for last successful fetches
# ---------------------------------------------------------------------------

_weather_cache: dict = {
    "current": None, "daily": None, "hourly": [], "stale": False,
}

# Remote reload flag — set via GET /set-reload, consumed by GET /reload-signal
_reload_flag: bool = False


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
        _weather_cache.update({
            "current": current, "daily": daily, "hourly": hourly, "stale": False,
        })
        logger.info("Weather: %.1f°C %.1f hPa %.1f km/h",
                    current.temperature, current.pressure, current.wind_speed)
    else:
        _weather_cache["stale"] = True
        logger.warning("Weather fetch failed — serving stale data")


async def _backfill() -> None:
    historical = await fetch_historical_samples()
    two_hourly = [s for s in historical if s[0].hour % 2 == 0]
    for ts, pressure, wind in two_hourly[-24:]:
        store.append(WeatherSample(timestamp=ts, pressure_hpa=pressure, wind_speed_kmh=wind))
    logger.info("Weather backfill: %d samples", len(store.get_all()))


async def _collect_ups() -> None:
    reading = await asyncio.to_thread(
        get_ups_reading, NUT_HOST, NUT_PORT, UPS_NAME_CFG
    )
    if reading is not None:
        ups_store.append(UpsSample(
            timestamp=reading.updated_at,
            load_pct=reading.load_pct,
            watts=reading.watts,
            input_voltage=reading.input_voltage,
            battery_charge=reading.battery_charge,
        ))
        _ups_cache.update({"reading": reading, "stale": False})
        logger.info("UPS: %s load=%d%% bat=%d%%",
                    reading.status, reading.load_pct, reading.battery_charge)
    else:
        _ups_cache["stale"] = True
        logger.warning("UPS poll failed — serving stale data")


# ---------------------------------------------------------------------------
# Lifespan — start only the jobs needed for this service
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler(timezone=ROME_TZ)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if SERVICE in ("all", "weather"):
        await _backfill()
        await _collect_weather()
        scheduler.add_job(_collect_weather, "interval", hours=2, id="weather")

    if SERVICE in ("all", "ups"):
        await _collect_ups()
        scheduler.add_job(_collect_ups, "interval", minutes=5, id="ups")

    if scheduler.get_jobs():
        scheduler.start()

    ip = ANNOUNCE_IP or _local_ip()
    print(f"\n  Kindle Dashboard [{SERVICE}]:  http://{ip}:{HOST_PORT}/\n", flush=True)

    yield
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.middleware("http")
async def no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/reload-signal")
async def reload_signal():
    global _reload_flag
    flag, _reload_flag = _reload_flag, False
    return JSONResponse({"reload": flag})


@app.get("/set-reload")
async def set_reload():
    global _reload_flag
    _reload_flag = True
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = {
        "weather_url": WEATHER_URL,
        "docker_url":  DOCKER_URL,
        "ups_url":     UPS_URL,
        "esp32_url":   ESP32_URL,
    }
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/weather", response_class=HTMLResponse)
async def weather_page(request: Request):
    current, daily, hourly = await fetch_current_and_hourly()
    if current is None:
        current = _weather_cache.get("current")
        daily   = _weather_cache.get("daily")
        hourly  = _weather_cache.get("hourly") or []
        stale   = True
    else:
        _weather_cache.update({
            "current": current, "daily": daily, "hourly": hourly, "stale": False,
        })
        stale = False

    samples      = store.get_all()
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
        "back_url":        LANDING_URL,
    }
    return templates.TemplateResponse(request, "weather.html", ctx)


@app.get("/docker", response_class=HTMLResponse)
async def docker_page(request: Request, refresh: int = 15):
    containers, error = await asyncio.to_thread(get_containers)
    refresh_secs  = max(5, min(refresh, 900))
    running_count = sum(1 for c in containers if c.status == "running")
    with_stats    = [c for c in containers if c.stats_ok]

    cpu_sorted = sorted(with_stats, key=lambda c: c.cpu_pct, reverse=True)[:8]
    cpu_max    = max((c.cpu_pct for c in cpu_sorted), default=1.0) or 1.0
    cpu_chart  = docker_bar_chart(
        [(c.name, c.cpu_pct) for c in cpu_sorted], cpu_max, 448, 460)

    mem_sorted = sorted(with_stats, key=lambda c: c.mem_pct, reverse=True)[:8]
    mem_chart  = docker_bar_chart(
        [(c.name, c.mem_pct) for c in mem_sorted], 100.0, 448, 460)

    ctx = {
        "containers":    containers,
        "error":         error,
        "now":           datetime.now(ROME_TZ).strftime("%H:%M:%S, %d %b %Y"),
        "refresh_secs":  refresh_secs,
        "running_count": running_count,
        "stopped_count": len(containers) - running_count,
        "cpu_chart":     cpu_chart,
        "mem_chart":     mem_chart,
        "back_url":      LANDING_URL,
    }
    return templates.TemplateResponse(request, "docker.html", ctx)


@app.get("/ups", response_class=HTMLResponse)
async def ups_page(request: Request, refresh: int = 30):
    # Always try a live poll; fall back to cache if NUT is unreachable.
    reading = await asyncio.to_thread(
        get_ups_reading, NUT_HOST, NUT_PORT, UPS_NAME_CFG
    )
    if reading is not None:
        _ups_cache.update({"reading": reading, "stale": False})
        stale = False
        # Append to store on page load, but at most once per minute, so graphs
        # update on every visible refresh without flooding the 48h deque.
        if not ups_store or (reading.updated_at - ups_store[-1].timestamp).total_seconds() >= 60:
            ups_store.append(UpsSample(
                timestamp=reading.updated_at,
                load_pct=reading.load_pct,
                watts=reading.watts,
                input_voltage=reading.input_voltage,
                battery_charge=reading.battery_charge,
            ))
    else:
        reading = _ups_cache.get("reading")
        stale   = True

    samples = list(ups_store)

    def _svg(attr: str, title: str, unit: str) -> str:
        return line_graph(samples, attr, AUX_GRAPH_W, AUX_GRAPH_H, title, unit) if samples else ""

    refresh_secs = max(10, min(refresh, 900))
    ctx = {
        "reading":      reading,
        "stale":        stale,
        "nut_host":     NUT_HOST,
        "nut_port":     NUT_PORT,
        "load_svg":     _svg("load_pct",       "Load %",           "%"),
        "voltage_svg":  _svg("input_voltage",  "Input Voltage",    "V"),
        "battery_svg":  _svg("battery_charge", "Battery Charge",   "%"),
        "refresh_secs": refresh_secs,
        "now":          datetime.now(ROME_TZ).strftime("%H:%M:%S, %d %b %Y"),
        "back_url":     LANDING_URL,
        "samples":      samples,
    }
    return templates.TemplateResponse(request, "ups.html", ctx)


@app.get("/esp32", response_class=HTMLResponse)
async def esp32_page(request: Request, refresh: int = 60):
    reading = None
    history  = []
    error    = None

    if not INFLUX_URL or not INFLUX_TOKEN:
        error = "InfluxDB not configured — set INFLUX_URL and INFLUX_TOKEN env vars."
    else:
        try:
            reading = await fetch_esp32_latest(
                INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET)
            history = await fetch_esp32_history(
                INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET)
        except Exception as exc:
            error = str(exc)
            logger.warning("ESP32 data fetch error: %s", exc)

    def _svg(attr: str, title: str, unit: str) -> str:
        return line_graph(history, attr, AUX_GRAPH_W, AUX_GRAPH_H, title, unit) if history else ""

    refresh_secs = max(30, min(refresh, 900))
    ctx = {
        "reading":      reading,
        "error":        error,
        "temp_svg":     _svg("temperature", "Temperature",    "°C"),
        "humid_svg":    _svg("humidity",    "Humidity",       "%"),
        "press_svg":    _svg("pressure",    "Pressure",       "hPa"),
        "refresh_secs": refresh_secs,
        "now":          datetime.now(ROME_TZ).strftime("%H:%M:%S, %d %b %Y"),
        "back_url":     LANDING_URL,
        "history":      history,
    }
    return templates.TemplateResponse(request, "esp32.html", ctx)


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
