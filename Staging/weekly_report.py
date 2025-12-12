from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import json
import httpx
import asyncio
import re
import os
import random
import logging
import time
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from urllib.parse import urlencode, quote
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("API_BASE")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL")
OPENSEARCH_USER = os.getenv("OPENSEARCH_USER")
OPENSEARCH_PWD = os.getenv("OPENSEARCH_PWD")
VALID_API_KEY = os.getenv("VALID_API_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

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

OPENSEARCH_AUTH = (OPENSEARCH_USER, OPENSEARCH_PWD)
TIMEOUT_SECONDS = 10.0

# ==========================================
# REQUEST/RESPONSE MODELS
# ==========================================

class WeeklyReportRequest(BaseModel):
    user_id: str
    start_date: str  # Format: YYYY-MM-DD
    end_date: str  # Format: YYYY-MM-DD

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
        
        # Log data size for context
        item_count = len(data) if isinstance(data, list) else len(data.get("results", [])) if "results" in data else 1
        logger.debug(f"✅ Success: {full_url} ({duration}ms) | Items: {item_count}")
        return data
        
    except httpx.HTTPStatusError as e:
        logger.error(f"❌ HTTP Error {e.response.status_code} fetching {full_url}: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected error fetching {full_url}: {e}")
        return None

async def fetch_primary_goal(user_id):
    """Queries OpenSearch for the user's primary goal."""
    logger.info(f"🎯 Fetching primary goal for user: {user_id}")
    query = {"query": {"match": {"foodhak_user_id": user_id}}}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(OPENSEARCH_URL, json=query, auth=OPENSEARCH_AUTH)
            
        if response.status_code != 200:
            logger.warning(f"⚠️ OpenSearch status {response.status_code}, defaulting to 'Weight Loss'")
            return "Weight Loss"

        hits = response.json().get("hits", {}).get("hits", [])
        if not hits: 
            logger.warning("⚠️ No OpenSearch profile found, defaulting to 'Weight Loss'")
            return "Weight Loss"

        source = hits[0].get("_source", {})
        goals = source.get("user_health_goals", [])

        primary = next((g for g in goals if g.get("user_goal", {}).get("is_primary")), None)
        if primary: 
            goal = primary["user_goal"].get("title")
            logger.info(f"✅ Found primary goal: {goal}")
            return goal
        
        if goals: 
            goal = goals[0]["user_goal"].get("title")
            logger.info(f"✅ Found secondary goal (fallback): {goal}")
            return goal

        logger.warning("⚠️ User has profile but no goals, defaulting to 'Weight Loss'")
        return "Weight Loss"
    except Exception as e:
        logger.error(f"❌ Error fetching primary goal: {e}, defaulting to 'Weight Loss'")
        return "Weight Loss"

async def fetch_active_days(user_id, start_date, end_date):
    url = f"{API_BASE}/user-insight-messages/date-range"
    params = {
        "user_id": user_id,
        "startdate": f"{start_date}T00:00:00Z",
        "enddate": f"{end_date}T23:59:59Z"
    }
    resp = await fetch_json(url, params=params)
    active_dates = set()

    if not resp: return active_dates
    messages = resp if isinstance(resp, list) else resp.get("results", resp.get("data", []))

    for msg in messages:
        raw_ts = msg.get("message_date")
        if raw_ts:
            try:
                active_dates.add(raw_ts.split("T")[0])
            except:
                continue
    
    logger.info(f"📅 Active Days Found: {len(active_dates)} ({sorted(list(active_dates))})")
    return active_dates

async def fetch_meals(user_id, start_date, end_date):
    url = f"{API_BASE}/meal-planner/custom-meals/foodhak-user/{user_id}"
    return await fetch_json(url, params={"start_date": start_date, "end_date": end_date})

async def fetch_daily_target(user_id):
    url = f"{API_BASE}/user-healthprofile-group-details/{user_id}/nutrient-guidelines"
    data = await fetch_json(url)
    if not data or "results" not in data: 
        logger.debug("ℹ️ No nutrient target found, defaulting to 2000 kcal")
        return 2000
    energy = data["results"].get("Energy", [])
    if energy: 
        target = int(float(energy[0].get("target_value", 2000)))
        logger.debug(f"ℹ️ Found daily calorie target: {target} kcal")
        return target
    return 2000

async def fetch_chats(user_id, start_date, end_date):
    url = f"{API_BASE}/chathistory/latest-sessions/"
    resp = await fetch_json(url, params={"user_id": user_id, "limit": 100})
    if not resp: return []

    valid_chats = []
    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)

    for session in resp:
        ts = session.get("timestamp")
        if not ts: continue
        try:
            sess_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
            if s_dt <= sess_dt < e_dt:
                history = json.loads(session.get("chat_history", "[]"))
                for msg in history:
                    if msg.get("role") == "user":
                        content = msg.get("content", "").strip()
                        if content: valid_chats.append(content)
        except:
            continue
    
    logger.info(f"💬 Found {len(valid_chats)} chat messages in date range")
    return valid_chats

async def fetch_trackers(user_id, start_date, end_date):
    base_url = f"{API_BASE}/user-profile/{user_id}/tracker"
    types = ["WEIGHT", "STEPS", "SLEEP", "MOOD_ENTRY"]
    data = {}
    for t in types:
        inner_query = urlencode({"type": t, "start_date": start_date, "end_date": end_date})
        encoded_query = quote(inner_query, safe="")
        resp = await fetch_json(base_url, manual_query_string=encoded_query)
        data[t] = resp.get("results", resp.get("data", [])) if resp else []
    return data

async def fetch_scans(user_id, start_date, end_date):
    url = f"{API_BASE}/scans/by-user/"
    params = {"foodhak_user_id": user_id, "startDate": start_date, "endDate": end_date}
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

    if is_loss_goal: return delta < 0
    elif is_gain_goal: return delta > 0
    elif is_maintain_goal: return False
    else: return delta == 0

# ==========================================
# 5. SECTION PROCESSORS (ASYNC)
# ==========================================

async def build_title_card(user_id, start_date, end_date, goal):
    logger.info("Building Title Card...")
    active_dates = await fetch_active_days(user_id, start_date, end_date)
    
    days_map = {
        "sunday": False, "monday": False, "tuesday": False,
        "wednesday": False, "thursday": False, "friday": False, "saturday": False
    }

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    if start_dt.month == end_dt.month:
        date_range = f"{start_dt.day}-{end_dt.day} {start_dt.strftime('%b')} {start_dt.year}"
    else:
        date_range = f"{start_dt.day} {start_dt.strftime('%b')} - {end_dt.day} {end_dt.strftime('%b')} {start_dt.year}"

    curr = start_dt
    while curr <= end_dt:
        if curr.strftime("%Y-%m-%d") in active_dates:
            days_map[curr.strftime("%A").lower()] = True
        curr += timedelta(days=1)

    return {
        "title": "Your week in review",
        "date_range": date_range,
        "primary_weight_goal": goal,
        "sun_sat_overview": {
            "description": "True indicates insight generated",
            "days": days_map
        }
    }

async def build_nutrition(user_id, start_date, end_date, primary_goal):
    logger.info(f"Building Nutrition Section (Goal: {primary_goal})")
    
    dt_curr = datetime.strptime(start_date, "%Y-%m-%d")
    prev_start = (dt_curr - timedelta(days=7)).strftime("%Y-%m-%d")
    prev_end = (dt_curr - timedelta(days=1)).strftime("%Y-%m-%d")
    
    logger.info("⚡ Parallel Fetch: Current Meals, Previous Meals, Target")
    curr_data, prev_data, target = await asyncio.gather(
        fetch_meals(user_id, start_date, end_date),
        fetch_meals(user_id, prev_start, prev_end),
        fetch_daily_target(user_id)
    )

    def analyze(data, range_start_str, range_end_str, label=""):
        # 1. Process logged data into a map (if data exists)
        daily_map = defaultdict(lambda: {"p": 0.0, "c": 0.0, "f": 0.0, "k": 0.0})
        
        if data and "results" in data:
            for item in data["results"]:
                ts = item.get("timestamp")
                if ts:
                    date_key = ts.split("T")[0]
                    daily_map[date_key]["p"] += float(item.get("protein", 0))
                    daily_map[date_key]["c"] += float(item.get("carbohydrates", 0))
                    daily_map[date_key]["f"] += float(item.get("fat", 0))
                    daily_map[date_key]["k"] += float(item.get("calories", 0))

        # 2. Setup Loop Variables
        totals = {"p": 0, "c": 0, "f": 0, "k": 0}
        logged_count = len(daily_map) # Count of days that actually have logs
        daily_stats = []
        
        curr_d = datetime.strptime(range_start_str, "%Y-%m-%d")
        end_d = datetime.strptime(range_end_str, "%Y-%m-%d")

        # 3. Iterate through EVERY day in the range (Start -> End)
        while curr_d <= end_d:
            d_str = curr_d.strftime("%Y-%m-%d")
            day_name = curr_d.strftime("%A")

            if d_str in daily_map:
                # -- Logic for Logged Days --
                stats = daily_map[d_str]
                eaten = int(stats["k"])
                
                totals["p"] += stats["p"]; totals["c"] += stats["c"]
                totals["f"] += stats["f"]; totals["k"] += stats["k"]
                
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
                # -- Logic for Empty Days  --
                daily_stats.append({
                    "day": day_name,
                    "is_logged": False,
                    "total_recc_kcal": target,
                    "total_eaten_kcal": 0,
                    "fulfillment_percentage": 0
                })

            curr_d += timedelta(days=1)

        # 4. Calculate Averages
        if logged_count > 0:
            avgs = {k: int(v/logged_count) for k, v in totals.items()}
        else:
            avgs = {"p": 0, "c": 0, "f": 0, "k": 0}
            
        logger.debug(f"ℹ️ {label} Analysis: {logged_count} days logged out of {len(daily_stats)} total days")
        
        # Always return the structure, even if logged_count is 0
        return {"avgs": avgs, "days": daily_stats, "count": logged_count}

    # Use the new signature with date ranges
    curr_res = analyze(curr_data, start_date, end_date, "CURRENT WEEK")
    prev_res = analyze(prev_data, prev_start, prev_end, "PREVIOUS WEEK")
    
    # This ensures an empty chart is generated if the user has 0 logs.
    if not curr_res: 
        logger.warning("⚠️ Unexpected error in nutrition analysis")
        return None
    
    prev_day_count = prev_res["count"] if prev_res else 0
    show_deltas = prev_day_count >= 1
    avg_curr = curr_res["avgs"]
    
    logger.info(f"Nutrition Status: Current Logged={curr_res['count']} days. Deltas={'ON' if show_deltas else 'OFF'}")

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
    
    logger.debug(f"Macro Persona Kcal Distribution: P={pct_p}% C={pct_c}% F={pct_f}%")

    goal_lower = primary_goal.lower()
    is_loss_goal = "loss" in goal_lower or "lose" in goal_lower
    is_gain_goal = "gain" in goal_lower
    
    if is_loss_goal:
        protein_threshold = 30; carbs_threshold = 50; fat_threshold = 35
        balanced_ranges = {"p": (25, 30), "c": (40, 50), "f": (20, 30)}
    elif is_gain_goal:
        protein_threshold = 25; carbs_threshold = 60; fat_threshold = 35
        balanced_ranges = {"p": (20, 25), "c": (50, 60), "f": (25, 35)}
    else:  # Maintenance
        protein_threshold = 25; carbs_threshold = 55; fat_threshold = 35
        balanced_ranges = {"p": (20, 25), "c": (45, 55), "f": (20, 30)}
    
    persona = None
    persona_title = "Balanced" 

    # Note: If 0 logs, percentages are 0, which falls through to "Balanced week"
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
    
    logger.info(f"🏷️ Assigned Persona: {persona_title}")

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
        response["macro_averages_7d"]["protein"].update({"delta_grams": deltas["p"], "positive_change": protein_positive})
        response["macro_averages_7d"]["carbs"].update({"delta_grams": deltas["c"], "positive_change": carbs_positive})
        response["macro_averages_7d"]["fats"].update({"delta_grams": deltas["f"], "positive_change": fats_positive})

    return response

async def build_faye(user_id, start_date, end_date):
    logger.info("🏗️ Building Faye Insights...")
    queries = await fetch_chats(user_id, start_date, end_date)
    if not queries:
        logger.warning("⚠️ No chats found - Skipping Faye Insights")
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
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024, temperature=0, messages=[{"role": "user", "content": prompt}]
        )
        result = json.loads(extract_json_from_llm_response(msg.content[0].text))
        logger.info("✅ Faye Insights generated successfully")
        return result
    except Exception as e:
        logger.error(f"❌ Error generating Faye insights: {e}")
        return None

async def build_wellness(user_id, start_date, end_date, primary_goal):
    logger.info("🏗️ Building Wellness Section...")
    dt_curr = datetime.strptime(start_date, "%Y-%m-%d")
    prev_start = (dt_curr - timedelta(days=7)).strftime("%Y-%m-%d")
    prev_end = (dt_curr - timedelta(days=1)).strftime("%Y-%m-%d")

    logger.info("⚡ Parallel Fetch: Wellness Trackers & Scans")
    curr_t, prev_t, scans = await asyncio.gather(
        fetch_trackers(user_id, start_date, end_date),
        fetch_trackers(user_id, prev_start, prev_end),
        fetch_scans(user_id, start_date, end_date)
    )

    wellness = {}

    # 1. Steps
    steps_list = curr_t.get("STEPS", [])
    total_steps = sum(int(x.get("total_steps", x.get("actual_value", 0))) for x in steps_list)
    if total_steps > 0:
        best = max(steps_list, key=lambda x: int(x.get("total_steps", x.get("actual_value", 0))))
        try:
            day_lbl = datetime.strptime(best.get("date", "")[:10], "%Y-%m-%d").strftime("%A")
        except:
            day_lbl = "-"
        wellness["total_steps"] = {
            "total_weekly_steps": total_steps,
            "best_single_day": {"step_count": int(best.get("total_steps", best.get("actual_value", 0))),
                                "day_label": day_lbl}
        }
        logger.info(f"👟 Steps processed: Total={total_steps}")
    else:
        logger.debug("ℹ️ No steps data found")

    # 2. Mood
    moods = [m.get("actual_value") for m in curr_t.get("MOOD_ENTRY", []) if m.get("actual_value")]
    if len(set(moods)) >= 2:
        counts = Counter(moods).most_common(3)
        clusters = []
        sizes = ["Large", "Medium", "Small"]
        emojis = {
            "HAPPY": "😊", "SAD": "😢", "ENERGETIC": "⚡", "FRISKY": "😏",
            "MOOD SWINGS": "🎭", "IRRITATED": "😠", "ANXIOUS": "😰", "DEPRESSED": "😞",
            "LOW ENERGY": "🔋", "CONFUSED": "😕", "APATHETIC": "😐", "CUSTOM": "✨"
        }
        if len(counts) == 2: random.shuffle(counts)
        for i, (mood_name, count) in enumerate(counts):
            clusters.append({
                "mood_name": mood_name,
                "emoji": emojis.get(mood_name, "😐"),
                "count": count,
                "visual_size": sizes[i] if i < 3 else "Small"
            })
        wellness["mood_mix"] = {"status": "active", "clusters": clusters}
        logger.info(f"😊 Mood Mix created: {len(clusters)} clusters")
    else:
        logger.debug(f"ℹ️ Insufficient mood diversity (Found {len(set(moods))} unique moods)")

    # 3. Scans
    if scans:
        processed = []
        for s in scans:
            name = s.get("name", "Unknown Item")
            try:
                score = float(s.get("foodhak_score", {}).get("Score", 0) if isinstance(s.get("foodhak_score"), dict) else s.get("foodhak_score", 0))
            except:
                score = 0.0
            image_url = s.get("image_url") or s.get("image") or s.get("product_image") or s.get("img_url") or None
            scan_item = {"product_name": name, "score": score}
            if image_url: scan_item["image_url"] = image_url
            processed.append(scan_item)
        processed.sort(key=lambda x: x["score"], reverse=True)
        wellness["top_scan"] = {
            "best_scan_summary": processed[0],
            "scan_history_sorted_desc": processed[:5]
        }
        logger.info(f"📸 Scans processed: {len(processed)} items")
    else:
        logger.debug("ℹ️ No scans found")

    # 4. Sleep
    sleep_list = curr_t.get("SLEEP", [])
    total_mins = 0; valid_sleep = 0
    for s in sleep_list:
        try:
            v = float(s.get("actual_value", 0))
            if v > 0: total_mins += v; valid_sleep += 1
        except: continue
    if valid_sleep > 0:
        avg = total_mins / valid_sleep
        wellness["sleep"] = {"avg_total_sleep_daily": f"{int(avg // 60)}h {int(avg % 60)}m"}
        logger.info(f"😴 Sleep processed: Avg {int(avg // 60)}h {int(avg % 60)}m")

    # 5. Weight
    def get_last_wt(data):
        if not data: return None
        try:
            return float(sorted(data, key=lambda x: x.get("timestamp", ""), reverse=True)[0].get("actual_value"))
        except: return None

    curr_wt = get_last_wt(curr_t.get("WEIGHT"))
    prev_wt = get_last_wt(prev_t.get("WEIGHT"))

    if curr_wt:
        delta = round(curr_wt - prev_wt, 1) if prev_wt else 0
        positive = is_positive_change(delta, 'weight', primary_goal)
        lbl = f"gained {delta}kg" if delta > 0 else f"lost {abs(delta)}kg" if delta < 0 else "no change"
        wellness["weight"] = {
            "current_value": curr_wt,
            "unit": "kg",
            "change_7d_value": delta,
            "change_7d_label": lbl,
            "positive_change": positive
        }
        logger.info(f"⚖️ Weight processed: {curr_wt}kg (Delta: {delta}kg)")

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
    logger.info(f"📅 Range: {request.start_date} to {request.end_date}")
    
    try:
        try:
            datetime.strptime(request.start_date, "%Y-%m-%d")
            datetime.strptime(request.end_date, "%Y-%m-%d")
        except ValueError:
            logger.error("❌ Invalid date format provided")
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

        # Step 1: Fetch Goal
        primary_goal = await fetch_primary_goal(request.user_id)
        
        # Step 2: Parallel Build
        logger.info("⚡ STARTING PARALLEL SECTION BUILD...")
        title_res, nutrition_res, faye_res, wellness_res = await asyncio.gather(
            build_title_card(request.user_id, request.start_date, request.end_date, primary_goal),
            build_nutrition(request.user_id, request.start_date, request.end_date, primary_goal),
            build_faye(request.user_id, request.start_date, request.end_date),
            build_wellness(request.user_id, request.start_date, request.end_date, primary_goal)
        )
        
        final_report = {}
        if title_res: final_report["title_card"] = title_res
        if nutrition_res: final_report["nutrition"] = nutrition_res
        if faye_res: final_report["faye_insights"] = faye_res
        if wellness_res: final_report.update(wellness_res)

        gen_time = round(time.time() - start_time, 2)
        logger.info(f"✅ REPORT GENERATED in {gen_time}s")
        logger.info(f"📦 Sections Included: {list(final_report.keys())}")

        # Step 3: Post to External
        external_api_url = "https://api-staging.foodhak.com/weekly-reports/create/"
        payload = {
            "user": request.user_id,
            "week_start": request.start_date,
            "week_end": request.end_date,
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
                logger.warning(f"Response: {external_response.text}")
                return JSONResponse(
                    status_code=200,
                    content={
                        "status": "partial_success",
                        "message": "Weekly report generated but failed to post to external API",
                        "weekly_summary_report": final_report,
                        "external_api_error": external_response.text
                    }
                )
        except httpx.HTTPError as e:
            logger.error(f"❌ EXTERNAL API ERROR: {e}")
            return JSONResponse(
                status_code=200,
                content={
                    "status": "partial_success",
                    "message": "Weekly report generated but failed to post to external API",
                    "weekly_summary_report": final_report,
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
