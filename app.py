import os
import math
import requests
import polyline as polyline_lib
from datetime import datetime, timezone, timedelta
from flask import Flask, redirect, request, session, render_template, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'fallback-secret')

CLIENT_ID = os.getenv('STRAVA_CLIENT_ID')
CLIENT_SECRET = os.getenv('STRAVA_CLIENT_SECRET')
_railway_domain = os.getenv('RAILWAY_PUBLIC_DOMAIN') or os.getenv('RAILWAY_STATIC_URL')
if _railway_domain:
    REDIRECT_URI = f'https://{_railway_domain}/callback'
else:
    REDIRECT_URI = os.getenv('REDIRECT_URI', 'http://localhost:5001/callback')

# ── Strava OAuth ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    logged_in = 'access_token' in session
    athlete = session.get('athlete', {})
    return render_template('index.html', logged_in=logged_in, athlete=athlete)

@app.route('/login')
def login():
    auth_url = (
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&approval_prompt=force"
        f"&scope=read,read_all,activity:read,activity:read_all"
    )
    return redirect(auth_url)

@app.route('/callback')
def callback():
    code = request.args.get('code')
    if not code:
        return redirect('/')
    resp = requests.post('https://www.strava.com/oauth/token', data={
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
    })
    data = resp.json()
    session['access_token'] = data.get('access_token')
    session['athlete'] = data.get('athlete', {})
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    """Distance in km between two coordinates."""
    R = 6371
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))

def get_bearing(lat1, lon1, lat2, lon2):
    """Compass bearing in degrees from point 1 to point 2."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def bearing_label(bearing):
    dirs = ['N', 'NO', 'O', 'ZO', 'Z', 'ZW', 'W', 'NW']
    return dirs[round(bearing / 45) % 8]

def get_wind(lat, lon, start_dt=None):
    """
    Fetch wind from Open-Meteo (open-meteo.com).
    Uses the 7-day hourly forecast model (GFS/ECMWF blend).
    If start_dt is None, returns current conditions.
    """
    if start_dt is None:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat:.4f}&longitude={lon:.4f}"
            f"&current_weather=true"
        )
        try:
            r = requests.get(url, timeout=5)
            cw = r.json().get('current_weather', {})
            return cw.get('windspeed', 0), cw.get('winddirection', 0)
        except Exception:
            return 0, 0
    else:
        # Hourly forecast for a specific time (up to 7 days ahead)
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat:.4f}&longitude={lon:.4f}"
            f"&hourly=windspeed_10m,winddirection_10m"
            f"&forecast_days=7&timezone=auto"
        )
        try:
            r = requests.get(url, timeout=5)
            data = r.json()
            times = data['hourly']['time']
            speeds = data['hourly']['windspeed_10m']
            dirs = data['hourly']['winddirection_10m']
            # Match the requested hour (round down to hour)
            target = start_dt.strftime('%Y-%m-%dT%H:00')
            if target in times:
                idx = times.index(target)
                return speeds[idx] or 0, dirs[idx] or 0
            return 0, 0
        except Exception:
            return 0, 0

def score_direction(points, wind_speed, wind_dir):
    """
    Average wind-effect score for riding points in order.
    Positive = net tailwind, negative = net headwind.
    Uses cos(angle between travel bearing and wind direction).
    """
    if len(points) < 2:
        return 0
    total = 0
    for i in range(len(points) - 1):
        bearing = get_bearing(points[i][0], points[i][1], points[i+1][0], points[i+1][1])
        angle = math.radians(bearing - wind_dir)
        total += -math.cos(angle) * wind_speed
    return total / (len(points) - 1)

def sample_points(points, n=8):
    """Evenly sample n points from a list."""
    if len(points) <= n:
        return points
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]

def wind_label(score_10, wind_speed):
    """Label based on 0-10 return-tailwind score."""
    if wind_speed < 5:
        return ('Weinig wind', '🌤️')
    if score_10 >= 9:
        return ('Sterke meewind terug', '💨✅')
    elif score_10 >= 7.5:
        return ('Meewind terug', '✅')
    elif score_10 >= 6:
        return ('Lichte meewind terug', '➡️')
    elif score_10 >= 4.5:
        return ('Zijwind / weinig voordeel', '↗️')
    else:
        return ('Tegenwind op terugweg', '⛔')

# ── API endpoint ──────────────────────────────────────────────────────────────

@app.route('/api/routes')
def api_routes():
    access_token = session.get('access_token')
    if not access_token:
        return jsonify({'error': 'Niet ingelogd'}), 401

    try:
        user_lat = float(request.args.get('lat'))
        user_lon = float(request.args.get('lon'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Locatie ontbreekt'}), 400

    radius = float(request.args.get('radius', 40))

    # Optional start time for forecast wind
    start_dt = None
    start_time_str = request.args.get('start_time', '').strip()
    if start_time_str:
        try:
            start_dt = datetime.fromisoformat(start_time_str)
        except ValueError:
            pass

    headers = {'Authorization': f'Bearer {access_token}'}

    source = request.args.get('source', 'activities')  # 'activities' | 'saved' | 'both'

    strava_items = []

    # ── Fetch all saved routes (paginated) ────────────────────────────────────
    if source in ('saved', 'both'):
        page = 1
        while True:
            resp = requests.get(
                f'https://www.strava.com/api/v3/athlete/routes?per_page=50&page={page}',
                headers=headers
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            for r in batch:
                if r.get('type') == 2:  # skip running routes
                    continue
                r['_url'] = f"https://www.strava.com/routes/{r['id']}"
                r['_item_type'] = 'route'
            strava_items.extend([r for r in batch if r.get('type') != 2])
            if len(batch) < 50:
                break
            page += 1

    # ── Fetch cycling activities from last 2 years ────────────────────────────
    if source in ('activities', 'both'):
        RIDE_TYPES = {'Ride', 'EBikeRide', 'GravelRide', 'MountainBikeRide', 'VirtualRide'}
        after_ts = int((datetime.now() - timedelta(days=730)).timestamp())
        page = 1
        fetched = 0
        while fetched < 600:
            resp = requests.get(
                f'https://www.strava.com/api/v3/athlete/activities'
                f'?per_page=50&page={page}&after={after_ts}',
                headers=headers
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            for a in batch:
                if a.get('type') in RIDE_TYPES:
                    polyline = a.get('map', {}).get('summary_polyline', '')
                    if polyline:
                        a['elevation_gain'] = a.get('total_elevation_gain', 0)
                        a['map'] = {'summary_polyline': polyline}
                        a['_url'] = f"https://www.strava.com/activities/{a['id']}"
                        a['_item_type'] = 'activity'
                        strava_items.append(a)
                        fetched += 1
            if len(batch) < 50:
                break
            page += 1

    if not strava_items:
        error_msg = {
            'activities': 'Geen gereden ritten gevonden op Strava (laatste 2 jaar)',
            'saved':      'Geen opgeslagen routes gevonden op Strava',
            'both':       'Geen routes of ritten gevonden op Strava',
        }.get(source, 'Geen resultaten gevonden')
        return jsonify({'error': error_msg}), 502

    routes = strava_items

    results = []
    for route in routes:
        # Decode polyline
        map_data = route.get('map', {})
        encoded = map_data.get('polyline') or map_data.get('summary_polyline', '')
        if not encoded:
            continue

        try:
            points = polyline_lib.decode(encoded)
        except Exception:
            continue

        if len(points) < 2:
            continue

        start_lat, start_lon = points[0]
        end_lat, end_lon = points[-1]

        # Alleen rondje-routes: eindpunt binnen 10% van totale afstand (min 3 km, max 10 km)
        total_km = route.get('distance', 0) / 1000
        loop_threshold = max(3.0, min(10.0, total_km * 0.10))
        loop_dist_km = haversine(start_lat, start_lon, end_lat, end_lon)
        if loop_dist_km > loop_threshold:
            continue

        dist_from_user = haversine(user_lat, user_lon, start_lat, start_lon)
        if dist_from_user > radius:
            continue

        # Sample points for scoring (8) and map rendering (30)
        sampled = sample_points(points, n=8)
        map_pts = sample_points(points, n=30)
        mid = sampled[len(sampled) // 2]
        wind_speed, wind_dir = get_wind(mid[0], mid[1], start_dt)

        fwd_score = score_direction(sampled, wind_speed, wind_dir)

        # ── Direction logic: score each half of the loop separately ──────────
        # Split into outward (first half) and return (second half) leg.
        # Choose the direction that gives the best tailwind on the way HOME.
        mid_idx = len(sampled) // 2
        first_half  = sampled[:mid_idx + 1]   # start → midpoint
        second_half = sampled[mid_idx:]        # midpoint → end

        # Wind score on second half riding forward (= return leg when riding normally)
        second_half_score = score_direction(second_half, wind_speed, wind_dir)
        # Wind score on first half riding in reverse (= return leg when riding reversed)
        first_half_rev_score = score_direction(list(reversed(first_half)), wind_speed, wind_dir)

        if second_half_score >= first_half_rev_score:
            return_score = second_half_score   # can be negative (headwind on return)
            best_dir = 'heen'
        else:
            return_score = first_half_rev_score
            best_dir = 'terug'

        def calc_meewind_pct(pts):
            cos_vals = []
            for i in range(len(pts) - 1):
                b = get_bearing(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])
                cos_vals.append(-math.cos(math.radians(b - wind_dir)))
            return round(100 * sum(1 for c in cos_vals if c > 0.1) / len(cos_vals)) if cos_vals else 50

        # Return-leg meewind% voor beste richting (wat de app aanbeveelt)
        return_pts_best = second_half if best_dir == 'heen' else list(reversed(first_half))
        meewind_pct = calc_meewind_pct(return_pts_best)

        # Return-leg meewind% voor de ANDERE richting (andersom gereden)
        return_pts_alt = list(reversed(first_half)) if best_dir == 'heen' else second_half
        meewind_pct_alt = calc_meewind_pct(return_pts_alt)

        # ── 0-10 score ────────────────────────────────────────────────────────
        # return_score / wind_speed ∈ [-1, 1]
        # Map to [0, 1] then blend with no-wind neutral score
        MAX_WIND_REF = 20.0
        avg_cos_return = (return_score / wind_speed) if wind_speed > 0 else 0
        avg_cos_return = (avg_cos_return + 1) / 2   # [-1,1] → [0,1]
        avg_cos_return = max(0.0, min(1.0, avg_cos_return))
        wind_factor = min(wind_speed / MAX_WIND_REF, 1.0)
        no_wind_score = 7.0
        wind_score = 3.0 + avg_cos_return * 7.0     # 3.0 (full headwind) → 10.0 (full tailwind)
        score_10 = no_wind_score * (1 - wind_factor) + wind_score * wind_factor
        score_10 = round(max(1.0, min(10.0, score_10)), 1)

        label, icon = wind_label(score_10, wind_speed)

        # ── Shelter advice ──
        # When wind is significant but route alignment is weak, suggest sheltered routes
        shelter_advice = None
        if wind_speed >= 15 and score_10 <= 6.5:
            shelter_advice = (
                '🌲 Weinig windvoordeel op terugweg — overweeg een route door '
                'bos of bebouwing voor minder windweerstand.'
            )

        # ── Per-segment cos values for map coloring ──
        # Calculate cos in forward direction, then orient map to best_dir
        fwd_cos = []
        for i in range(len(map_pts) - 1):
            bearing = get_bearing(map_pts[i][0], map_pts[i][1],
                                  map_pts[i+1][0], map_pts[i+1][1])
            fwd_cos.append(round(-math.cos(math.radians(bearing - wind_dir)), 3))

        if best_dir == 'terug':
            # Reverse both points and cos values so map shows B→A (recommended direction)
            oriented_pts = list(reversed(map_pts))
            segment_cos = [round(-c, 3) for c in reversed(fwd_cos)]
        else:
            oriented_pts = map_pts
            segment_cos = fwd_cos

        # Calorieverbranding: 90 kg fietser
        # Basis: 0.5 kcal/kg/km, hoogte: +8 kcal per 10m/90kg, wind: ±12%
        elev = route.get('elevation_gain', 0)
        base_kcal = (route.get('distance', 0) / 1000) * 90 * 0.5
        elev_kcal = (elev / 10) * 8
        wind_factor_cal = 1 + (fwd_score / max(wind_speed, 1)) * 0.12 if wind_speed > 2 else 1.0
        wind_factor_cal = max(0.88, min(1.12, wind_factor_cal))
        calories = round((base_kcal + elev_kcal) * wind_factor_cal)

        results.append({
            'id': route['id'],
            'name': route['name'],
            'distance_km': round(route.get('distance', 0) / 1000, 1),
            'distance_from_you_km': round(dist_from_user, 1),
            'elevation_gain': round(route.get('elevation_gain', 0)),
            'fwd_score': round(fwd_score, 1),
            'return_score': round(return_score, 1),
            'best_direction': best_dir,
            'wind_label': label,
            'wind_icon': icon,
            'wind_speed': round(wind_speed, 1),
            'wind_direction': round(wind_dir),
            'wind_dir_label': bearing_label(wind_dir),
            'score_10': score_10,
            'meewind_pct': meewind_pct,
            'meewind_pct_alt': meewind_pct_alt,
            'calories': calories,
            'shelter_advice': shelter_advice,
            'map_points': [[round(p[0], 5), round(p[1], 5)] for p in oriented_pts],
            'segment_cos': segment_cos,
            'start_lat': oriented_pts[0][0],
            'start_lon': oriented_pts[0][1],
            'strava_url': route.get('_url', f"https://www.strava.com/activities/{route['id']}"),
            'item_type': route.get('_item_type', 'activity'),
            'item_id': route['id'],
        })

    results.sort(key=lambda x: x['score_10'], reverse=True)
    return jsonify(results)


# ── GPX export ────────────────────────────────────────────────────────────────

def build_gpx_from_streams(name, streams):
    """Build a minimal GPX string from Strava activity streams."""
    latlng = streams.get('latlng', {}).get('data', [])
    alts   = streams.get('altitude', {}).get('data', [])
    lines  = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="WindSplit" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f'  <trk><name>{name}</name><trkseg>',
    ]
    for i, (lat, lon) in enumerate(latlng):
        alt = alts[i] if i < len(alts) else 0
        lines.append(f'    <trkpt lat="{lat}" lon="{lon}"><ele>{alt:.1f}</ele></trkpt>')
    lines += ['  </trkseg></trk>', '</gpx>']
    return '\n'.join(lines)


@app.route('/gpx/<item_type>/<int:item_id>')
def download_gpx(item_type, item_id):
    from flask import Response
    access_token = session.get('access_token')
    if not access_token:
        return 'Niet ingelogd', 401

    hdrs = {'Authorization': f'Bearer {access_token}'}

    if item_type == 'route':
        resp = requests.get(
            f'https://www.strava.com/api/v3/routes/{item_id}/export_gpx',
            headers=hdrs, timeout=10
        )
        if resp.status_code == 200:
            return Response(
                resp.content,
                mimetype='application/gpx+xml',
                headers={'Content-Disposition': f'attachment; filename="route_{item_id}.gpx"'}
            )
        return f'Strava fout ({resp.status_code}): {resp.text[:200]}', 502
    else:  # activity
        a_resp = requests.get(
            f'https://www.strava.com/api/v3/activities/{item_id}',
            headers=hdrs, timeout=10
        )
        if not a_resp.ok:
            return f'Activiteit ophalen mislukt ({a_resp.status_code}): {a_resp.text[:200]}', 502
        name = a_resp.json().get('name', f'Activity {item_id}')

        s_resp = requests.get(
            f'https://www.strava.com/api/v3/activities/{item_id}/streams'
            f'?keys=latlng,altitude&key_by_type=true',
            headers=hdrs, timeout=10
        )
        if s_resp.status_code == 200:
            gpx = build_gpx_from_streams(name, s_resp.json())
            return Response(
                gpx,
                mimetype='application/gpx+xml',
                headers={'Content-Disposition': f'attachment; filename="rit_{item_id}.gpx"'}
            )
        return f'Streams ophalen mislukt ({s_resp.status_code}): {s_resp.text[:200]}', 502


@app.route('/api/segments')
def api_segments():
    access_token = session.get('access_token')
    if not access_token:
        return jsonify({'error': 'Niet ingelogd'}), 401

    try:
        user_lat = float(request.args.get('lat'))
        user_lon = float(request.args.get('lon'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Locatie ontbreekt'}), 400

    radius = float(request.args.get('radius', 20))

    start_dt = None
    start_time_str = request.args.get('start_time', '').strip()
    if start_time_str:
        try:
            start_dt = datetime.fromisoformat(start_time_str)
        except ValueError:
            pass

    # Bounding box rondom locatie
    delta_lat = radius / 111.0
    delta_lon = radius / (111.0 * math.cos(math.radians(user_lat)))
    bounds = f"{user_lat-delta_lat},{user_lon-delta_lon},{user_lat+delta_lat},{user_lon+delta_lon}"

    headers = {'Authorization': f'Bearer {access_token}'}
    resp = requests.get(
        f'https://www.strava.com/api/v3/segments/explore?bounds={bounds}&activity_type=riding',
        headers=headers, timeout=10
    )
    if resp.status_code != 200:
        return jsonify({'error': 'Kon segmenten niet ophalen van Strava'}), 502

    segments = resp.json().get('segments', [])
    if not segments:
        return jsonify([])

    wind_speed, wind_dir = get_wind(user_lat, user_lon, start_dt)

    results = []
    for seg in segments:
        start_ll = seg.get('start_latlng', [])
        end_ll   = seg.get('end_latlng', [])
        if len(start_ll) < 2 or len(end_ll) < 2:
            continue

        bearing = get_bearing(start_ll[0], start_ll[1], end_ll[0], end_ll[1])
        cos_val = -math.cos(math.radians(bearing - wind_dir))
        dist_from_user = haversine(user_lat, user_lon, start_ll[0], start_ll[1])

        MAX_WIND_REF = 20.0
        wind_factor = min(wind_speed / MAX_WIND_REF, 1.0)
        score_10 = 8.5 * (1 - wind_factor) + (5.0 + cos_val * 5.0) * wind_factor
        score_10 = round(max(5.0, min(10.0, score_10)), 1)

        # Polyline voor kaartweergave
        encoded = seg.get('points', '')
        try:
            map_pts = sample_points(polyline_lib.decode(encoded), n=20) if encoded else [start_ll, end_ll]
        except Exception:
            map_pts = [start_ll, end_ll]

        seg_cos = []
        for i in range(len(map_pts) - 1):
            b = get_bearing(map_pts[i][0], map_pts[i][1], map_pts[i+1][0], map_pts[i+1][1])
            seg_cos.append(round(-math.cos(math.radians(b - wind_dir)), 3))

        wind_boost = round(cos_val * wind_speed, 1)  # effectieve windcomponent in km/u

        if wind_boost >= 8:
            wind_txt, wind_icon = 'Sterke meewind', '💨✅'
        elif wind_boost >= 4:
            wind_txt, wind_icon = 'Meewind', '✅'
        elif wind_boost >= 1:
            wind_txt, wind_icon = 'Lichte meewind', '➡️'
        elif wind_boost >= -1:
            wind_txt, wind_icon = 'Zijwind', '↗️'
        else:
            wind_txt, wind_icon = 'Tegenwind', '⛔'

        # Haal segment details op: athlete_count + effort_count
        athlete_count = 0
        effort_count = 0
        try:
            det = requests.get(
                f'https://www.strava.com/api/v3/segments/{seg["id"]}',
                headers=headers, timeout=5
            )
            if det.status_code == 200:
                d = det.json()
                athlete_count = d.get('athlete_count', 0)
                effort_count  = d.get('effort_count', 0)
        except Exception:
            pass

        if athlete_count < 50:
            comp_label, comp_icon = 'Weinig concurrentie', '🟢'
        elif athlete_count < 300:
            comp_label, comp_icon = 'Gemiddeld', '🟡'
        elif athlete_count < 1000:
            comp_label, comp_icon = 'Veel concurrentie', '🟠'
        else:
            comp_label, comp_icon = 'Zwaar aangevochten', '🔴'

        # Kans combineert wind + concurrentie
        # wind_score: 0 (tegenwind) → 2 (sterke meewind)
        wind_score = 1 + min(max(cos_val, -1), 1)   # 0..2
        # comp_score: 2 (weinig) → 0 (veel)
        if athlete_count < 50:
            comp_score = 2
        elif athlete_count < 300:
            comp_score = 1.5
        elif athlete_count < 1000:
            comp_score = 0.8
        else:
            comp_score = 0.3
        combined = (wind_score * wind_factor + 1 * (1 - wind_factor)) * comp_score / 2

        if combined >= 1.4:
            kans = '🔥 Uitstekend'
        elif combined >= 1.0:
            kans = '✅ Goed'
        elif combined >= 0.6:
            kans = '➡️ Redelijk'
        else:
            kans = '⚠️ Laag'

        cat = seg.get('climb_category', 0)
        cat_label = ['', '4', '3', '2', '1', 'HC'][cat] if cat <= 5 else ''

        results.append({
            'id': seg['id'],
            'name': seg['name'],
            'distance_km': round(seg.get('distance', 0) / 1000, 2),
            'distance_from_you_km': round(dist_from_user, 1),
            'avg_grade': seg.get('avg_grade', 0),
            'climb_category': cat_label,
            'score_10': score_10,
            'cos_val': round(cos_val, 2),
            'wind_label': wind_txt,
            'wind_icon': wind_icon,
            'wind_boost': wind_boost,
            'kans': kans,
            'athlete_count': athlete_count,
            'effort_count': effort_count,
            'comp_label': comp_label,
            'comp_icon': comp_icon,
            'wind_speed': round(wind_speed, 1),
            'wind_direction': round(wind_dir),
            'wind_dir_label': bearing_label(wind_dir),
            'bearing_label': bearing_label(bearing),
            'strava_url': f'https://www.strava.com/segments/{seg["id"]}',
            'map_points': [[round(p[0], 5), round(p[1], 5)] for p in map_pts],
            'segment_cos': seg_cos,
        })

    results.sort(key=lambda x: x['score_10'], reverse=True)
    return jsonify(results)


@app.route('/api/osm-routes')
def api_osm_routes():
    if 'access_token' not in session:
        return jsonify({'error': 'Niet ingelogd'}), 401

    try:
        user_lat = float(request.args.get('lat'))
        user_lon = float(request.args.get('lon'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Locatie ontbreekt'}), 400

    radius = float(request.args.get('radius', 40))
    radius_m = int(radius * 1000)

    start_dt = None
    start_time_str = request.args.get('start_time', '').strip()
    if start_time_str:
        try:
            start_dt = datetime.fromisoformat(start_time_str)
        except ValueError:
            pass

    query = f"""
[out:json][timeout:30];
relation["type"="route"]["route"="bicycle"](around:{radius_m},{user_lat},{user_lon});
out geom;
"""
    try:
        resp = requests.post(
            'https://overpass-api.de/api/interpreter',
            data={'data': query},
            headers={'User-Agent': 'WindSplit/1.0 (fietsroute windanalyse; contact@windsplit.app)'},
            timeout=35
        )
        resp.raise_for_status()
        osm_data = resp.json()
    except Exception as e:
        return jsonify({'error': f'OpenStreetMap ophalen mislukt: {str(e)[:120]}'}), 502

    elements = osm_data.get('elements', [])
    if not elements:
        return jsonify({'error': 'Geen fietsroutes gevonden in dit gebied via OpenStreetMap'}), 404

    NETWORK_LABELS = {'ncn': 'Nationaal (LF)', 'rcn': 'Regionaal', 'lcn': 'Lokaal', 'mtb': 'MTB'}

    results = []
    for rel in elements:
        if rel.get('type') != 'relation':
            continue

        tags = rel.get('tags', {})
        name = tags.get('name') or tags.get('ref') or f"Route {rel['id']}"

        # Collect geometry from all member ways in listed order
        points = []
        for member in rel.get('members', []):
            geom = member.get('geometry', [])
            for pt in geom:
                points.append((pt['lat'], pt['lon']))

        if len(points) < 2:
            continue

        # Deduplicate consecutive identical points
        deduped = [points[0]]
        for p in points[1:]:
            if p != deduped[-1]:
                deduped.append(p)
        points = deduped

        # Total distance
        total_km = sum(
            haversine(points[i][0], points[i][1], points[i+1][0], points[i+1][1])
            for i in range(len(points) - 1)
        )
        if total_km < 5 or total_km > 220:
            continue

        start_lat, start_lon = points[0]
        dist_from_user = haversine(user_lat, user_lon, start_lat, start_lon)
        if dist_from_user > radius:
            continue

        sampled  = sample_points(points, n=8)
        map_pts  = sample_points(points, n=30)
        mid      = sampled[len(sampled) // 2]
        wind_speed, wind_dir = get_wind(mid[0], mid[1], start_dt)

        fwd_score = score_direction(sampled, wind_speed, wind_dir)

        mid_idx          = len(sampled) // 2
        first_half       = sampled[:mid_idx + 1]
        second_half      = sampled[mid_idx:]
        second_half_score    = score_direction(second_half, wind_speed, wind_dir)
        first_half_rev_score = score_direction(list(reversed(first_half)), wind_speed, wind_dir)

        if second_half_score >= first_half_rev_score:
            return_score = second_half_score
            best_dir = 'heen'
        else:
            return_score = first_half_rev_score
            best_dir = 'terug'

        def calc_meewind_pct(pts):
            cos_vals = []
            for i in range(len(pts) - 1):
                b = get_bearing(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])
                cos_vals.append(-math.cos(math.radians(b - wind_dir)))
            return round(100 * sum(1 for c in cos_vals if c > 0.1) / len(cos_vals)) if cos_vals else 50

        return_pts_best = second_half if best_dir == 'heen' else list(reversed(first_half))
        meewind_pct     = calc_meewind_pct(return_pts_best)
        return_pts_alt  = list(reversed(first_half)) if best_dir == 'heen' else second_half
        meewind_pct_alt = calc_meewind_pct(return_pts_alt)

        MAX_WIND_REF   = 20.0
        avg_cos_return = (return_score / wind_speed) if wind_speed > 0 else 0
        avg_cos_return = (avg_cos_return + 1) / 2
        avg_cos_return = max(0.0, min(1.0, avg_cos_return))
        wind_factor    = min(wind_speed / MAX_WIND_REF, 1.0)
        score_10       = 7.0 * (1 - wind_factor) + (3.0 + avg_cos_return * 7.0) * wind_factor
        score_10       = round(max(1.0, min(10.0, score_10)), 1)

        label, icon = wind_label(score_10, wind_speed)

        shelter_advice = None
        if wind_speed >= 15 and score_10 <= 6.5:
            shelter_advice = (
                '🌲 Weinig windvoordeel — overweeg een route door '
                'bos of bebouwing voor minder windweerstand.'
            )

        fwd_cos = []
        for i in range(len(map_pts) - 1):
            bearing = get_bearing(map_pts[i][0], map_pts[i][1],
                                  map_pts[i+1][0], map_pts[i+1][1])
            fwd_cos.append(round(-math.cos(math.radians(bearing - wind_dir)), 3))

        if best_dir == 'terug':
            oriented_pts  = list(reversed(map_pts))
            segment_cos   = [round(-c, 3) for c in reversed(fwd_cos)]
        else:
            oriented_pts  = map_pts
            segment_cos   = fwd_cos

        wind_factor_cal = 1 + (fwd_score / max(wind_speed, 1)) * 0.12 if wind_speed > 2 else 1.0
        wind_factor_cal = max(0.88, min(1.12, wind_factor_cal))
        calories        = round(total_km * 90 * 0.5 * wind_factor_cal)

        network       = tags.get('network', '')
        network_label = NETWORK_LABELS.get(network, 'Fietsroute')
        osm_url       = f"https://www.openstreetmap.org/relation/{rel['id']}"

        results.append({
            'id':                   rel['id'],
            'name':                 name,
            'distance_km':          round(total_km, 1),
            'distance_from_you_km': round(dist_from_user, 1),
            'elevation_gain':       0,
            'fwd_score':            round(fwd_score, 1),
            'return_score':         round(return_score, 1),
            'best_direction':       best_dir,
            'wind_label':           label,
            'wind_icon':            icon,
            'wind_speed':           round(wind_speed, 1),
            'wind_direction':       round(wind_dir),
            'wind_dir_label':       bearing_label(wind_dir),
            'score_10':             score_10,
            'meewind_pct':          meewind_pct,
            'meewind_pct_alt':      meewind_pct_alt,
            'calories':             calories,
            'shelter_advice':       shelter_advice,
            'map_points':           [[round(p[0], 5), round(p[1], 5)] for p in oriented_pts],
            'segment_cos':          segment_cos,
            'start_lat':            oriented_pts[0][0],
            'start_lon':            oriented_pts[0][1],
            'strava_url':           osm_url,
            'item_type':            'osm',
            'item_id':              rel['id'],
            'network_label':        network_label,
            'osm_url':              osm_url,
        })

    if not results:
        return jsonify({'error': 'Geen geschikte fietsroutes gevonden (te kort, te lang of buiten bereik)'}), 404

    results.sort(key=lambda x: x['score_10'], reverse=True)
    return jsonify(results[:30])


if __name__ == '__main__':
    app.run(debug=True, port=5001)
