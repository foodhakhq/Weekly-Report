from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Tuple, Dict, Any, List
import json
import httpx
import asyncio
import math
import re
import os
import random
import logging
import time
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict
from urllib.parse import urlencode, quote
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()

API_BASE = os.getenv("API_BASE")
VALID_API_KEY = os.getenv("VALID_API_KEY")
FOODHAK_CSRF_TOKEN = os.getenv("FOODHAK_CSRF_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
HEALTHDATA_BEARER = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbiIsImV4cCI6MTgxNTMwMDg0MH0.TPj0mDBuk9EHFUcUaTZq8XTkKcK7Q4JfAcKUdjDGsw8"
# ==========================================
# LOGGING CONFIGURATION
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("weekly_report")

app = FastAPI(title="Weekly Reports API")

# ==========================================
# 1. CONFIGURATION & CONSTANTS
# ==========================================

HEADERS = {
    "Authorization": f"Api-Key {VALID_API_KEY}",
    "accept": "application/json",
}
if FOODHAK_CSRF_TOKEN:
    HEADERS["X-CSRFToken"] = FOODHAK_CSRF_TOKEN

TIMEOUT_SECONDS = 10.0
# Shared catalog from GET /health-concerns/goals/ (same for every user); not per-user state.
_goals_catalog_cache: Optional[List[Dict[str, Any]]] = None

# ==========================================
# REQUEST/RESPONSE MODELS
# ==========================================

class WeeklyReportRequest(BaseModel):
    user_id: str
    start_date: str  # Format: YYYY-MM-DD (in user's local timezone)
    end_date: str  # Format: YYYY-MM-DD (in user's local timezone)


async def _safe_get(url: str, headers: dict = None, params: dict = None):
    """Wrapper for HTTP GET requests."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        return await client.get(url, headers=headers, params=params)


def _parse_iso_utc(date_str: Optional[str]) -> Optional[datetime]:
    """Parses ISO string or YYYY-MM-DD to datetime object."""
    if not date_str: return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d")
        except:
            return None


def _to_iso_z(dt: datetime) -> str:
    """Converts datetime to ISO string with Z suffix."""
    return dt.isoformat().replace("+00:00", "Z")


async def _fetch_tracker(user_id: str, tracker_type: str, start_date: str, end_date: str) -> dict:
    """Fetch Internal Foodhak Tracker Data (Fallback)."""
    type_map = {
        "steps": "STEPS", "sleep": "SLEEP", "weight": "WEIGHT", "mood": "MOOD_ENTRY"
    }
    db_type = type_map.get(tracker_type.lower(), tracker_type.upper())
    url = f"{API_BASE}/user-profile/{user_id}/tracker"
    query_string = f"type={db_type}&start_datetime={start_date}&end_datetime={end_date}"

    try:
        resp = await _safe_get(url, headers=HEADERS, params={"query": query_string})
        if resp.status_code == 200:
            data = resp.json()
            return {"status": "ok", "data": data.get("results", data.get("data", []))}
        return {"status": "error", "message": f"HTTP {resp.status_code}", "data": []}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}


async def connection_status(user_id: str) -> Tuple[bool, Optional[str]]:
    url = "https://healthdata.foodhak.com/api/v1/health/connection-status"
    headers = {"accept": "application/json", "Authorization": f"Bearer {HEALTHDATA_BEARER}"}
    params = {"user_id": user_id}

    try:
        resp = await _safe_get(url, headers=headers, params=params)
        if resp.status_code != 200:
            return False, None

        body = resp.json()
        data = body.get("data", [])
        if not data:
            return False, None

        # 1) keep ANY device_type we see
        last_seen_type = None
        for conn in data:
            if conn.get("device_type"):
                last_seen_type = conn.get("device_type")
            if conn.get("is_connected"):
                # 2) connected now
                return True, conn.get("device_type") or last_seen_type

        # 3) not connected now, but we may still know the type
        return False, last_seen_type
    except Exception:
        return False, None


def parse_dt_safe(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def foodhak_sleep_steps_fallback(user_id: str, start_date: str = None, end_date: str = None) -> dict:
    """Internal Fallback Logic."""
    adj_steps_start = start_date
    try:
        s_dt = _parse_iso_utc(start_date)
        e_dt = _parse_iso_utc(end_date)
        if s_dt and e_dt and (e_dt - s_dt) < timedelta(hours=24):
            adj_steps_start = _to_iso_z(e_dt - timedelta(hours=24))
    except Exception:
        pass

    sleep_res, steps_res = await asyncio.gather(
        _fetch_tracker(user_id, "sleep", adj_steps_start, end_date),
        _fetch_tracker(user_id, "steps", adj_steps_start, end_date),
    )

    errors = {}
    if sleep_res["status"] != "ok": errors["sleep"] = sleep_res["message"]
    if steps_res["status"] != "ok": errors["steps"] = steps_res["message"]

    if len(errors) == 2:
        return {"status": "error", "source": "foodhak", "sleep": None, "steps": None, "errors": errors}

    return {
        "status": "ok" if not errors else "partial",
        "source": "foodhak",
        "sleep": [] if "sleep" in errors else sleep_res["data"],
        "steps": [] if "steps" in errors else steps_res["data"],
        "errors": errors or None
    }


async def wearable_api(user_id: str, start_date: str, end_date: str, provider_type: str) -> dict:
    """Fetch from External Provider via Health Data API."""
    url = (
        f"https://healthdata.foodhak.com/api/v1/health/health-data/{user_id}"
        f"?provider_type={provider_type}&start_date={start_date}&end_date={end_date}"
    )
    headers = {"Authorization": f"Bearer {HEALTHDATA_BEARER}"}

    try:
        resp = await _safe_get(url, headers=headers)
        if resp.status_code >= 400:
            return {"status": "error", "message": f"HTTP {resp.status_code}", "source": "provider", "data": None}

        payload = resp.json()
        return {"status": "ok", "source": "provider", "provider_type": provider_type, "data": payload}
    except Exception as e:
        return {"status": "error", "message": str(e), "source": "provider", "data": None}


def safe_float(value):
    try:
        if value is None: return 0.0
        f_val = float(value)
        if math.isnan(f_val) or math.isinf(f_val): return 0.0
        return f_val
    except (ValueError, TypeError):
        return 0.0


def safe_int(value):
    try:
        return int(safe_float(value))
    except (ValueError, TypeError):
        return 0


# ==========================================
# AUTHENTICATION
# ==========================================

def verify_token(authorization: str = Header(None)):
    """Verify Bearer token from Authorization header"""
    if not authorization:
        logger.error("❌ Authorization header missing")
        raise HTTPException(status_code=401, detail="Authorization header missing")

    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            logger.error(f"❌ Invalid authentication scheme: {scheme}")
            raise HTTPException(status_code=401, detail="Invalid authentication scheme")

        if token != VALID_API_KEY:
            logger.error("❌ Invalid API key provided")
            raise HTTPException(status_code=401, detail="Invalid API key")

        return token
    except ValueError:
        logger.error("❌ Invalid Authorization header format")
        raise HTTPException(status_code=401, detail="Invalid Authorization header format")


# ==========================================
# TIMEZONE CONVERSION FUNCTIONS
# ==========================================

async def fetch_user_timezone(user_id: str) -> str:
    """Fetch user's timezone from their profile."""
    url = f"{API_BASE}/user-profile/foodhak-user-id/{user_id}/"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=HEADERS)
            response.raise_for_status()
            data = response.json()

        user_block = data.get("foodhak_user", {})
        tz = user_block.get("timezone", "UTC")
        logger.info(f"✅ Fetched timezone for user {user_id}: {tz}")
        return tz
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch timezone for user {user_id}, defaulting to UTC: {e}")
        return "UTC"


def convert_local_date_to_utc_window(local_date_str: str, user_tz: str) -> tuple:
    """
    Convert a local date (YYYY-MM-DD) to UTC start/end timestamps.
    Returns: Tuple of (start_utc_iso, end_utc_iso) in ISO format with Z suffix
    """
    try:
        local_day = datetime.strptime(local_date_str, "%Y-%m-%d").date()
    except ValueError as e:
        logger.error(f"❌ Invalid date format: {local_date_str}")
        raise ValueError(f"Date must be in YYYY-MM-DD format: {e}")

    tz = ZoneInfo(user_tz)

    # Create datetime at start of day in user's timezone
    local_start = datetime(local_day.year, local_day.month, local_day.day, 0, 0, 0, tzinfo=tz)
    local_end = local_start + timedelta(days=1)

    # Convert to UTC
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)

    # Format as ISO strings with Z suffix
    start_utc_str = start_utc.isoformat().replace("+00:00", "Z")
    end_utc_str = end_utc.isoformat().replace("+00:00", "Z")

    logger.debug(f"📅 Converted {local_date_str} ({user_tz}) -> UTC: {start_utc_str} to {end_utc_str}")
    return start_utc_str, end_utc_str


# ==========================================
# 2. SHARED FETCHERS (ASYNC)
# ==========================================

async def fetch_json(url, params=None, manual_query_string=None):
    full_url = url
    if manual_query_string:
        full_url = f"{url}?query={manual_query_string}"

    start_time = time.time()
    logger.debug(f"🌐 GET {full_url} | Params: {params}")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(full_url, headers=HEADERS, params=params)

        duration = round((time.time() - start_time) * 1000, 2)

        if response.status_code == 404:
            logger.warning(f"⚠️  404 Not Found: {full_url} ({duration}ms)")
            return None

        response.raise_for_status()
        data = response.json()
        return data

    except httpx.HTTPStatusError as e:
        logger.error(f"❌ HTTP Error {e.response.status_code} fetching {full_url}: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected error fetching {full_url}: {e}")
        return None


async def _fetch_goals_catalog() -> List[Dict[str, Any]]:
    """GET /health-concerns/goals/ — cached for process lifetime."""
    global _goals_catalog_cache
    if _goals_catalog_cache is not None:
        return _goals_catalog_cache
    url = f"{API_BASE}/health-concerns/goals/"
    data = await fetch_json(url)
    if isinstance(data, list):
        _goals_catalog_cache = data
        return data
    return []


async def fetch_primary_goal(user_id: str) -> str:
    """Resolve primary goal title via health-concerns APIs (replaces OpenSearch)."""
    logger.info(f"🎯 Fetching primary goal for user: {user_id}")
    # Fallback when API errors, no goals row, or unknown goal UUID (aligned with former OpenSearch default).
    default_goal = "Weight Loss"
    try:
        user_url = f"{API_BASE}/health-concerns/foodhak-user/{user_id}/goals"
        user_data = await fetch_json(user_url)
        if not user_data or not isinstance(user_data.get("results"), list):
            return default_goal

        rows = user_data["results"]
        if not rows:
            return default_goal

        primary_row = next((r for r in rows if r.get("is_primary")), None)
        if not primary_row:
            primary_row = rows[0]

        goal_uuid = primary_row.get("goal")
        if not goal_uuid:
            return default_goal

        catalog = await _fetch_goals_catalog()
        for g in catalog:
            if g.get("id") == goal_uuid:
                title = g.get("title")
                if title:
                    return str(title).strip() or default_goal
        return default_goal
    except Exception as e:
        logger.error(f"❌ Error fetching primary goal: {e}, defaulting to '{default_goal}'")
        return default_goal


async def fetch_meals(user_id, start_date_utc, end_date_utc):
    url = f"{API_BASE}/meal-planner/custom-meals/foodhak-user-id/{user_id}"
    return await fetch_json(url, params={"start_date": start_date_utc, "end_date": end_date_utc})


async def fetch_daily_target(user_id):
    url = f"{API_BASE}/user-healthprofile-group-details/{user_id}/nutrient-guidelines"
    data = await fetch_json(url)
    if not data or "results" not in data:
        return 2000
    energy = data["results"].get("Energy", [])
    if energy:
        return safe_int(energy[0].get("target_value", 2000))
    return 2000


async def fetch_chats(user_id, start_date_utc, end_date_utc):
    url = f"{API_BASE}/chathistory/latest-sessions/"
    resp = await fetch_json(url, params={"user_id": user_id, "limit": 100})
    if not resp: return []

    valid_chats = []

    # Parse timestamps for comparison
    s_dt = datetime.fromisoformat(start_date_utc.replace("Z", "+00:00"))
    e_dt = datetime.fromisoformat(end_date_utc.replace("Z", "+00:00"))

    for session in resp:
        ts = session.get("timestamp")
        if not ts: continue
        try:
            sess_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if s_dt <= sess_dt < e_dt:
                history = json.loads(session.get("chat_history", "[]"))
                for msg in history:
                    if msg.get("role") == "user":
                        content = msg.get("content", "").strip()
                        if content: valid_chats.append(content)
        except:
            continue

    return valid_chats


async def fetch_trackers(user_id, start_date_utc, end_date_utc):
    base_url = f"{API_BASE}/user-profile/{user_id}/tracker"
    types = ["WEIGHT", "STEPS", "SLEEP", "MOOD_ENTRY"]
    data = {}

    for t in types:
        query_string = f"type={t}&start_datetime={start_date_utc}&end_datetime={end_date_utc}"
        params = {"query": query_string}
        resp = await fetch_json(base_url, params=params)
        data[t] = resp.get("results", resp.get("data", [])) if resp else []
    return data


async def fetch_scans(user_id, start_date_utc, end_date_utc):
    url = f"{API_BASE}/scans/by-user/"
    params = {"foodhak_user_id": user_id, "startDate": start_date_utc, "endDate": end_date_utc}
    resp = await fetch_json(url, params=params)
    if isinstance(resp, list): return resp
    if isinstance(resp, dict): return resp.get("results", [])
    return []


# ==========================================
# 3. HELPER: CLEAN LLM JSON
# ==========================================
def extract_json_from_llm_response(text):
    try:
        pattern = r"```json\s*(\{.*?\})\s*```"
        match = re.search(pattern, text, re.DOTALL)
        if match: return match.group(1)
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end != -1: return text[start:end]
        return text
    except:
        return text


# ==========================================
# 4. GOAL-AWARE LOGIC
# ==========================================

def is_positive_change(delta, metric_type, goal):
    goal_lower = goal.lower()
    is_loss_goal = "loss" in goal_lower or "lose" in goal_lower
    is_gain_goal = "gain" in goal_lower
    is_maintain_goal = "maintain" in goal_lower or "maintenance" in goal_lower

    if delta == 0:
        return True if is_maintain_goal else False

    if is_loss_goal:
        return delta < 0
    elif is_gain_goal:
        return delta > 0
    elif is_maintain_goal:
        return False
    else:
        return delta == 0


# ==========================================
# 5. SECTION PROCESSORS (ASYNC)
# ==========================================

async def build_title_card(user_id, start_date_utc, end_date_utc, goal, start_date_local, end_date_local, user_tz):
    logger.info("Building Title Card...")

    start_dt = datetime.strptime(start_date_local, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_local, "%Y-%m-%d")

    if start_dt.month == end_dt.month:
        date_range = f"{start_dt.day}-{end_dt.day} {start_dt.strftime('%b')} {start_dt.year}"
    else:
        date_range = f"{start_dt.day} {start_dt.strftime('%b')} - {end_dt.day} {end_dt.strftime('%b')} {start_dt.year}"

    # Check connection status
    is_connected, provider_type = await connection_status(user_id)

    # Fetch data in parallel
    meals_data, manual_steps_data, manual_sleep_data = await asyncio.gather(
        fetch_meals(user_id, start_date_utc, end_date_utc),
        _fetch_tracker(user_id, "steps", start_date_utc, end_date_utc),
        _fetch_tracker(user_id, "sleep", start_date_utc, end_date_utc)
    )

    # Extract meal dates (convert UTC to local)
    meal_dates = set()
    if meals_data and "results" in meals_data:
        for item in meals_data["results"]:
            ts = item.get("timestamp")
            if ts:
                try:
                    utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    local_dt = utc_dt.astimezone(ZoneInfo(user_tz))
                    meal_dates.add(local_dt.strftime("%Y-%m-%d"))
                except:
                    continue

    # Extract manual steps dates
    manual_steps_dates = set()
    if manual_steps_data.get("status") == "ok":
        for item in manual_steps_data.get("data", []):
            date_str = item.get("date")
            if date_str:
                manual_steps_dates.add(date_str)

    # Extract manual sleep dates (convert UTC to local)
    manual_sleep_dates = set()
    if manual_sleep_data.get("status") == "ok":
        for item in manual_sleep_data.get("data", []):
            ts = item.get("timestamp")
            if ts:
                try:
                    utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    local_dt = utc_dt.astimezone(ZoneInfo(user_tz))
                    manual_sleep_dates.add(local_dt.strftime("%Y-%m-%d"))
                except:
                    continue
    # Build day-by-day activity map
    days_map = {}
    curr = start_dt
    while curr <= end_dt:
        day_key = curr.strftime("%Y-%m-%d")
        day_name = curr.strftime("%A").lower()

        # Determine if day is active based on connection status
        if is_connected:
            # Connected: meal
            days_map[day_name] = day_key in meal_dates
        else:
            # Disconnected: meal AND manual_steps AND manual_sleep (all 3 required)
            has_all_three = (
                    day_key in meal_dates and
                    day_key in manual_steps_dates and
                    day_key in manual_sleep_dates
            )

            days_map[day_name] = has_all_three

        curr += timedelta(days=1)

    # Count active days (true values)
    active_days_count = sum(1 for v in days_map.values() if v)

    logger.info(
        f"📅 Active Days (connection={'connected' if is_connected else 'disconnected'}): "
        f"{active_days_count} out of 7 days active"
    )

    return {
        "title": "Weekly Summary",
        "date_range": date_range,
        "primary_weight_goal": goal,
        "sun_sat_overview": {
            "description": "True indicates user activity logged",
            "days": days_map
        }
    }


async def build_nutrition(user_id, start_date_utc, end_date_utc, primary_goal, start_date_local, end_date_local,
                          user_tz):
    logger.info(f"Building Nutrition Section (Goal: {primary_goal})")

    # Calculate previous week dates in local timezone
    dt_curr = datetime.strptime(start_date_local, "%Y-%m-%d")
    prev_start_local = (dt_curr - timedelta(days=7)).strftime("%Y-%m-%d")
    prev_end_local = (dt_curr - timedelta(days=1)).strftime("%Y-%m-%d")

    # Convert previous week to UTC
    prev_start_utc, _ = convert_local_date_to_utc_window(prev_start_local, user_tz)
    _, prev_end_utc = convert_local_date_to_utc_window(prev_end_local, user_tz)

    logger.info("⚡ Parallel Fetch: Current Meals, Previous Meals, Target")
    curr_data, prev_data, target = await asyncio.gather(
        fetch_meals(user_id, start_date_utc, end_date_utc),
        fetch_meals(user_id, prev_start_utc, prev_end_utc),
        fetch_daily_target(user_id)
    )

    # -----------------------------------------------------
    # Analyze Function now converts UTC -> Local
    # -----------------------------------------------------
    def analyze(data, range_start_str, range_end_str, timezone_str, label=""):
        daily_map = defaultdict(lambda: {"p": 0.0, "c": 0.0, "f": 0.0, "k": 0.0})

        if data and "results" in data:
            for item in data["results"]:
                ts = item.get("timestamp")  # UTC Timestamp from DB
                if ts:
                    try:
                        # 1. Parse UTC
                        utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        # 2. Convert to User TZ
                        local_dt = utc_dt.astimezone(ZoneInfo(timezone_str))
                        # 3. Bucket into Local Day
                        date_key = local_dt.strftime("%Y-%m-%d")
                        daily_map[date_key]["p"] += safe_float(item.get("protein"))
                        daily_map[date_key]["c"] += safe_float(item.get("carbohydrates"))
                        daily_map[date_key]["f"] += safe_float(item.get("fat"))
                        daily_map[date_key]["k"] += safe_float(item.get("calories"))
                    except Exception as e:
                        logger.warning(f"Date conversion error in meal: {e}")
                        continue

        totals = {"p": 0, "c": 0, "f": 0, "k": 0}
        logged_count = len(daily_map)
        daily_stats = []

        curr_d = datetime.strptime(range_start_str, "%Y-%m-%d")
        end_d = datetime.strptime(range_end_str, "%Y-%m-%d")

        while curr_d <= end_d:
            d_str = curr_d.strftime("%Y-%m-%d")
            day_name = curr_d.strftime("%A")

            if d_str in daily_map:
                stats = daily_map[d_str]
                eaten = safe_int(stats["k"])

                totals["p"] += stats["p"];
                totals["c"] += stats["c"]
                totals["f"] += stats["f"];
                totals["k"] += stats["k"]

                exact_protein_g = stats["p"]
                exact_carbs_g = stats["c"]
                exact_fats_g = stats["f"]
                total_grams = exact_protein_g + exact_carbs_g + exact_fats_g

                if total_grams > 0:
                    protein_pct = round((exact_protein_g / total_grams) * 100, 1)
                    carbs_pct = round((exact_carbs_g / total_grams) * 100, 1)
                    fats_pct = round(100.0 - protein_pct - carbs_pct, 1)
                else:
                    protein_pct = carbs_pct = fats_pct = 0.0

                daily_stats.append({
                    "day": day_name,
                    "is_logged": True,
                    "total_eaten_kcal": eaten,
                    "total_recc_kcal": target,
                    "fulfillment_percentage": round((eaten / target) * 100, 1),
                    "macros": {
                        "protein": {"grams": round(exact_protein_g, 1), "pct_of_total_eaten": protein_pct},
                        "carbs": {"grams": round(exact_carbs_g, 1), "pct_of_total_eaten": carbs_pct},
                        "fats": {"grams": round(exact_fats_g, 1), "pct_of_total_eaten": fats_pct}
                    }
                })
            else:
                daily_stats.append({
                    "day": day_name,
                    "is_logged": False,
                    "total_recc_kcal": target,
                    "total_eaten_kcal": 0,
                    "fulfillment_percentage": 0
                })

            curr_d += timedelta(days=1)

        if logged_count > 0:
            avgs = {k: safe_int(v / logged_count) for k, v in totals.items()}
        else:
            avgs = {"p": 0, "c": 0, "f": 0, "k": 0}

        logger.debug(f"ℹ️ {label} Analysis: {logged_count} days logged out of {len(daily_stats)} total days")
        return {"avgs": avgs, "days": daily_stats, "count": logged_count}

    # Pass user_tz to the analyzer
    curr_res = analyze(curr_data, start_date_local, end_date_local, user_tz, "CURRENT WEEK")
    prev_res = analyze(prev_data, prev_start_local, prev_end_local, user_tz, "PREVIOUS WEEK")

    if not curr_res:
        logger.warning("⚠️ Unexpected error in nutrition analysis")
        return None

    prev_day_count = prev_res["count"] if prev_res else 0
    show_deltas = prev_day_count >= 1
    avg_curr = curr_res["avgs"]

    if show_deltas:
        avg_prev = prev_res["avgs"]
        deltas = {
            "p": avg_curr["p"] - avg_prev["p"],
            "c": avg_curr["c"] - avg_prev["c"],
            "f": avg_curr["f"] - avg_prev["f"],
            "k": avg_curr["k"] - avg_prev["k"]
        }
        kcal_positive = is_positive_change(deltas["k"], 'calories', primary_goal)
        protein_positive = is_positive_change(deltas["p"], 'macro', primary_goal)
        carbs_positive = is_positive_change(deltas["c"], 'macro', primary_goal)
        fats_positive = is_positive_change(deltas["f"], 'macro', primary_goal)

    protein_kcal = avg_curr["p"] * 4
    carbs_kcal = avg_curr["c"] * 4
    fat_kcal = avg_curr["f"] * 9
    total_macro_kcal = protein_kcal + carbs_kcal + fat_kcal

    if total_macro_kcal > 0:
        pct_p = round((protein_kcal / total_macro_kcal) * 100)
        pct_c = round((carbs_kcal / total_macro_kcal) * 100)
        pct_f = round((fat_kcal / total_macro_kcal) * 100)
    else:
        pct_p = pct_c = pct_f = 0

    goal_lower = primary_goal.lower()
    is_loss_goal = "loss" in goal_lower or "lose" in goal_lower
    is_gain_goal = "gain" in goal_lower

    if is_loss_goal:
        protein_threshold = 30;
        carbs_threshold = 50;
        fat_threshold = 35
        balanced_ranges = {"p": (25, 30), "c": (40, 50), "f": (20, 30)}
    elif is_gain_goal:
        protein_threshold = 25;
        carbs_threshold = 60;
        fat_threshold = 35
        balanced_ranges = {"p": (20, 25), "c": (50, 60), "f": (25, 35)}
    else:  # Maintenance
        protein_threshold = 25;
        carbs_threshold = 55;
        fat_threshold = 35
        balanced_ranges = {"p": (20, 25), "c": (45, 55), "f": (20, 30)}

    persona = None
    persona_title = "Balanced"

    if pct_f >= fat_threshold:
        persona_title = "Fat-heavy week"
        persona = {
            "title": persona_title,
            "description": "ate more fat than the healthy upper limit",
            "visual_data": {"protein_pct": pct_p, "carbs_pct": pct_c, "fats_pct": pct_f},
            "logic_rule_applied": f"Fat >= {fat_threshold}%"
        }
    elif pct_c > carbs_threshold:
        persona_title = "Carb-leaning week"
        persona = {
            "title": persona_title,
            "description": "ate more carbs than their goal recommends",
            "visual_data": {"protein_pct": pct_p, "carbs_pct": pct_c, "fats_pct": pct_f},
            "logic_rule_applied": f"Carbs > {carbs_threshold}%"
        }
    elif pct_p >= protein_threshold:
        persona_title = "Protein-forward week"
        persona = {
            "title": persona_title,
            "description": "ate more protein than their goal recommends",
            "visual_data": {"protein_pct": pct_p, "carbs_pct": pct_c, "fats_pct": pct_f},
            "logic_rule_applied": f"Protein >= {protein_threshold}%"
        }
    else:
        persona_title = "Balanced week"
        p_min, p_max = balanced_ranges["p"]
        c_min, c_max = balanced_ranges["c"]
        f_min, f_max = balanced_ranges["f"]
        persona = {
            "title": persona_title,
            "description": "ate within the ideal macro ranges",
            "visual_data": {"protein_pct": pct_p, "carbs_pct": pct_c, "fats_pct": pct_f},
            "logic_rule_applied": f"P {p_min}-{p_max}% | C {c_min}-{c_max}% | F {f_min}-{f_max}%"
        }

    response = {
        "summary": {
            "avg_daily_kcal": avg_curr["k"],
            "total_recc_kcal_daily": target
        },
        "macro_averages_7d": {
            "protein": {"avg_grams": avg_curr["p"]},
            "carbs": {"avg_grams": avg_curr["c"]},
            "fats": {"avg_grams": avg_curr["f"]}
        },
        "daily_breakdown": curr_res["days"],
        "macro_persona": persona
    }

    if show_deltas:
        response["summary"].update({
            "kcal_delta_7d": deltas["k"],
            "kcal_positive_change": kcal_positive
        })
        response["macro_averages_7d"]["protein"].update(
            {"delta_grams": deltas["p"], "positive_change": protein_positive})
        response["macro_averages_7d"]["carbs"].update({"delta_grams": deltas["c"], "positive_change": carbs_positive})
        response["macro_averages_7d"]["fats"].update({"delta_grams": deltas["f"], "positive_change": fats_positive})

    return response


async def build_faye(user_id, start_date_utc, end_date_utc):
    logger.info("🏗️ Building Faye Insights...")
    queries = await fetch_chats(user_id, start_date_utc, end_date_utc)
    if not queries:
        return None

    client = AsyncAnthropic(api_key=CLAUDE_API_KEY)
    prompt = f"""
    Process user queries into weekly insights.
    INPUT: {json.dumps(queries)}
    TASKS:
    1. Filter out greetings.
    2. Classify RELEVANT queries into: Recipes and Meal Ideas, Ingredients and Food Facts, Nutrition Guidance, Habits and Lifestyle.
    3. Calculate %.
    4. Generate JSON.
    REQUIRED JSON FORMAT:
    {{
        "summary_card": {{
            "insight_text": "String (ex: 40% of your chats with Faye were about Recipes and Meal Ideas)",
            "top_category": "String",
            "top_category_percentage": Integer
        }},
        "category_breakdown": [
            {{ "category_name": "String", "percentage": Integer }}
        ]
    }}
    Return ONLY JSON. No markdowns or extra strings.
    """
    try:
        logger.info("🤖 Calling Claude API...")
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024, temperature=0, messages=[{"role": "user", "content": prompt}]
        )
        result = json.loads(extract_json_from_llm_response(msg.content[0].text))
        return result
    except Exception as e:
        logger.error(f"❌ Error generating Faye insights: {e}")
        return None


def bucket_tracker_by_local_day(records: List[dict], user_tz: str, value_key: str, deduplicate: bool = False):
    """
    Buckets tracker records by local YYYY-MM-DD.
    If deduplicate=True, keeps only the latest entry per day (for sleep/weight).
    """
    if deduplicate:
        # For sleep/weight: keep only the latest timestamp per day
        day_entries = defaultdict(list)

        for r in records or []:
            ts = r.get("timestamp") or r.get("date")
            if not ts:
                continue

            try:
                utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local_dt = utc_dt.astimezone(ZoneInfo(user_tz))
                day_key = local_dt.strftime("%Y-%m-%d")
                day_entries[day_key].append((utc_dt, safe_float(r.get(value_key, 0))))
            except Exception:
                continue

        # Keep only the latest entry per day
        buckets = {}
        for day_key, entries in day_entries.items():
            # Sort by timestamp and take the last one
            latest_entry = sorted(entries, key=lambda x: x[0])[-1]
            buckets[day_key] = latest_entry[1]

        return buckets
    else:
        # For steps: sum all values per day
        buckets = defaultdict(float)

        for r in records or []:
            ts = r.get("timestamp") or r.get("date")
            if not ts:
                continue

            try:
                utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local_dt = utc_dt.astimezone(ZoneInfo(user_tz))
                day_key = local_dt.strftime("%Y-%m-%d")
                buckets[day_key] += safe_float(r.get(value_key, 0))
            except Exception:
                continue

        return buckets


def bucket_provider_steps(records, user_tz):
    buckets = defaultdict(int)
    tz = ZoneInfo(user_tz)

    for r in records:
        meta = r.get("data", {}).get("metadata", {})
        start = meta.get("start_time")
        dt = parse_dt_safe(start)
        if not dt:
            continue

        local_day = dt.astimezone(tz).strftime("%Y-%m-%d")
        steps = r.get("data", {}).get("distance_data", {}).get("steps", 0)
        buckets[local_day] += int(steps)

    return buckets


def bucket_provider_sleep(records, user_tz: str):
    """
    Sleep belongs to the 'night of' the start day (bedtime day).
    Example: 2026-01-02 23:37 -> 2026-01-03 07:18 counts for Jan 2.
    """
    buckets = defaultdict(float)
    tz = ZoneInfo(user_tz)

    for r in records or []:
        meta = r.get("data", {}).get("metadata", {})
        if meta.get("is_nap"):
            continue

        start = meta.get("start_time")
        dt = parse_dt_safe(start)
        if not dt:
            continue

        local_day = dt.astimezone(tz).strftime("%Y-%m-%d")

        total_minutes = sum(
            safe_float(s.get("total_duration", 0))
            for s in r.get("data", {}).get("stages", [])
        )

        buckets[local_day] += float(total_minutes)

    return buckets


async def fetch_weight_direct(user_id: str) -> dict:
    """
    Fetches weight from the specific /trackers/foodhak-user/ endpoint.
    """
    url = f"{API_BASE}/trackers/foodhak-user/{user_id}/weight"
    logger.info(f"⚖️ Fetching weight direct from: {url}")
    try:
        resp = await _safe_get(url, headers=HEADERS)
        if resp.status_code == 200:
            return {"status": "ok", "data": resp.json()}
        logger.warning(f"⚠️ Weight fetch failed: {resp.status_code}")
        return {"status": "error", "message": f"HTTP {resp.status_code}", "data": {}}
    except Exception as e:
        logger.error(f"❌ Error fetching weight direct: {e}")
        return {"status": "error", "message": str(e), "data": {}}


async def build_wellness(
        user_id: str,
        start_date_utc: str,
        end_date_utc: str,
        primary_goal: str,
        start_date_local: str,
        end_date_local: str,
        user_tz: str,
):
    """
    Robust weekly wellness:
    - Fetch provider data (best-effort) for the whole week.
    - Fetch Foodhak fallback for the whole week.
    - Merge PER DAY (provider wins if it has data; otherwise fallback).
    - Aggregate totals + best day + avg sleep.
    """

    logger.info("🏗️ Building Wellness Section (Robust Weekly Merge: Provider + Foodhak)...")

    start_dt = datetime.strptime(start_date_local, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_local, "%Y-%m-%d")
    tz = ZoneInfo(user_tz)

    # ---------------------------
    # Initialize aggregates
    # ---------------------------
    total_steps = 0
    best_day_steps = 0
    best_day_label = "-"
    total_sleep_minutes = 0.0
    valid_sleep_days = 0

    # ---------------------------
    # Fetch non-steps/sleep data
    # ---------------------------
    scans, mood_data, weight_data = await asyncio.gather(
        fetch_scans(user_id, start_date_utc, end_date_utc),
        _fetch_tracker(user_id, "mood", start_date_utc, end_date_utc),
        fetch_weight_direct(user_id),
    )

    # ---------------------------
    # Provider (best-effort) + Fallback (always)
    # ---------------------------
    # Note: your current connection_status returns provider_type only if connected.
    # For full robustness, ideally update connection_status() to return device_type even if disconnected.
    _, provider_type = await connection_status(user_id)

    provider_steps_by_day = defaultdict(int)
    provider_sleep_by_day = defaultdict(float)
    provider_steps_present = defaultdict(bool)
    provider_sleep_present = defaultdict(bool)

    fallback_steps_by_day = defaultdict(int)
    fallback_sleep_by_day = defaultdict(float)
    fallback_steps_present = defaultdict(bool)
    fallback_sleep_present = defaultdict(bool)

    # -------- Provider weekly fetch (best-effort) --------
    if provider_type:
        res = await wearable_api(user_id, start_date_utc, end_date_utc, provider_type)
        if res.get("status") == "ok":
            provider_records = res.get("data", {}).get("data", []) or []

            steps_records = [r for r in provider_records if r.get("schema_type") == "daily"]
            sleep_records = [r for r in provider_records if r.get("schema_type") == "sleep"]

            # -------- Provider steps: bucket by local day (start_time) --------
            for r in steps_records:
                meta = (r.get("data", {}) or {}).get("metadata", {}) or {}
                start = meta.get("start_time")
                dt = parse_dt_safe(start)
                if not dt:
                    continue

                day_key = dt.astimezone(tz).strftime("%Y-%m-%d")
                provider_steps_present[day_key] = True

                steps = (r.get("data", {}) or {}).get("distance_data", {}) or {}
                provider_steps_by_day[day_key] += int(steps.get("steps", 0))

            # -------- Provider sleep: bucket by "night of" start_time --------
            # (You requested: night of Jan 2 => bucket by start_time local day)
            for r in sleep_records:
                meta = (r.get("data", {}) or {}).get("metadata", {}) or {}
                if meta.get("is_nap"):
                    continue

                anchor = meta.get("start_time")  # <- IMPORTANT: start_time for "night of"
                dt = parse_dt_safe(anchor)
                if not dt:
                    continue

                day_key = dt.astimezone(tz).strftime("%Y-%m-%d")
                provider_sleep_present[day_key] = True

                stages = (r.get("data", {}) or {}).get("stages", []) or []
                total_minutes = sum(
                    safe_float(s.get("total_duration", 0)) for s in stages
                )
                provider_sleep_by_day[day_key] += float(total_minutes)

    # =========================================================
    # CHANGE 4: Always fetch fallback and bucket + presence
    # =========================================================
    fb = await foodhak_sleep_steps_fallback(user_id, start_date_utc, end_date_utc)
    fb_steps_records = fb.get("steps", []) or []
    fb_sleep_records = fb.get("sleep", []) or []

    # -------- Fallback steps: "date" is already the day key --------
    for r in fb_steps_records:
        d = r.get("date")
        if not d:
            continue
        fallback_steps_present[d] = True
        fallback_steps_by_day[d] += int(r.get("total_steps", 0))

    # -------- Fallback sleep: bucket by local day of timestamp --------
    fallback_sleep_timestamps = {}

    for r in fb_sleep_records:
        ts = r.get("timestamp")
        dt = parse_dt_safe(ts)
        if not dt:
            continue
        day_key = dt.astimezone(tz).strftime("%Y-%m-%d")

        # Keep only the latest entry per day
        if day_key not in fallback_sleep_by_day or dt > fallback_sleep_timestamps.get(day_key, datetime.min.replace(
                tzinfo=timezone.utc)):
            fallback_sleep_present[day_key] = True
            fallback_sleep_by_day[day_key] = float(r.get("actual_value", 0))
            fallback_sleep_timestamps[day_key] = dt

    # =========================================================
    # CHANGE 5: Merge PER DAY based on PRESENCE (not >0)
    # =========================================================
    steps_by_day = defaultdict(int)
    sleep_by_day = defaultdict(float)
    steps_source_by_day: Dict[str, str] = {}
    sleep_source_by_day: Dict[str, str] = {}

    curr = start_dt
    while curr <= end_dt:
        day_key = curr.strftime("%Y-%m-%d")

        # Steps: provider if record present, else fallback if present
        if provider_steps_present.get(day_key):
            steps_by_day[day_key] = int(provider_steps_by_day.get(day_key, 0))
            steps_source_by_day[day_key] = "provider"
        elif fallback_steps_present.get(day_key):
            steps_by_day[day_key] = int(fallback_steps_by_day.get(day_key, 0))
            steps_source_by_day[day_key] = "foodhak"
        else:
            steps_source_by_day[day_key] = "none"

        # precedance change
        # if fallback_steps_present.get(day_key):
        #     steps_by_day[day_key] = fallback_steps_by_day.get(day_key, 0)
        #     steps_source_by_day[day_key] = "foodhak"
        # elif provider_steps_present.get(day_key):
        #     steps_by_day[day_key] = provider_steps_by_day.get(day_key, 0)
        #     steps_source_by_day[day_key] = "provider"

        # Sleep: provider if record present, else fallback if present
        if provider_sleep_present.get(day_key):
            sleep_by_day[day_key] = float(provider_sleep_by_day.get(day_key, 0))
            sleep_source_by_day[day_key] = "provider"
        elif fallback_sleep_present.get(day_key):
            sleep_by_day[day_key] = float(fallback_sleep_by_day.get(day_key, 0))
            sleep_source_by_day[day_key] = "foodhak"
        else:
            sleep_source_by_day[day_key] = "none"

        # precedance change
        # if fallback_sleep_present.get(day_key):
        #     sleep_by_day[day_key] = fallback_sleep_by_day.get(day_key, 0)
        #     sleep_source_by_day[day_key] = "foodhak"
        # elif provider_sleep_present.get(day_key):
        #     sleep_by_day[day_key] = provider_sleep_by_day.get(day_key, 0)
        #     sleep_source_by_day[day_key] = "provider"

        curr += timedelta(days=1)

    # =========================================================
    # CHANGE 6 (optional): Better logs using presence flags
    # =========================================================
    provider_steps_days = len([d for d, p in provider_steps_present.items() if p])
    fallback_steps_days = len([d for d, p in fallback_steps_present.items() if p])
    provider_sleep_days = len([d for d, p in provider_sleep_present.items() if p])
    fallback_sleep_days = len([d for d, p in fallback_sleep_present.items() if p])
    resolved_steps_days = len(
        [d for d, v in steps_by_day.items() if v is not None and steps_source_by_day.get(d) != "none"])
    resolved_sleep_days = len(
        [d for d, v in sleep_by_day.items() if v is not None and sleep_source_by_day.get(d) != "none"])

    logger.info(
        f"🩺 Wellness merge | provider_type={provider_type} | "
        f"provider_steps_days={provider_steps_days} | fallback_steps_days={fallback_steps_days} | resolved_steps_days={resolved_steps_days} | "
        f"provider_sleep_days={provider_sleep_days} | fallback_sleep_days={fallback_sleep_days} | resolved_sleep_days={resolved_sleep_days}"
    )

    # =========================================================
    # Existing aggregation logic (keep as-is)
    # =========================================================
    curr = start_dt
    while curr <= end_dt:
        day_key = curr.strftime("%Y-%m-%d")

        d_steps = int(steps_by_day.get(day_key, 0))
        d_sleep = float(sleep_by_day.get(day_key, 0))

        total_steps += d_steps
        if d_steps > best_day_steps:
            best_day_steps = d_steps
            best_day_label = curr.strftime("%A")

        if d_sleep > 0:
            total_sleep_minutes += d_sleep
            valid_sleep_days += 1

        curr += timedelta(days=1)

    wellness: Dict[str, Any] = {}

    if total_steps > 0:
        wellness["total_steps"] = {
            "total_weekly_steps": total_steps,
            "best_single_day": {
                "step_count": best_day_steps,
                "day_label": best_day_label,
            },
        }

    if valid_sleep_days > 0:
        avg_sleep = total_sleep_minutes / valid_sleep_days
        wellness["sleep"] = {
            "avg_total_sleep_daily": f"{int(avg_sleep // 60)}h {int(avg_sleep % 60)}m"
        }
    # ---------------- Mood ----------------
    if mood_data.get("status") == "ok":
        mood_records = mood_data.get("data") or []
        
        # Extract mood values with timestamps for tie-breaking when 3+ moods
        mood_entries = []
        for m in mood_records:
            if m.get("actual_value"):
                ts = m.get("timestamp") or m.get("date")
                mood_entries.append({
                    "mood": m.get("actual_value"),
                    "timestamp": ts
                })
        
        if not mood_entries:
            logger.debug("ℹ️ No mood data logged")
        else:
            # Count occurrences
            vals = [entry["mood"] for entry in mood_entries]
            unique_moods = len(set(vals))
            
            # Show mood mix if there's at least 1 mood entry
            if unique_moods >= 1:
                counts = Counter(vals).most_common(3)
                
                # Handle ties: random for 2 moods, chronological for 3+ moods
                if len(counts) >= 2:
                    # Group by count
                    count_groups = defaultdict(list)
                    for mood_name, count in counts:
                        count_groups[count].append(mood_name)
                    
                    # Sort each group
                    sorted_counts = []
                    for count in sorted(count_groups.keys(), reverse=True):
                        moods_with_this_count = count_groups[count]
                        
                        # Only sort if there's a tie (2+ moods with same count)
                        if len(moods_with_this_count) >= 2:
                            if len(counts) == 2 and len(moods_with_this_count) == 2:
                                # CASE 1: Exactly 2 moods tied → Random
                                random.shuffle(moods_with_this_count)
                                logger.info(f"🎲 Mood Mix: 2-way tie, using random selection")
                            else:
                                # CASE 2: 3+ moods (or partial tie) → Chronological with fallback
                                # Try to parse timestamps for chronological ordering
                                first_occurrence = {}
                                timestamp_parse_failed = False
                                
                                for entry in mood_entries:
                                    mood_name = entry["mood"]
                                    if mood_name in moods_with_this_count and mood_name not in first_occurrence:
                                        try:
                                            ts = entry["timestamp"]
                                            dt = parse_dt_safe(ts)
                                            if dt:
                                                first_occurrence[mood_name] = dt
                                            else:
                                                timestamp_parse_failed = True
                                                break
                                        except:
                                            timestamp_parse_failed = True
                                            break
                                
                                # If all timestamps parsed successfully, use chronological
                                if not timestamp_parse_failed and len(first_occurrence) == len(moods_with_this_count):
                                    moods_with_this_count.sort(key=lambda m: first_occurrence.get(m, datetime.max))
                                    logger.info(f"⏰ Mood Mix: {len(moods_with_this_count)}-way tie, using chronological order")
                                else:
                                    # Fallback: Random if timestamp parsing failed
                                    random.shuffle(moods_with_this_count)
                                    logger.info(f"🎲 Mood Mix: {len(moods_with_this_count)}-way tie, timestamp error - using random selection")
                        
                        for mood in moods_with_this_count:
                            sorted_counts.append((mood, count))
                    
                    counts = sorted_counts[:3]  # Keep top 3
                
                clusters = []
                sizes = ["Large", "Medium", "Small"]
                emojis = {
                    "HAPPY": "😁",
                    "SAD": "😔",
                    "ENERGETIC": "😎",
                    "FRISKY": "🤪",
                    "MOOD SWINGS": "🫠",
                    "IRRITATED": "🙄",
                    "ANXIOUS": "😰",
                    "DEPRESSED": "😞",
                    "LOW ENERGY": "🤕",
                    "CONFUSED": "😵‍💫",
                    "APATHETIC": "😐",
                    "CUSTOM": "😶"
                }
                
                for i, (mood_name, count) in enumerate(counts):
                    clusters.append({
                        "mood_name": mood_name,
                        "emoji": emojis.get(mood_name, "😐"),
                        "count": count,
                        "visual_size": sizes[i] if i < 3 else "Small"
                    })
                
                wellness["mood_mix"] = {"status": "active", "clusters": clusters}
                logger.info(f"😊 Mood Mix created: {len(clusters)} clusters (unique_moods={unique_moods})")
            else:
                logger.debug(f"ℹ️ No mood data logged")
    else:
        logger.debug("ℹ️ No mood data found")

    # ---------------- Scans ----------------
    if scans:
        processed = []
        for s in scans:
            name = s.get("name", "Unknown Item")
            try:
                score = float(
                    s.get("foodhak_score", {}).get("Score", 0) if isinstance(s.get("foodhak_score"), dict) else s.get(
                        "foodhak_score", 0))
                score = round(score, 1)
            except:
                score = 0.0
            image_url = s.get("image_url") or s.get("image") or s.get("product_image") or s.get("img_url") or None
            scan_item = {"product_name": name, "score": score}
            if image_url:
                scan_item["image_url"] = image_url
            processed.append(scan_item)
        processed.sort(key=lambda x: x["score"], reverse=True)
        wellness["top_scan"] = {
            "best_scan_summary": processed[0],
            "scan_history_sorted_desc": processed[:5]
        }
        logger.info(f"📸 Scans processed: {len(processed)} items")
    else:
        logger.debug("ℹ️ No scans found")

    # ---------------- Weight ----------------
    def get_latest_weight_in_range(data_obj, start_date_local, end_date_local, user_tz):
        """Get the most recent weight entry WITHIN a specific date range"""
        if data_obj.get("status") != "ok" or "results" not in data_obj.get("data", {}):
            return None
        try:
            results = data_obj["data"]["results"]
            if not results:
                return None

            # Convert date range to UTC
            start_utc, _ = convert_local_date_to_utc_window(start_date_local, user_tz)
            _, end_utc = convert_local_date_to_utc_window(end_date_local, user_tz)

            start_dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))

            # Filter entries WITHIN the date range
            valid_entries = []
            for entry in results:
                ts = entry.get("timestamp")
                if ts:
                    try:
                        entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if start_dt <= entry_dt < end_dt:
                            valid_entries.append((entry_dt, float(entry.get("actual_value"))))
                    except:
                        continue

            if valid_entries:
                # Get the most recent entry in this range
                latest = sorted(valid_entries, key=lambda x: x[0], reverse=True)[0]
                return latest[1]
            return None
        except Exception as e:
            logger.error(f"Error processing weight data: {e}")
            return None

    # Calculate previous week dates (7 days before start_date)
    start_dt = datetime.strptime(start_date_local, "%Y-%m-%d")
    prev_week_start_local = (start_dt - timedelta(days=7)).strftime("%Y-%m-%d")
    prev_week_end_local = (start_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    # Get latest weight WITHIN CURRENT WEEK (Jan 4-10)
    weight_current_week = get_latest_weight_in_range(weight_data, start_date_local, end_date_local, user_tz)

    # Get latest weight WITHIN PREVIOUS WEEK (Dec 28 - Jan 3)
    weight_prev_week = get_latest_weight_in_range(weight_data, prev_week_start_local, prev_week_end_local, user_tz)

    # Only add weight section if we have AT LEAST 2 weight values (one from each week)
    if weight_current_week and weight_prev_week:
        delta = round(weight_current_week - weight_prev_week, 1)
        positive = is_positive_change(delta, 'weight', primary_goal)

        if delta > 0:
            lbl = f"gained {delta}kg"
        elif delta < 0:
            lbl = f"lost {abs(delta)}kg"
        else:
            lbl = "no change"

        wellness["weight"] = {
            "current_value": weight_current_week,
            "unit": "kg",
            "change_7d_value": delta,
            "change_7d_label": lbl,
            "positive_change": positive
        }

        logger.info(
            f"⚖️ Weight: Current week ({start_date_local} to {end_date_local})={weight_current_week}kg, "
            f"Prev week ({prev_week_start_local} to {prev_week_end_local})={weight_prev_week}kg, "
            f"Delta={delta}kg"
        )
    else:
        logger.debug(
            f"ℹ️ Weight section omitted (need weight in both weeks): "
            f"Current week ({start_date_local}-{end_date_local})={'✅' if weight_current_week else '❌'}, "
            f"Prev week ({prev_week_start_local}-{prev_week_end_local})={'✅' if weight_prev_week else '❌'}"
        )

    return wellness


# ==========================================
# API ENDPOINTS
# ==========================================

@app.post("/weekly-reports/")
async def generate_weekly_report(
        request: WeeklyReportRequest,
        token: str = Depends(verify_token)
):
    start_time = time.time()
    logger.info("=" * 80)
    logger.info(f"🚀 NEW WEEKLY REPORT REQUEST: {request.user_id}")
    logger.info(f"📅 Local Date Range: {request.start_date} to {request.end_date}")

    try:
        try:
            datetime.strptime(request.start_date, "%Y-%m-%d")
            datetime.strptime(request.end_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

        # Step 1: Fetch timezone and goal
        user_tz, primary_goal = await asyncio.gather(
            fetch_user_timezone(request.user_id),
            fetch_primary_goal(request.user_id)
        )

        # Step 2: Convert local dates to UTC windows
        start_date_utc_begin, _ = convert_local_date_to_utc_window(request.start_date, user_tz)
        _, end_date_utc_end = convert_local_date_to_utc_window(request.end_date, user_tz)

        # Step 3: Parallel Build
        logger.info("⚡ STARTING PARALLEL SECTION BUILD...")
        title_res, nutrition_res, faye_res, wellness_res = await asyncio.gather(
            build_title_card(request.user_id, start_date_utc_begin, end_date_utc_end, primary_goal,
                             request.start_date, request.end_date, user_tz),
            build_nutrition(request.user_id, start_date_utc_begin, end_date_utc_end, primary_goal,
                            request.start_date, request.end_date, user_tz),
            build_faye(request.user_id, start_date_utc_begin, end_date_utc_end),
            build_wellness(request.user_id, start_date_utc_begin, end_date_utc_end, primary_goal,
                           request.start_date, request.end_date, user_tz)
        )

        final_report = {}
        if title_res: final_report["title_card"] = title_res
        if nutrition_res: final_report["nutrition"] = nutrition_res
        if faye_res: final_report["faye_insights"] = faye_res
        if wellness_res: final_report.update(wellness_res)

        # Step 4: Post to External
        external_api_url = "https://api.foodhak.com/weekly-reports/create/"
        payload = {
            "user": request.user_id,
            "week_start": request.start_date,  # Sending LOCAL date as requested
            "week_end": request.end_date,  # Sending LOCAL date as requested
            "report_json": final_report
        }

        logger.info(f"📤 Posting report to External API...")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                external_response = await client.post(
                    external_api_url,
                    json=payload,
                    headers=HEADERS
                )

            if external_response.status_code in [200, 201]:
                logger.info("✅ SUCCESS: Report posted to external API")
                return JSONResponse(status_code=200, content=final_report)
            else:
                logger.warning(f"⚠️ EXTERNAL API FAILURE: Status {external_response.status_code}")
                return JSONResponse(
                    status_code=200,
                    content={
                        "status": "partial_success",
                        "message": "Weekly report generated but failed to post to external API",
                        "report_json": final_report,
                        "external_api_error": external_response.text
                    }
                )
        except httpx.HTTPError as e:
            return JSONResponse(
                status_code=200,
                content={
                    "status": "partial_success",
                    "message": "Weekly report generated but failed to post to external API",
                    "report_json": final_report,
                    "external_api_error": str(e)
                }
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ CRITICAL SERVER ERROR: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.get("/")
async def root():
    return {"status": "ok", "service": "Weekly Reports API"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
