import streamlit as st
import requests
from icalendar import Calendar
import pandas as pd
from datetime import datetime, timedelta, time
import pytz
import re
import difflib
from urllib.parse import urlparse, parse_qs
from streamlit_calendar import calendar

# --- CONFIGURATION ---
st.set_page_config(page_title="Calculateur ADE 35h (Promo)", page_icon="🎓", layout="wide")

st.markdown("""
    <style>
        .block-container { padding-top: 1rem; padding-bottom: 2rem; }
        [data-testid="stMetricValue"] { font-size: 1.5rem; }
    </style>
""", unsafe_allow_html=True)

# --- STATE ---
if 'added_blocks' not in st.session_state:
    st.session_state.added_blocks = []

# --- 0. EXTRACTION DATE URL ---

def get_monday_from_url(url):
    """Extrait la date 'firstDate' de l'URL ADE."""
    try:
        parsed_url = urlparse(url)
        params = parse_qs(parsed_url.query)
        date_str = params.get('firstDate', [None])[0]

        if date_str:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            return date_obj - timedelta(days=date_obj.weekday())
    except Exception:
        pass

    today = datetime.now()
    return today - timedelta(days=today.weekday())

# --- 1. ALGO DE DÉ-DOUBLONNAGE ---

def clean_title(title):
    patterns = [
        r'\bG\d\b', r'\bGr\s?[A-Z0-9]\b', r'\bGroupe\s?[A-Z0-9]\b',
        r'\bTP\b', r'\bTD\b', r'\bCM\b', r'\s-\s.*'
    ]
    cleaned = title.lower()
    for p in patterns:
        cleaned = re.sub(p, '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def are_similar(title1, title2):
    t1, t2 = clean_title(title1), clean_title(title2)
    return difflib.SequenceMatcher(None, t1, t2).ratio() > 0.8

def calculate_student_load(events):
    if not events: return 0, 0

    sorted_events = sorted(events, key=lambda x: x['Start'])
    kept_events = []
    skip_indices = set()

    for i in range(len(sorted_events)):
        if i in skip_indices: continue
        current = sorted_events[i]
        kept_events.append(current)

        for j in range(i + 1, len(sorted_events)):
            next_evt = sorted_events[j]
            time_diff = (next_evt['Start'] - current['Start']).total_seconds() / 3600

            if time_diff < 5.0:
                if are_similar(current['Title'], next_evt['Title']):
                    skip_indices.add(j)
            else:
                break

    kept_events.sort(key=lambda x: x['Start'])
    merged_for_count = []
    if kept_events:
        curr = kept_events[0]
        for nxt in kept_events[1:]:
            if nxt['Start'] < curr['End']:
                curr['End'] = max(curr['End'], nxt['End'])
            else:
                merged_for_count.append(curr)
                curr = nxt
        merged_for_count.append(curr)

    h_cours = 0
    h_pauses = 0
    for i, interval in enumerate(merged_for_count):
        dur = (interval['End'] - interval['Start']).total_seconds() / 3600
        h_cours += dur
        if i > 0:
            gap = (interval['Start'] - merged_for_count[i-1]['End']).total_seconds() / 60
            if 0 < gap <= 15: h_pauses += gap / 60

    return h_cours, h_pauses

# --- 2. ALGO DE TROUS COMMUNS ---

def get_common_holes(all_events, week_start_date, added_blocks):
    paris_tz = pytz.timezone('Europe/Paris')
    WORK_START = time(8, 0)
    WORK_END = time(18, 0)

    constraints = all_events.copy()
    for block in added_blocks:
        constraints.append({'Start': block['Start'], 'End': block['End']})

    if not constraints: return []

    constraints.sort(key=lambda x: x['Start'])
    merged_constraints = []
    if constraints:
        curr = constraints[0].copy()
        for nxt in constraints[1:]:
            if nxt['Start'] < curr['End']:
                curr['End'] = max(curr['End'], nxt['End'])
            else:
                merged_constraints.append(curr)
                curr = nxt.copy()
        merged_constraints.append(curr)

    suggestions = []
    constraints_by_day = {i: [] for i in range(5)}
    for evt in merged_constraints:
        if evt['Start'].weekday() < 5: constraints_by_day[evt['Start'].weekday()].append(evt)

    for day_idx in range(5):
        current_date = week_start_date + timedelta(days=day_idx)
        day_start = datetime.combine(current_date, WORK_START).replace(tzinfo=paris_tz)
        day_end = datetime.combine(current_date, WORK_END).replace(tzinfo=paris_tz)

        cursor = day_start
        day_constraints = constraints_by_day[day_idx]

        for c in day_constraints:
            if c['Start'] > cursor:
                gap = (c['Start'] - cursor).total_seconds() / 3600
                if gap >= 0.5 and cursor >= day_start:
                    suggestions.append({'Start': cursor, 'End': c['Start']})
            cursor = max(cursor, c['End'])

        if cursor < day_end:
            gap = (day_end - cursor).total_seconds() / 3600
            if gap >= 0.5:
                suggestions.append({'Start': cursor, 'End': day_end})

    return suggestions

def get_ade_data_raw(url, week_start_date):
    try:
        response = requests.get(url)
        response.raise_for_status()
        cal = Calendar.from_ical(response.text)
    except: return []

    utc = pytz.utc
    paris_tz = pytz.timezone('Europe/Paris')
    week_start = paris_tz.localize(datetime.combine(week_start_date, datetime.min.time()))
    week_end = week_start + timedelta(days=7)

    raw_events = []
    for event in cal.walk('vevent'):
        dtstart, dtend = event.get('dtstart').dt, event.get('dtend').dt
        summary = str(event.get('summary', ''))

        if not isinstance(dtstart, datetime): continue
        if dtstart.tzinfo is None: dtstart = utc.localize(dtstart)
        if dtend.tzinfo is None: dtend = utc.localize(dtend)
        start, end = dtstart.astimezone(paris_tz), dtend.astimezone(paris_tz)

        if start >= week_start and start < week_end:
            raw_events.append({'Start': start, 'End': end, 'Title': summary})

    return raw_events

# --- INTERFACE ---
st.title("🎓 Planificateur 35h (Vue Promo)")

col_calendar, col_controls = st.columns([3, 1], gap="medium")

# --- CONTRÔLES ---
with col_controls:
    with st.container(border=True):
        st.subheader("Configuration")

        default_url = "https://ade-uga-ro-vs.grenet.fr/jsp/custom/modules/plannings/anonymous_cal.jsp?resources=54303,55713,55613,54994,54320,54315,55542,55467,55000,65286,46667&projectId=1&calType=ical&firstDate=2026-03-09&lastDate=2026-03-15"

        # Le simple fait de changer ce champ relance tout le script
        url_ade = st.text_input("Lien ADE", value=default_url, label_visibility="collapsed")

        if url_ade:
            target_monday = get_monday_from_url(url_ade)
            st.caption(f"📅 Semaine du **{target_monday.strftime('%d/%m/%Y')}**")
        else:
            today = datetime.now()
            target_monday = today - timedelta(days=today.weekday())

    if url_ade:
        # 1. Récupération
        all_events = get_ade_data_raw(url_ade, target_monday)

        # 2. Calculs
        h_cours, h_pauses = calculate_student_load(all_events)
        h_ajoutees = sum([(b['End'] - b['Start']).total_seconds()/3600 for b in st.session_state.added_blocks])
        total = h_cours + h_pauses + h_ajoutees
        reste = max(0, 35 - total)
    else:
        h_cours, h_pauses, h_ajoutees, total, reste = 0, 0, 0, 0, 35
        all_events = []

    # --- BILAN ---
    with st.container(border=True):
        st.subheader("Bilan Promo")

        c1, c2 = st.columns(2)
        c1.metric("Cours (Est.)", f"{h_cours+h_pauses:.1f}h")
        c2.metric("Ajouts", f"{h_ajoutees:.1f}h")

        st.divider()

        # --- MODIFICATION ICI : Colonnes pour Total et Manque ---
        c_tot, c_rem = st.columns([1, 1.2]) # On sépare la ligne en 2

        with c_tot:
            st.metric("Total / Étudiant", f"{total:.2f}h")

        with c_rem:
            # On affiche l'alerte ou le succès à côté
            if reste > 0.01:
                st.warning(f"Manque **{reste:.2f}h**")
            else:
                st.success("✅ 35h atteintes")
        # --------------------------------------------------------

# --- CALENDRIER ---
with col_calendar:
    if url_ade:
        calendar_events = []

        # A. Cours (Bleu)
        for evt in all_events:
            calendar_events.append({
                "title": evt['Title'],
                "start": evt['Start'].isoformat(),
                "end": evt['End'].isoformat(),
                "backgroundColor": "#3b82f6",
                "borderColor": "#1e40af",
                "extendedProps": {"type": "cours"}
            })

        # B. Ajouts (Orange)
        for i, evt in enumerate(st.session_state.added_blocks):
            calendar_events.append({
                "id": f"added_{i}",
                "title": "Entreprise",
                "start": evt['Start'].isoformat(),
                "end": evt['End'].isoformat(),
                "backgroundColor": "#f97316",
                "borderColor": "#c2410c",
                "extendedProps": {"type": "ajout", "index": i}
            })

        # C. Suggestions (Vert)
        if reste > 0.01:
            suggestions = get_common_holes(all_events, target_monday, st.session_state.added_blocks)
            for i, s in enumerate(suggestions):
                available_dur = (s['End'] - s['Start']).total_seconds() / 3600
                to_take = min(available_dur, reste)
                visual_end = s['Start'] + timedelta(hours=to_take)

                calendar_events.append({
                    "id": f"sugg_{i}",
                    "title": f"Libre Promo (+{to_take:.2f}h)",
                    "start": s['Start'].isoformat(),
                    "end": visual_end.isoformat(),
                    "backgroundColor": "#22c55e",
                    "borderColor": "#15803d",
                    "extendedProps": {
                        "type": "suggestion",
                        "to_take": to_take,
                        "start_iso": s['Start'].isoformat()
                    }
                })

        calendar_options = {
            "editable": False,
            "selectable": False,
            "weekends": False,
            "headerToolbar": {"left": "title", "center": "", "right": ""},
            "initialView": "timeGridWeek",
            "initialDate": target_monday.strftime("%Y-%m-%d"),
            "slotMinTime": "08:00:00",
            "slotMaxTime": "19:00:00",
            "allDaySlot": False,
            "locale": "fr",
            "height": 750,
        }

        cal = calendar(events=calendar_events, options=calendar_options, custom_css=".fc-event-title { font-weight: bold; }")

        if cal.get("eventClick"):
            props = cal["eventClick"]["event"]["extendedProps"]
            if props.get("type") == "suggestion":
                to_take = props["to_take"]
                start_dt = datetime.fromisoformat(props["start_iso"])
                st.session_state.added_blocks.append({
                    'Start': start_dt,
                    'End': start_dt + timedelta(hours=to_take)
                })
                st.rerun()

            elif props.get("type") == "ajout":
                idx = props["index"]
                if 0 <= idx < len(st.session_state.added_blocks):
                    st.session_state.added_blocks.pop(idx)
                    st.rerun()
    else:
        st.info("👈 Entrez l'URL ADE à droite.")