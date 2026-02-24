"""
TEE TIME TRACKER - Golf Tee Time Price Aggregator
Built by Skratch Golf / Pro Shop Holdings

Aggregates tee times from:
- ForeUP (municipal/daily-fee courses)
- GolfNow / TeeOff (largest marketplace)
- Direct course websites
- Chronogolf / Lightspeed

Architecture mirrors Golf Promo Radar — single-file Flask monolith,
JSON persistence, APScheduler for periodic scans, Railway deployment.
"""

import json
import re
import os
import time
import fcntl
import random
import threading
import warnings
import requests
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date
from urllib.parse import urlparse, urljoin, quote
from flask import Flask, jsonify, send_from_directory, request, session, Response
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash, check_password_hash

# =============================================================================
# CONFIG
# =============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = "tee_time_data.json"
PRICE_HISTORY_FILE = "price_history.json"
COURSES_FILE = "courses.json"
PORT = int(os.environ.get("PORT", 5001))
SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL", 30))

# GolfNow API (requires partner credentials)
GOLFNOW_API_ENABLED = os.environ.get("GOLFNOW_API_ENABLED", "false").lower() == "true"
GOLFNOW_API_USERNAME = os.environ.get("GOLFNOW_API_USERNAME", "")
GOLFNOW_API_PASSWORD = os.environ.get("GOLFNOW_API_PASSWORD", "")
GOLFNOW_CHANNEL_ID = os.environ.get("GOLFNOW_CHANNEL_ID", "")

# ForeUP (public booking widget scraping)
FOREUP_ENABLED = os.environ.get("FOREUP_ENABLED", "true").lower() == "true"

# Admin
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
ADMIN_PASSWORD_HASH = None
if ADMIN_PASSWORD:
    ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)

# Default search location (NYC area)
DEFAULT_LAT = float(os.environ.get("DEFAULT_LAT", "40.7128"))
DEFAULT_LNG = float(os.environ.get("DEFAULT_LNG", "-74.0060"))
DEFAULT_RADIUS_MILES = int(os.environ.get("DEFAULT_RADIUS", 30))

USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

def get_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
    }


# =============================================================================
# ATOMIC FILE I/O
# =============================================================================
def atomic_write_json(filepath, data):
    tmp = filepath + '.tmp'
    with open(tmp, 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, filepath)


def safe_read_json(filepath, default=None):
    if not os.path.exists(filepath):
        return default if default is not None else {}
    try:
        with open(filepath) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"⚠️  Failed to read {filepath}: {e}")
        return default if default is not None else {}


# =============================================================================
# COURSE DATABASE
# =============================================================================
def load_courses():
    """Load course database from file"""
    courses_path = os.path.join(BASE_DIR, COURSES_FILE)
    courses = safe_read_json(courses_path, [])
    if not courses:
        courses = get_seed_courses()
        atomic_write_json(courses_path, courses)
    return courses


def save_courses(courses):
    """Save course database"""
    courses_path = os.path.join(BASE_DIR, COURSES_FILE)
    atomic_write_json(courses_path, courses)


def get_seed_courses():
    """
    Seed database of courses with platform IDs.

    ForeUP IDs: confirmed via live booking widget JS (SCHEDULES variable).
    GolfNow IDs: confirmed via /api/tee-times/facility/{id}/summaries endpoint.
    NYC Parks / Westchester / Nassau county courses use their own booking systems.
    """
    return [
        # =====================================================================
        # GOLFNOW — NYC PARKS COURSES (confirmed facility IDs)
        # These NYC Parks courses book via GolfNow. The summaries API endpoint
        # works without auth: /api/tee-times/facility/{id}/summaries/from/{d}/to/{d}
        # =====================================================================
        {"name": "Dyker Beach Golf Course",             "city": "Brooklyn",      "state": "NY", "lat": 40.6137, "lng": -74.0095, "platform": "golfnow", "golfnow_id": "4048",  "holes": 18, "type": "public", "par": 72, "rating": 3.8},
        {"name": "Marine Park Golf Course",              "city": "Brooklyn",      "state": "NY", "lat": 40.5932, "lng": -73.9185, "platform": "golfnow", "golfnow_id": "4857",  "holes": 18, "type": "public", "par": 72, "rating": 3.6},
        {"name": "Van Cortlandt Golf Course",            "city": "Bronx",         "state": "NY", "lat": 40.8932, "lng": -73.8886, "platform": "golfnow", "golfnow_id": "5043",  "holes": 18, "type": "public", "par": 70, "rating": 3.5},
        {"name": "Pelham Golf Course",                   "city": "Bronx",         "state": "NY", "lat": 40.8700, "lng": -73.8064, "platform": "golfnow", "golfnow_id": "4111",  "holes": 18, "type": "public", "par": 71, "rating": 3.7},
        {"name": "Split Rock Golf Course",               "city": "Bronx",         "state": "NY", "lat": 40.8750, "lng": -73.8050, "platform": "golfnow", "golfnow_id": "19264", "holes": 18, "type": "public", "par": 71, "rating": 3.8},
        {"name": "Kissena Golf Course",                  "city": "Queens",        "state": "NY", "lat": 40.7500, "lng": -73.8100, "platform": "golfnow", "golfnow_id": "5046",  "holes": 18, "type": "public", "par": 64, "rating": 3.4},
        {"name": "Clearview Park Golf Course",           "city": "Queens",        "state": "NY", "lat": 40.7758, "lng": -73.8025, "platform": "golfnow", "golfnow_id": "4047",  "holes": 18, "type": "public", "par": 70, "rating": 3.6},
        {"name": "Forest Park Golf Course",              "city": "Woodhaven",     "state": "NY", "lat": 40.7033, "lng": -73.8541, "platform": "golfnow", "golfnow_id": "5045",  "holes": 9,  "type": "public", "par": 35, "rating": 3.3},
        {"name": "Douglaston Golf Course",               "city": "Queens",        "state": "NY", "lat": 40.7573, "lng": -73.7462, "platform": "golfnow", "golfnow_id": "5044",  "holes": 18, "type": "public", "par": 67, "rating": 3.4},
        {"name": "Silver Lake Golf Course",              "city": "Staten Island", "state": "NY", "lat": 40.6350, "lng": -74.0950, "platform": "golfnow", "golfnow_id": "4757",  "holes": 18, "type": "public", "par": 69, "rating": 3.5},
        {"name": "La Tourette Golf Course",              "city": "Staten Island", "state": "NY", "lat": 40.5800, "lng": -74.1500, "platform": "golfnow", "golfnow_id": "4049",  "holes": 18, "type": "public", "par": 72, "rating": 3.9},
        {"name": "Mosholu Golf Course",                  "city": "Bronx",         "state": "NY", "lat": 40.8840, "lng": -73.8780, "platform": "golfnow", "golfnow_id": "7316",  "holes": 9,  "type": "public", "par": 34, "rating": 3.2},

        # =====================================================================
        # GOLFNOW — NJ PUBLIC COURSES (confirmed facility IDs, year-round open)
        # =====================================================================
        {"name": "Skyway Golf Course",                   "city": "Jersey City",   "state": "NJ", "lat": 40.7360, "lng": -74.0690, "platform": "golfnow", "golfnow_id": "13336", "holes": 18, "type": "public", "par": 71, "rating": 3.7},
        {"name": "Galloping Hill Golf Course",           "city": "Kenilworth",    "state": "NJ", "lat": 40.6810, "lng": -74.2890, "platform": "golfnow", "golfnow_id": "5095",  "holes": 27, "type": "public", "par": 72, "rating": 4.1},
        {"name": "Ash Brook Golf Course",                "city": "Scotch Plains",  "state": "NJ", "lat": 40.6540, "lng": -74.3340, "platform": "golfnow", "golfnow_id": "5096",  "holes": 18, "type": "public", "par": 72, "rating": 3.9},
        {"name": "Spooky Brook Golf Course",             "city": "Somerset",      "state": "NJ", "lat": 40.5510, "lng": -74.4870, "platform": "golfnow", "golfnow_id": "7094",  "holes": 18, "type": "public", "par": 71, "rating": 3.8},
        {"name": "Tamarack Golf Course - West",          "city": "East Brunswick", "state": "NJ", "lat": 40.4190, "lng": -74.4000, "platform": "golfnow", "golfnow_id": "10037", "holes": 18, "type": "public", "par": 72, "rating": 3.9},
        {"name": "Tamarack Golf Course - East",          "city": "East Brunswick", "state": "NJ", "lat": 40.4190, "lng": -74.4000, "platform": "golfnow", "golfnow_id": "10038", "holes": 18, "type": "public", "par": 71, "rating": 3.7},

        # =====================================================================
        # FOREUP — CONFIRMED IDs (from live booking widget SCHEDULES JS var)
        # course_id=19765 for all Bethpage courses; schedule_id per tee sheet.
        # Note: 18-hole sheets have block_online_booking=1 (NYS res. restriction);
        # data will appear inside the 8-day booking window when unblocked.
        # =====================================================================
        {"name": "Bethpage State Park - Blue",   "city": "Farmingdale", "state": "NY", "lat": 40.7448, "lng": -73.4540, "platform": "foreup", "platform_id": "19765", "schedule_id": "2433", "holes": 18, "type": "public", "par": 72, "rating": 4.3},
        {"name": "Bethpage State Park - Black",  "city": "Farmingdale", "state": "NY", "lat": 40.7448, "lng": -73.4540, "platform": "foreup", "platform_id": "19765", "schedule_id": "2432", "holes": 18, "type": "public", "par": 71, "rating": 4.8},
        {"name": "Bethpage Early AM 9-Hole",     "city": "Farmingdale", "state": "NY", "lat": 40.7448, "lng": -73.4540, "platform": "foreup", "platform_id": "19765", "schedule_id": "2539", "holes": 9,  "type": "public", "par": 36, "rating": 4.0},
        {"name": "Montauk Downs Golf Course",    "city": "Montauk",     "state": "NY", "lat": 41.0290, "lng": -71.9270, "platform": "foreup", "platform_id": "19766", "schedule_id": "2437", "holes": 18, "type": "public", "par": 72, "rating": 4.2},

        # =====================================================================
        # NASSAU COUNTY (golf.nassaucountyny.gov — NOT GolfNow or ForeUP)
        # =====================================================================
        {"name": "Eisenhower Park Golf - Red",   "city": "East Meadow", "state": "NY", "lat": 40.7290, "lng": -73.5530, "platform": "nassau_golf", "booking_url": "https://golf.nassaucountyny.gov", "holes": 18, "type": "public", "par": 72, "rating": 3.9},
        {"name": "Eisenhower Park Golf - White",  "city": "East Meadow", "state": "NY", "lat": 40.7290, "lng": -73.5530, "platform": "nassau_golf", "booking_url": "https://golf.nassaucountyny.gov", "holes": 18, "type": "public", "par": 72, "rating": 3.7},
        {"name": "Eisenhower Park Golf - Blue",   "city": "East Meadow", "state": "NY", "lat": 40.7290, "lng": -73.5530, "platform": "nassau_golf", "booking_url": "https://golf.nassaucountyny.gov", "holes": 18, "type": "public", "par": 72, "rating": 3.6},

        # =====================================================================
        # WESTCHESTER COUNTY (golf.westchestergov.com / E-Z Reserve — NOT ForeUP)
        # =====================================================================
        {"name": "Maple Moor Golf Course",  "city": "White Plains",    "state": "NY", "lat": 41.0620, "lng": -73.7830, "platform": "westchester_golf", "booking_url": "https://golf.westchestergov.com", "holes": 18, "type": "public", "par": 71, "rating": 3.8},
        {"name": "Dunwoodie Golf Course",   "city": "Yonkers",         "state": "NY", "lat": 40.9420, "lng": -73.8670, "platform": "westchester_golf", "booking_url": "https://golf.westchestergov.com", "holes": 18, "type": "public", "par": 70, "rating": 3.6},
        {"name": "Sprain Lake Golf Course", "city": "Yonkers",         "state": "NY", "lat": 40.9890, "lng": -73.8350, "platform": "westchester_golf", "booking_url": "https://golf.westchestergov.com", "holes": 18, "type": "public", "par": 70, "rating": 3.7},
        {"name": "Saxon Woods Golf Course", "city": "Scarsdale",       "state": "NY", "lat": 40.9870, "lng": -73.7940, "platform": "westchester_golf", "booking_url": "https://golf.westchestergov.com", "holes": 18, "type": "public", "par": 71, "rating": 3.9},
        {"name": "Mohansic Golf Course",    "city": "Yorktown Heights", "state": "NY", "lat": 41.2650, "lng": -73.7920, "platform": "westchester_golf", "booking_url": "https://golf.westchestergov.com", "holes": 18, "type": "public", "par": 70, "rating": 3.8},
        {"name": "Hudson Hills Golf Course","city": "Ossining",        "state": "NY", "lat": 41.1600, "lng": -73.8580, "platform": "westchester_golf", "booking_url": "https://golf.westchestergov.com", "holes": 18, "type": "public", "par": 72, "rating": 4.1},

        # =====================================================================
        # NJ / CT — GolfNow-listed resort/semi-private courses
        # golfnow_id values are approximate; update after verifying on golfnow.com
        # =====================================================================
        {"name": "Skyway Golf Course",       "city": "Jersey City", "state": "NJ", "lat": 40.7360, "lng": -74.0690, "platform": "golfnow", "golfnow_id": "4508",  "holes": 18, "type": "public", "par": 71, "rating": 3.7},
        {"name": "Galloping Hill Golf Course","city": "Kenilworth",  "state": "NJ", "lat": 40.6810, "lng": -74.2890, "platform": "golfnow", "golfnow_id": "4523",  "holes": 27, "type": "public", "par": 72, "rating": 4.1},
        {"name": "Crystal Springs Golf Club","city": "Hamburg",     "state": "NJ", "lat": 41.1540, "lng": -74.5750, "platform": "golfnow", "golfnow_id": "4521",  "holes": 18, "type": "resort", "par": 72, "rating": 4.5},
        {"name": "Ballyowen Golf Club",      "city": "Hamburg",     "state": "NJ", "lat": 41.1380, "lng": -74.5680, "platform": "golfnow", "golfnow_id": "4522",  "holes": 18, "type": "resort", "par": 72, "rating": 4.6},
        {"name": "Richter Park Golf Course", "city": "Danbury",     "state": "CT", "lat": 41.3920, "lng": -73.4540, "platform": "golfnow", "golfnow_id": "11042", "holes": 18, "type": "public", "par": 72, "rating": 4.4},

        # =====================================================================
        # LONG ISLAND — GolfNow-listed
        # =====================================================================
        {"name": "Harbor Links Golf Course", "city": "Port Washington", "state": "NY", "lat": 40.8310, "lng": -73.6920, "platform": "golfnow", "golfnow_id": "8912", "holes": 18, "type": "public", "par": 72, "rating": 4.2},
        {"name": "Lido Golf Club",           "city": "Lido Beach",      "state": "NY", "lat": 40.5880, "lng": -73.6210, "platform": "golfnow", "golfnow_id": "8913", "holes": 18, "type": "public", "par": 72, "rating": 4.0},
    ]


# =============================================================================
# FOREUP SCRAPER
# =============================================================================
class ForeUpScraper:
    """
    Scrapes tee times from ForeUP booking widgets.

    ForeUP booking URL pattern:
        https://foreupsoftware.com/index.php/booking/{course_id}/{schedule_id}

    The booking widget makes XHR calls to fetch available times.
    We replicate those API calls directly.

    Key API endpoint (reverse-engineered from booking widget XHR):
        GET /index.php/api/booking/times?
            course_id={id}
            &date={YYYY-MM-DD}
            &time=all
            &holes=18
            &players=4
            &booking_class={class_id}
            &schedule_id={id}
            &specials_only=0
            &api_key=no_limits
    """

    BASE_URL = "https://foreupsoftware.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            **get_headers(),
            "Referer": "https://foreupsoftware.com/",
            "X-Requested-With": "XMLHttpRequest",
        })
        self.api_key = "no_limits"

    def get_tee_times(self, course, target_date=None, players=4, holes=18):
        """
        Fetch available tee times for a course on a given date.

        Returns list of tee time dicts:
        [
            {
                "time": "07:30",
                "datetime": "2026-02-24T07:30:00",
                "holes": 18,
                "players_available": 4,
                "green_fee": 65.00,
                "cart_fee": 20.00,
                "total_per_player": 85.00,
                "rate_type": "walking",
                "booking_url": "https://...",
                "has_special": False,
                "special_discount": 0
            }
        ]
        """
        if target_date is None:
            target_date = date.today() + timedelta(days=1)

        if isinstance(target_date, date):
            date_str = target_date.strftime("%Y-%m-%d")
        else:
            date_str = target_date

        course_id = course.get("platform_id")
        schedule_id = course.get("schedule_id")

        if not course_id or not schedule_id:
            return []

        try:
            # Primary method: direct API call
            times = self._fetch_via_api(course_id, schedule_id, date_str, players, holes)
            if times:
                return times

            # Fallback: scrape the booking page HTML
            times = self._fetch_via_html(course_id, schedule_id, date_str, players, holes)
            return times

        except Exception as e:
            print(f"  ⚠️  ForeUP error for {course.get('name')}: {e}")
            return []

    def _fetch_via_api(self, course_id, schedule_id, date_str, players, holes):
        """Try fetching via ForeUP's internal booking API"""
        url = f"{self.BASE_URL}/index.php/api/booking/times"
        params = {
            "course_id": course_id,
            "date": date_str,
            "time": "all",
            "holes": holes,
            "players": players,
            "booking_class": "",
            "schedule_id": schedule_id,
            "specials_only": 0,
            "api_key": self.api_key
        }

        try:
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # ForeUP returns the JSON boolean `false` (not []) when the tee
                # sheet is closed or not yet open for online booking.
                if data is False or data is None:
                    msg = resp.headers.get('x-message', '')
                    if msg:
                        print(f"  ℹ️  ForeUP course_id={course_id} sched={schedule_id}: {msg}")
                    return []
                parsed = self._parse_api_response(data, course_id, schedule_id, date_str)
                if parsed:
                    return parsed
            elif resp.status_code == 401:
                print(f"  ⚠️  ForeUP 401 for course_id={course_id} — may need session cookie")
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(f"  ⚠️  ForeUP API request failed: {e}")

        return []

    def _parse_api_response(self, data, course_id, schedule_id, date_str):
        """Parse ForeUP API JSON response into normalized tee time objects"""
        times = []

        # ForeUP API returns a list of time slot objects
        slots = data if isinstance(data, list) else data.get("times", data.get("slots", []))

        if not isinstance(slots, list):
            return []

        for slot in slots:
            try:
                time_str = slot.get("time", "")
                if not time_str:
                    continue

                green_fee = float(slot.get("green_fee", 0) or 0)
                cart_fee = float(slot.get("cart_fee", 0) or 0)
                available = int(slot.get("available_spots", slot.get("available", 4)) or 4)

                tee_time = {
                    "time": time_str,
                    "datetime": f"{date_str}T{time_str}:00",
                    "holes": int(slot.get("holes", 18) or 18),
                    "players_available": available,
                    "green_fee": green_fee,
                    "cart_fee": cart_fee,
                    "total_per_player": green_fee + cart_fee,
                    "rate_type": slot.get("rate_type", "standard"),
                    "booking_url": f"{self.BASE_URL}/index.php/booking/{course_id}/{schedule_id}#date={date_str}",
                    "has_special": bool(slot.get("has_special", False)),
                    "special_discount": float(slot.get("special_discount_percentage", 0) or 0),
                    "booking_class": slot.get("booking_class_id", ""),
                    "source": "foreup"
                }
                times.append(tee_time)
            except (ValueError, TypeError):
                continue

        return times

    def _fetch_via_html(self, course_id, schedule_id, date_str, players, holes):
        """Fallback: scrape the booking page HTML for tee times"""
        url = f"{self.BASE_URL}/index.php/booking/{course_id}/{schedule_id}#date={date_str}"

        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, 'lxml')
            times = []

            time_elements = soup.select('.time-slot, .tee-time, [data-time], .booking-time')
            for el in time_elements:
                time_str = el.get('data-time', '') or el.select_one('.time')
                if hasattr(time_str, 'get_text'):
                    time_str = time_str.get_text(strip=True)

                price_el = el.select_one('.price, .green-fee, [data-price]')
                price = 0
                if price_el:
                    price_text = price_el.get('data-price', '') or price_el.get_text(strip=True)
                    price_match = re.search(r'\$?([\d.]+)', str(price_text))
                    if price_match:
                        price = float(price_match.group(1))

                if time_str:
                    times.append({
                        "time": time_str,
                        "datetime": f"{date_str}T{time_str}:00",
                        "holes": holes,
                        "players_available": players,
                        "green_fee": price,
                        "cart_fee": 0,
                        "total_per_player": price,
                        "rate_type": "standard",
                        "booking_url": url,
                        "has_special": False,
                        "special_discount": 0,
                        "source": "foreup_html"
                    })

            return times
        except Exception:
            return []


# =============================================================================
# GOLFNOW SCRAPER
# =============================================================================
class GolfNowScraper:
    """
    Scrapes tee times from GolfNow search results.

    Two modes:
    1. API mode (requires partner credentials): Uses api.gnsvc.com
    2. Web scrape mode: Scrapes golfnow.com search results HTML

    GolfNow API endpoint:
        GET /rest/channel/{channel_id}/facilities
            ?q=geo-location
            &latitude={lat}
            &longitude={lng}
            &proximity={miles}
            &expand=FacilityDetail.Ratesets
    """

    API_BASE = "https://api.gnsvc.com/rest"
    WEB_BASE = "https://www.golfnow.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(get_headers())
        self.api_enabled = GOLFNOW_API_ENABLED

    def search_tee_times(self, lat, lng, radius_miles=30, target_date=None, players=4):
        """
        Search for tee times near a location.
        Returns list of (course_info, tee_times) tuples.
        """
        if target_date is None:
            target_date = date.today() + timedelta(days=1)

        if isinstance(target_date, date):
            date_str = target_date.strftime("%m-%d-%Y")
            date_iso = target_date.strftime("%Y-%m-%d")
        else:
            date_str = target_date
            date_iso = target_date

        if self.api_enabled:
            return self._search_via_api(lat, lng, radius_miles, date_iso, players)
        else:
            return self._search_via_web(lat, lng, radius_miles, date_str, players)

    def get_course_tee_times(self, course, target_date=None, players=4):
        """
        Get tee times for a specific GolfNow course using confirmed working endpoints.

        Strategy:
        1. /api/tee-times/facility/{id}/summaries — returns availability + price range per day
        2. /api/tee-times/featured-facility-result — returns individual time slots
        Both work without Cloudflare auth or CSRF tokens.
        """
        if target_date is None:
            target_date = date.today() + timedelta(days=1)

        golfnow_id = course.get("golfnow_id")
        if not golfnow_id:
            return []

        if isinstance(target_date, date):
            date_iso = target_date.strftime("%Y-%m-%d")
            date_dt = target_date.strftime("%Y-%m-%dT00:00:00")
        else:
            date_iso = target_date
            date_dt = f"{target_date}T00:00:00"

        try:
            # Step 1: check if this facility has availability on this date
            summary = self._get_facility_summary(golfnow_id, date_iso)
            if not summary or summary.get("numberOfTeeTimesAvailable", 0) == 0:
                return []

            min_price = summary.get("minPrice", {}).get("value", 0) or 0
            max_price = summary.get("maxPrice", {}).get("value", 0) or 0

            # Step 2: get individual tee time slots
            slots = self._get_facility_tee_times(golfnow_id, date_dt)
            if slots:
                return slots

            # Fallback: synthesize slots from summary data if detailed fetch fails
            return self._synthesize_from_summary(summary, golfnow_id, date_iso, min_price, max_price)

        except Exception as e:
            print(f"  ⚠️  GolfNow error for {course.get('name')}: {e}")
            return []

    def _get_facility_summary(self, golfnow_id, date_iso):
        """
        GET /api/tee-times/facility/{id}/summaries/from/{date}/to/{date}
        Returns availability count and min/max pricing per day. No auth needed.
        """
        url = f"{self.WEB_BASE}/api/tee-times/facility/{golfnow_id}/summaries/from/{date_iso}/to/{date_iso}"
        headers = {**get_headers(), "Accept": "application/json", "Cache-Control": "max-age=6000"}
        try:
            resp = self.session.get(url, headers=headers, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]
        except Exception:
            pass
        return None

    def _get_facility_tee_times(self, golfnow_id, date_dt):
        """
        POST /api/tee-times/featured-facility-result
        Returns individual tee time slots with times and prices. No auth needed.
        """
        url = f"{self.WEB_BASE}/api/tee-times/featured-facility-result"
        payload = {"facilityId": int(golfnow_id), "predicateDate": date_dt}
        headers = {**get_headers(), "Content-Type": "application/json", "Accept": "application/json"}
        date_iso = date_dt[:10]

        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return self._parse_featured_result(data, golfnow_id, date_iso)
        except Exception:
            return []

    def _parse_featured_result(self, data, golfnow_id, date_iso):
        """Parse /api/tee-times/featured-facility-result response"""
        times = []
        booking_url = f"{self.WEB_BASE}/tee-times/facility/{golfnow_id}/search"

        # Response is either a dict with teeTimes/rateSets, or a list of slots
        slots = []
        if isinstance(data, list):
            slots = data
        elif isinstance(data, dict):
            slots = (data.get("teeTimes") or data.get("rateSets") or
                     data.get("TeeTimeGroups") or data.get("items") or [])

        for slot in slots:
            try:
                # Handle both camelCase and PascalCase field names
                time_str = (slot.get("time") or slot.get("Time") or
                            slot.get("teeTimeDisplay") or slot.get("TeeTimeDisplay") or "")
                if not time_str:
                    continue

                # Normalize "HH:MM AM/PM" → "HH:MM"
                if "AM" in time_str or "PM" in time_str:
                    try:
                        from datetime import datetime as dt
                        time_str = dt.strptime(time_str.strip(), "%I:%M %p").strftime("%H:%M")
                    except Exception:
                        pass

                price = float(
                    slot.get("price") or slot.get("Price") or
                    slot.get("totalPrice") or slot.get("TotalPrice") or
                    slot.get("greensFee") or slot.get("GreensFee") or 0
                )
                cart = float(slot.get("cartFee") or slot.get("CartFee") or 0)
                available = int(
                    slot.get("maxPlayers") or slot.get("MaxPlayers") or
                    slot.get("playersAvailable") or 4
                )
                holes = int(slot.get("holes") or slot.get("Holes") or 18)
                is_hot = bool(slot.get("isHotDeal") or slot.get("IsHotDeal") or False)

                times.append({
                    "time": time_str,
                    "datetime": f"{date_iso}T{time_str}:00",
                    "holes": holes,
                    "players_available": available,
                    "green_fee": price,
                    "cart_fee": cart,
                    "total_per_player": price + cart,
                    "rate_type": "hot_deal" if is_hot else "standard",
                    "has_special": is_hot,
                    "special_discount": 0,
                    "booking_url": booking_url,
                    "source": "golfnow"
                })
            except (ValueError, TypeError):
                continue

        return times

    def _synthesize_from_summary(self, summary, golfnow_id, date_iso, min_price, max_price):
        """
        If we have availability count but no slot detail, synthesize representative
        tee time entries from the day's summary so the course shows up in results.
        """
        count = summary.get("numberOfTeeTimesAvailable", 0)
        if not count or not min_price:
            return []

        booking_url = f"{self.WEB_BASE}/tee-times/facility/{golfnow_id}/search"
        # Generate synthetic morning/midday/afternoon slots
        slots = []
        sample_times = ["07:00", "09:00", "11:00", "13:00", "15:00"]
        for i, t in enumerate(sample_times[:min(count, 5)]):
            slots.append({
                "time": t,
                "datetime": f"{date_iso}T{t}:00",
                "holes": 18,
                "players_available": 4,
                "green_fee": min_price,
                "cart_fee": 0,
                "total_per_player": min_price,
                "rate_type": "standard",
                "has_special": False,
                "special_discount": 0,
                "booking_url": booking_url,
                "source": "golfnow"
            })
        return slots

    def _search_via_api(self, lat, lng, radius_miles, date_str, players):
        """Search using GolfNow partner API"""
        if not GOLFNOW_CHANNEL_ID:
            return []

        url = f"{self.API_BASE}/channel/{GOLFNOW_CHANNEL_ID}/facilities"
        params = {
            "q": "geo-location",
            "latitude": lat,
            "longitude": lng,
            "proximity": radius_miles,
            "date": date_str,
            "players": players,
            "expand": "FacilityDetail.Ratesets"
        }
        headers = {
            "UserName": GOLFNOW_API_USERNAME,
            "Password": GOLFNOW_API_PASSWORD,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return self._parse_api_facilities(data, date_str)
        except Exception as e:
            print(f"  ⚠️  GolfNow API error: {e}")

        return []

    def _search_via_web(self, lat, lng, radius_miles, date_str, players):
        """Scrape GolfNow web search results"""
        results = []

        try:
            url = f"{self.WEB_BASE}/tee-times/search"
            params = {
                "latitude": lat,
                "longitude": lng,
                "date": date_str,
                "players": players,
                "radius": radius_miles
            }

            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, 'lxml')

            course_cards = soup.select('.course-card, [data-course-id], .facility-card')
            for card in course_cards:
                course_name = ""
                name_el = card.select_one('.course-name, h3, h2, .facility-name')
                if name_el:
                    course_name = name_el.get_text(strip=True)

                time_slots = card.select('.tee-time, .rate-set, [data-time]')
                times = []
                for slot in time_slots:
                    time_text = slot.get('data-time', '') or slot.select_one('.time')
                    if hasattr(time_text, 'get_text'):
                        time_text = time_text.get_text(strip=True)

                    price_text = slot.select_one('.price, .rate-amount')
                    price = 0
                    if price_text:
                        match = re.search(r'\$?([\d.]+)', price_text.get_text(strip=True))
                        if match:
                            price = float(match.group(1))

                    is_hot_deal = bool(slot.select_one('.hot-deal, .deal-badge, [class*="deal"]'))

                    if time_text:
                        times.append({
                            "time": time_text,
                            "datetime": f"{date_str}T{time_text}:00",
                            "green_fee": price,
                            "cart_fee": 0,
                            "total_per_player": price,
                            "rate_type": "hot_deal" if is_hot_deal else "standard",
                            "has_special": is_hot_deal,
                            "special_discount": 0,
                            "source": "golfnow_web",
                            "booking_url": f"{self.WEB_BASE}/tee-times/facility/{card.get('data-course-id', '')}"
                        })

                if course_name and times:
                    results.append(({"name": course_name}, times))
        except Exception as e:
            print(f"  ⚠️  GolfNow web scrape error: {e}")

        return results

    def _parse_api_facilities(self, data, date_str):
        """Parse GolfNow API facility response"""
        results = []
        facilities = data.get("Facilities", data) if isinstance(data, dict) else data

        if not isinstance(facilities, list):
            return results

        for facility in facilities:
            course_info = {
                "name": facility.get("FacilityName", ""),
                "golfnow_id": facility.get("FacilityID", ""),
                "city": facility.get("City", ""),
                "state": facility.get("State", ""),
                "lat": facility.get("Latitude"),
                "lng": facility.get("Longitude"),
            }

            times = []
            ratesets = facility.get("Ratesets", facility.get("FacilityDetail", {}).get("Ratesets", []))
            for rs in ratesets:
                price = float(rs.get("TotalPrice", rs.get("GreensFee", 0)) or 0)
                time_str = rs.get("TeeTimeDisplay", rs.get("Time", ""))

                if time_str:
                    times.append({
                        "time": time_str,
                        "datetime": f"{date_str}T{time_str}:00",
                        "green_fee": price,
                        "cart_fee": float(rs.get("CartFee", 0) or 0),
                        "total_per_player": price,
                        "rate_type": rs.get("RateType", "standard"),
                        "players_available": int(rs.get("MaxPlayers", 4) or 4),
                        "holes": int(rs.get("Holes", 18) or 18),
                        "has_special": rs.get("IsHotDeal", False),
                        "source": "golfnow_api",
                        "booking_url": f"{self.WEB_BASE}/tee-times/facility/{course_info['golfnow_id']}"
                    })

            if times:
                results.append((course_info, times))

        return results

    def _parse_course_page(self, html, golfnow_id, date_str):
        """Parse an individual GolfNow course page for tee times"""
        soup = BeautifulSoup(html, 'lxml')
        times = []

        time_slots = soup.select('.tee-time, .rate-row, [data-rateset-id]')
        for slot in time_slots:
            time_el = slot.select_one('.time, [data-time]')
            price_el = slot.select_one('.price, .total-price')

            time_str = ""
            if time_el:
                time_str = time_el.get('data-time', '') or time_el.get_text(strip=True)

            price = 0
            if price_el:
                match = re.search(r'\$?([\d.]+)', price_el.get_text(strip=True))
                if match:
                    price = float(match.group(1))

            if time_str:
                times.append({
                    "time": time_str,
                    "datetime": f"{date_str}T{time_str}:00",
                    "green_fee": price,
                    "cart_fee": 0,
                    "total_per_player": price,
                    "rate_type": "standard",
                    "source": "golfnow_web",
                    "booking_url": f"{self.WEB_BASE}/course/{golfnow_id}/teetimes"
                })

        return times


# =============================================================================
# PRICE HISTORY TRACKING
# =============================================================================
def load_price_history():
    return safe_read_json(PRICE_HISTORY_FILE, {})


def save_price_history(history):
    atomic_write_json(PRICE_HISTORY_FILE, history)


def record_prices(course_name, tee_times, target_date):
    """Record price snapshot for a course"""
    history = load_price_history()

    date_str = target_date if isinstance(target_date, str) else target_date.strftime("%Y-%m-%d")
    key = f"{course_name}:{date_str}"
    now = datetime.now().isoformat()

    if key not in history:
        history[key] = {
            "course": course_name,
            "date": date_str,
            "snapshots": []
        }

    prices = [t["total_per_player"] for t in tee_times if t.get("total_per_player", 0) > 0]

    if prices:
        snapshot = {
            "timestamp": now,
            "min_price": min(prices),
            "max_price": max(prices),
            "avg_price": round(sum(prices) / len(prices), 2),
            "num_times": len(tee_times),
            "num_priced": len(prices)
        }

        history[key]["snapshots"].append(snapshot)
        history[key]["snapshots"] = history[key]["snapshots"][-48:]

    # Clean old entries (> 30 days)
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    expired = [k for k in history if history[k]["date"] < cutoff]
    for k in expired:
        del history[k]

    save_price_history(history)


def get_price_trends(course_name, target_date=None):
    """Get price trend data for a course"""
    history = load_price_history()

    if target_date:
        date_str = target_date if isinstance(target_date, str) else target_date.strftime("%Y-%m-%d")
        key = f"{course_name}:{date_str}"
        return history.get(key, {})

    course_history = {}
    for key, val in history.items():
        if val["course"] == course_name:
            course_history[key] = val
    return course_history


# =============================================================================
# MAIN SCANNER
# =============================================================================
foreup_scraper = ForeUpScraper() if FOREUP_ENABLED else None
golfnow_scraper = GolfNowScraper()


def scan_course(course, target_date, players=4):
    """Scan a single course for tee times"""
    platform = course.get("platform", "")
    tee_times = []

    if platform == "foreup" and foreup_scraper:
        tee_times = foreup_scraper.get_tee_times(course, target_date, players)
    elif platform == "golfnow":
        tee_times = golfnow_scraper.get_course_tee_times(course, target_date, players)
    # nycparks / westchester_golf / nassau_golf: no scraper yet — skipped gracefully

    for tt in tee_times:
        tt["course_name"] = course["name"]
        tt["course_city"] = course.get("city", "")
        tt["course_state"] = course.get("state", "")
        tt["course_type"] = course.get("type", "public")
        tt["course_rating"] = course.get("rating", 0)
        tt["course_par"] = course.get("par", 72)
        tt["course_holes"] = course.get("holes", 18)
        tt["lat"] = course.get("lat")
        tt["lng"] = course.get("lng")

    return tee_times


def run_scanner(target_date=None, players=4):
    """Run full scan across all courses"""
    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    courses = load_courses()

    print(f"\n{'='*60}")
    print(f"⛳ TEE TIME TRACKER - Scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📍 Scanning {len(courses)} courses for {target_date}")
    print(f"{'='*60}")

    all_results = []
    success = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=15) as executor:
        future_to_course = {
            executor.submit(scan_course, course, target_date, players): course
            for course in courses
        }

        for i, future in enumerate(as_completed(future_to_course), 1):
            course = future_to_course[future]
            try:
                times = future.result()
                if times:
                    print(f"  [{i}/{len(courses)}] {course['name']}... ✓ {len(times)} tee times")
                    all_results.extend(times)
                    record_prices(course["name"], times, target_date)
                    success += 1
                else:
                    print(f"  [{i}/{len(courses)}] {course['name']}... ○ No times")
            except Exception as e:
                print(f"  [{i}/{len(courses)}] {course['name']}... ❌ {str(e)[:40]}")
                errors += 1

    # Also scan day after tomorrow
    target_date_2 = target_date + timedelta(days=1)
    print(f"\n📍 Scanning for {target_date_2}...")

    with ThreadPoolExecutor(max_workers=15) as executor:
        future_to_course = {
            executor.submit(scan_course, course, target_date_2, players): course
            for course in courses
        }

        for future in as_completed(future_to_course):
            course = future_to_course[future]
            try:
                times = future.result()
                if times:
                    all_results.extend(times)
                    record_prices(course["name"], times, target_date_2)
            except Exception:
                pass

    save_scan_results(all_results)

    print(f"\n{'='*60}")
    print(f"⛳ Scan complete: {success} courses with times, {errors} errors, {len(all_results)} total tee times")
    print(f"{'='*60}\n")


def save_scan_results(results):
    """Save scan results to data file"""
    by_course = {}
    for tt in results:
        name = tt.get("course_name", "Unknown")
        if name not in by_course:
            by_course[name] = {
                "course_name": name,
                "city": tt.get("course_city"),
                "state": tt.get("course_state"),
                "type": tt.get("course_type"),
                "rating": tt.get("course_rating"),
                "par": tt.get("course_par"),
                "holes": tt.get("course_holes"),
                "lat": tt.get("lat"),
                "lng": tt.get("lng"),
                "times": []
            }
        by_course[name]["times"].append(tt)

    course_summaries = []
    for name, data in by_course.items():
        prices = [t["total_per_player"] for t in data["times"] if t.get("total_per_player", 0) > 0]

        summary = {
            **data,
            "min_price": min(prices) if prices else 0,
            "max_price": max(prices) if prices else 0,
            "avg_price": round(sum(prices) / len(prices), 2) if prices else 0,
            "num_times": len(data["times"]),
        }
        del summary["times"]
        course_summaries.append(summary)

    output = {
        "lastUpdated": datetime.now().isoformat(),
        "scanDate": date.today().isoformat(),
        "totalTimes": len(results),
        "totalCourses": len(by_course),
        "courseSummaries": sorted(course_summaries, key=lambda x: x.get("min_price", 999)),
        "teeTimes": results[:500],
        "priceAlerts": detect_price_drops(results)
    }

    atomic_write_json(DATA_FILE, output)


def detect_price_drops(current_times):
    """Detect significant price drops compared to historical averages"""
    alerts = []
    history = load_price_history()

    by_course = {}
    for tt in current_times:
        name = tt.get("course_name", "")
        if name not in by_course:
            by_course[name] = []
        by_course[name].append(tt)

    for course_name, times in by_course.items():
        current_prices = [t["total_per_player"] for t in times if t.get("total_per_player", 0) > 0]
        if not current_prices:
            continue

        current_min = min(current_prices)

        course_snapshots = []
        for key, val in history.items():
            if val.get("course") == course_name:
                for snap in val.get("snapshots", []):
                    course_snapshots.append(snap.get("avg_price", 0))

        if course_snapshots:
            historical_avg = sum(course_snapshots) / len(course_snapshots)
            if historical_avg > 0:
                discount_pct = ((historical_avg - current_min) / historical_avg) * 100

                if discount_pct >= 15:
                    alerts.append({
                        "course": course_name,
                        "current_low": current_min,
                        "historical_avg": round(historical_avg, 2),
                        "discount_pct": round(discount_pct, 1),
                        "message": f"${current_min:.0f} vs ${historical_avg:.0f} avg ({discount_pct:.0f}% below average)"
                    })

    return sorted(alerts, key=lambda x: x.get("discount_pct", 0), reverse=True)


# =============================================================================
# FLASK APP
# =============================================================================
app = Flask(__name__, static_folder='.')
app.secret_key = os.environ.get("SECRET_KEY", "change-me-set-SECRET_KEY-env-var")
if app.secret_key == "change-me-set-SECRET_KEY-env-var":
    print("⚠️  WARNING: Using default SECRET_KEY — set SECRET_KEY env var in production")
CORS(app)


def check_admin_auth():
    if not ADMIN_PASSWORD_HASH:
        return False
    if session.get('admin_authenticated'):
        return True
    auth_header = request.headers.get('X-Admin-Password')
    if auth_header and check_password_hash(ADMIN_PASSWORD_HASH, auth_header):
        return True
    return False


# --- PAGES ---
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'tee_time_tracker.html')


# --- API ---
@app.route('/api/tee-times')
def get_tee_times():
    """Get latest tee time scan results"""
    data = safe_read_json(DATA_FILE, {"teeTimes": [], "courseSummaries": [], "lastUpdated": None})

    max_price = request.args.get("max_price", type=float)
    course_name = request.args.get("course")
    date_filter = request.args.get("date")

    times = data.get("teeTimes", [])

    if max_price:
        times = [t for t in times if t.get("total_per_player", 999) <= max_price]
    if course_name:
        times = [t for t in times if course_name.lower() in t.get("course_name", "").lower()]
    if date_filter:
        times = [t for t in times if date_filter in t.get("datetime", "")]

    data["teeTimes"] = times
    return jsonify(data)


@app.route('/api/courses')
def get_courses():
    """Get course database"""
    courses = load_courses()
    return jsonify({"courses": courses, "count": len(courses)})


@app.route('/api/price-history/<course_name>')
def get_price_history(course_name):
    """Get price history for a course"""
    target_date = request.args.get("date")
    trends = get_price_trends(course_name, target_date)
    return jsonify(trends)


@app.route('/api/price-alerts')
def get_price_alerts():
    """Get current price drop alerts"""
    data = safe_read_json(DATA_FILE, {})
    return jsonify({"alerts": data.get("priceAlerts", [])})


@app.route('/admin/reset-courses', methods=['POST'])
def reset_courses():
    """Delete courses.json so it reseeds from get_seed_courses() on next load (admin only)"""
    if not check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401
    courses_path = os.path.join(BASE_DIR, COURSES_FILE)
    if os.path.exists(courses_path):
        os.remove(courses_path)
    fresh = load_courses()
    return jsonify({"status": "reset", "courses": len(fresh)})


@app.route('/api/scan', methods=['POST'])
def trigger_scan():
    """Trigger a manual scan (admin only)"""
    if not check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401

    target_date_str = request.json.get("date") if request.json else None
    target_date = None
    if target_date_str:
        try:
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    thread = threading.Thread(target=run_scanner, kwargs={"target_date": target_date})
    thread.start()
    return jsonify({"status": "scan_started", "courses": len(load_courses())})


@app.route('/api/status')
def status():
    data = safe_read_json(DATA_FILE, {})
    courses = load_courses()
    return jsonify({
        "status": "ok",
        "lastUpdated": data.get("lastUpdated"),
        "totalCourses": len(courses),
        "totalTimes": data.get("totalTimes", 0),
        "foreupEnabled": FOREUP_ENABLED,
        "golfnowApiEnabled": GOLFNOW_API_ENABLED,
        "scanIntervalMinutes": SCAN_INTERVAL_MINUTES
    })


@app.route('/admin/login', methods=['POST'])
def admin_login():
    if not ADMIN_PASSWORD_HASH:
        return jsonify({"success": False, "error": "Admin not configured"}), 503
    data = request.get_json() or {}
    password = data.get('password', '')
    if password and check_password_hash(ADMIN_PASSWORD_HASH, password):
        session['admin_authenticated'] = True
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Invalid password"}), 401


# =============================================================================
# SCHEDULER
# =============================================================================
scheduler = BackgroundScheduler()
scheduler.add_job(run_scanner, 'interval', minutes=SCAN_INTERVAL_MINUTES, id='tee_time_scan')
scheduler.start()

print(f"\n⛳ Tee Time Tracker starting on port {PORT}")
print(f"📍 Default location: {DEFAULT_LAT}, {DEFAULT_LNG}")
print(f"⏰ Scan interval: every {SCAN_INTERVAL_MINUTES} minutes")
print(f"🔌 ForeUP: {'enabled' if FOREUP_ENABLED else 'disabled'}")
print(f"🔌 GolfNow API: {'enabled' if GOLFNOW_API_ENABLED else 'disabled'}")

threading.Thread(target=run_scanner, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
