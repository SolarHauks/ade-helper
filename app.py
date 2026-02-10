import streamlit as st
import requests
from icalendar import Calendar
from datetime import datetime, timedelta, time
import pytz
import re
import difflib
from collections import Counter
from urllib.parse import urlparse, parse_qs, quote
from streamlit_calendar import calendar

# --- CONFIGURATION DES FORMATIONS ---
# Ici, on ne stocke que les IDs (la partie après "resources=")
# Vous pourrez rajouter les autres formations de votre responsable ici.
RESSOURCES_PROMOS = {
    "M1 MIAGE": "54303,55713,55613,54994,54320,54315,55542,55467,55000,65286,46667",
    "M2 MIAGE (Exemple)": "12345,67890", # À remplacer par les vrais IDs
    "L3 MIAGE (Exemple)": "11111,22222", # À remplacer
}

BASE_URL_ADE = "https://ade-uga-ro-vs.grenet.fr/jsp/custom/modules/plannings/anonymous_cal.jsp"

st.set_page_config(page_title="Calculateur ADE 35h (Promo)", page_icon="🎓", layout="wide")

# --- CSS PERSONNALISÉ ---
st.markdown("""
    <style>
        .block-container { padding-top: 1rem; padding-bottom: 2rem; }
        [data-testid="stMetricValue"] { font-size: 1.5rem; }
        
        .mailto-button {
            display: inline-block;
            padding: 0.6em 1em;
            color: white !important;
            background-color: #22c55e;
            text-decoration: none !important;
            border-radius: 8px;
            font-weight: bold;
            text-align: center;
            width: 100%;
            margin-top: 15px;
            margin-bottom: 15px;
            border: 1px solid #22c55e;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: all 0.3s ease;
        }
        .mailto-button:hover {
            background-color: #16a34a;
            border-color: #16a34a;
            transform: translateY(-1px);
            box-shadow: 0 4px 6px rgba(0,0,0,0.15);
        }

        .mailto-button-disabled {
            display: inline-block;
            padding: 0.6em 1em;
            color: #9ca3af !important;
            background-color: #e5e7eb;
            text-decoration: none !important;
            border-radius: 8px;
            font-weight: bold;
            text-align: center;
            width: 100%;
            margin-top: 15px;
            margin-bottom: 15px;
            border: 1px solid #d1d5db;
            cursor: not-allowed;
            pointer-events: none;
        }
    </style>
""", unsafe_allow_html=True)

# --- STATE ---
if 'added_blocks' not in st.session_state:
    st.session_state.added_blocks = []

# --- 0. FONCTIONS UTILES ---

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

def get_formation_name(events):
    if not events: return "Formation"
    lines = []
    for evt in events:
        desc = evt.get('Description', '')
        if desc:
            parts = [l.strip() for l in desc.split('\n') if l.strip()]
            if parts: lines.append(parts[0])

    if not lines: return "Formation"
    tokenized_lines = [line.split() for line in lines]
    if not any(tokenized_lines): return "Formation"

    common_words = []
    max_len = max(len(l) for l in tokenized_lines)
    total_lines = len(lines)
    THRESHOLD = 0.7

    for i in range(max_len):
        words_at_index = [line[i] for line in tokenized_lines if len(line) > i]
        if not words_at_index: break
        most_common, count = Counter(words_at_index).most_common(1)[0]
        if (count / total_lines) > THRESHOLD:
            common_words.append(most_common)
        else:
            break

    if common_words:
        return " ".join(common_words).strip(" -_")

    return Counter(lines).most_common(1)[0][0]

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
        description = str(event.get('description', ''))

        if not isinstance(dtstart, datetime): continue
        if dtstart.tzinfo is None: dtstart = utc.localize(dtstart)
        if dtend.tzinfo is None: dtend = utc.localize(dtend)
        start, end = dtstart.astimezone(paris_tz), dtend.astimezone(paris_tz)

        if start >= week_start and start < week_end:
            raw_events.append({
                'Start': start,
                'End': end,
                'Title': summary,
                'Description': description
            })

    return raw_events

# --- INTERFACE ---
st.title("🎓 Planificateur 35h (Vue Promo)")

col_calendar, col_controls = st.columns([3, 1], gap="medium")

# --- CONTRÔLES ---
with col_controls:
    with st.container(border=True):
        st.subheader("Configuration")

        # 1. Menu Déroulant des Promos
        options_promos = list(RESSOURCES_PROMOS.keys()) + ["🔗 URL Personnalisée"]
        choix_promo = st.selectbox("Formation", options=options_promos)

        # 2. Sélecteur de Date
        # On calcule le Lundi de la semaine actuelle par défaut
        today = datetime.now()
        monday_current = today - timedelta(days=today.weekday())

        target_monday_date = st.date_input(
            "Semaine du (Lundi)",
            value=monday_current,
            format="DD/MM/YYYY"
        )

        # On force la date à être un Lundi si l'utilisateur choisit un autre jour
        target_monday = datetime.combine(target_monday_date - timedelta(days=target_monday_date.weekday()), datetime.min.time())

        # 3. Construction de l'URL
        url_ade = None

        if choix_promo == "🔗 URL Personnalisée":
            url_ade = st.text_input("Coller l'URL ADE complète ici")
            # Si URL collée, on met à jour la date cible pour matcher l'URL (si possible)
            if url_ade:
                try:
                    parsed = urlparse(url_ade)
                    d_str = parse_qs(parsed.query).get('firstDate', [None])[0]
                    if d_str:
                        d_obj = datetime.strptime(d_str, "%Y-%m-%d")
                        target_monday = d_obj - timedelta(days=d_obj.weekday())
                except: pass
        else:
            # Construction dynamique
            resources_ids = RESSOURCES_PROMOS[choix_promo]

            # Formatage des dates pour ADE (YYYY-MM-DD)
            date_debut = target_monday.strftime("%Y-%m-%d")
            date_fin = (target_monday + timedelta(days=6)).strftime("%Y-%m-%d") # Dimanche

            # Assemblage
            url_ade = f"{BASE_URL_ADE}?resources={resources_ids}&projectId=1&calType=ical&firstDate={date_debut}&lastDate={date_fin}"

            # Debug (optionnel, pour vérifier l'url générée)
            # st.caption(f"Code ressources : `{resources_ids[:10]}...`")

        st.info(f"Semaine du **{target_monday.strftime('%d/%m/%Y')}**")

        # 4. Paramètre Objectif Hebdo (Bonus : pour jours fériés)
        objectif_hebdo = st.number_input("Objectif Hebdo", min_value=0.0, max_value=45.0, value=35.0, step=1.0)


    if url_ade:
        # 1. Récupération
        all_events = get_ade_data_raw(url_ade, target_monday)

        # 2. Identification Formation
        formation_name = get_formation_name(all_events)

        # 3. Calculs
        h_cours, h_pauses = calculate_student_load(all_events)
        h_ajoutees = sum([(b['End'] - b['Start']).total_seconds()/3600 for b in st.session_state.added_blocks])
        total = h_cours + h_pauses + h_ajoutees

        # Calcul du reste basé sur l'objectif variable
        reste = max(0, objectif_hebdo - total)
    else:
        h_cours, h_pauses, h_ajoutees, total, reste = 0, 0, 0, 0, 35
        all_events = []

    # --- BILAN & MAIL ---
    with st.container(border=True):
        st.subheader("Bilan")

        st.caption(f"🏷️ {formation_name}")

        c1, c2 = st.columns(2)
        c1.metric("Cours", f"{h_cours+h_pauses:.1f}h")
        c2.metric("Ajouts", f"{h_ajoutees:.1f}h")

        st.divider()

        c_tot, c_rem = st.columns([1, 1.2])

        with c_tot:
            st.metric("Total", f"{total:.2f}h")

        with c_rem:
            if reste > 0.01:
                st.warning(f"Manque **{reste:.2f}h**")
            else:
                st.success(f"✅ Objectif {objectif_hebdo}h OK")

        # --- GÉNÉRATION DU MAIL ---
        if url_ade:
            if reste <= 0.01:
                subject = f"Saisie heures complémentaires [{formation_name}] - Semaine du {target_monday.strftime('%d/%m/%Y')}"

                body = f"Bonjour,\n\nConcernant la formation {formation_name}, voici les créneaux complémentaires à saisir pour la semaine du {target_monday.strftime('%d/%m/%Y')} :\n\n"

                if st.session_state.added_blocks:
                    sorted_blocks = sorted(st.session_state.added_blocks, key=lambda x: x['Start'])
                    for block in sorted_blocks:
                        jour = block['Start'].strftime('%d/%m')
                        debut = block['Start'].strftime('%Hh%M')
                        fin = block['End'].strftime('%Hh%M')
                        duree = (block['End'] - block['Start']).total_seconds() / 3600
                        body += f"- Le {jour} : {debut} - {fin} ({duree:.2f}h)\n"
                else:
                    body += f"Aucune heure supplémentaire à saisir cette semaine ({objectif_hebdo}h atteintes via ADE).\n"

                body += "\nCordialement."

                recipient = "ajpbordes@gmail.com"
                mailto_link = f"mailto:{recipient}?subject={quote(subject)}&body={quote(body)}"

                st.markdown(f"""
                    <a href="{mailto_link}" target="_blank" class="mailto-button">
                        ✉️ Envoyer pour saisie
                    </a>
                """, unsafe_allow_html=True)

            else:
                st.markdown(f"""
                    <a class="mailto-button-disabled">
                         🚫 Incomplet (Total < {objectif_hebdo}h)
                    </a>
                """, unsafe_allow_html=True)

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
        st.info("👈 Configurez la promo et la date à droite.")