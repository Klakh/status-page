#!/usr/bin/env python3
import urllib.request
import json
import time
import os
import subprocess
from datetime import datetime, date, timedelta

# --- CONFIGURATION DES SERVICES ---
SERVICES = [
    {
        "id": "ktv",
        "name": "k.tv",
        "url": "https://example.com",
        "icon": "https://example.com/img/icons/ktv.webp"
    },
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
OUTPUT_HTML = os.path.join(BASE_DIR, "index.html")

def check_service(url, timeout=5):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'PiStatusMonitor/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def clean_old_history(history):
    today = date.today()
    cutoff = (today - timedelta(days=95)).strftime("%Y-%m-%d")
    return {d: v for d, v in history.items() if d >= cutoff}

def generate_html(services_data, global_status, history_data, record_data):
    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M:%S")
    history_json = json.dumps(history_data)
    
    global_banner_class = "up" if global_status else "down"
    global_banner_text = "Tous les systèmes sont opérationnels" if global_status else "Perturbation sur un ou plusieurs services"

    # Formatage de l'affichage du record
    rec_name = record_data.get("name", "N/A")
    rec_start_ts = record_data.get("start_ts", 0)
    rec_end_ts = record_data.get("end_ts")
    
    start_dt_str = datetime.fromtimestamp(rec_start_ts).strftime("%d/%m/%Y") if rec_start_ts else "--"
    end_dt_str = datetime.fromtimestamp(rec_end_ts).strftime("%d/%m/%Y à %H:%M") if rec_end_ts else "maintenant"

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>État des Services — Keeklah</title>
    <style>
        :root {{
            --bg: #0d1117;
            --card-bg: #161b22;
            --border: #30363d;
            --text-main: #f0f6fc;
            --text-muted: #8b949e;
            --up: #238636;
            --down: #da3633;
            --partial: #d29922;
            --accent-gold: #f2cc60;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg);
            color: var(--text-main);
            padding: 2rem 1rem;
            display: flex;
            justify-content: center;
        }}
        .container {{ width: 100%; max-width: 760px; }}
        
        .header {{ text-align: center; margin-bottom: 1.5rem; }}
        .header h1 {{ font-size: 1.75rem; font-weight: 700; margin-bottom: 0.5rem; }}
        .header p {{ color: var(--text-muted); font-size: 0.875rem; }}

        .record-card {{
            background: rgba(210, 153, 34, 0.08);
            border: 1px solid rgba(210, 153, 34, 0.3);
            border-radius: 8px;
            padding: 0.85rem 1.25rem;
            margin-bottom: 1.5rem;
            font-size: 0.875rem;
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }}
        .record-icon {{ font-size: 1.25rem; }}
        .record-content {{ line-height: 1.4; color: var(--text-main); }}
        .record-content strong {{ color: var(--accent-gold); }}

        .global-banner {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.75rem;
            padding: 0.85rem;
            border-radius: 8px;
            font-weight: 600;
            margin-bottom: 2rem;
            border: 1px solid transparent;
        }}
        .global-banner.up {{ background: rgba(35, 134, 54, 0.15); border-color: var(--up); color: #3fb950; }}
        .global-banner.down {{ background: rgba(218, 54, 51, 0.15); border-color: var(--down); color: #f85149; }}
        
        .controls {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
            flex-wrap: wrap;
            gap: 1rem;
        }}
        .timeframe-selector {{
            display: flex;
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 3px;
        }}
        .tf-btn {{
            background: none;
            border: none;
            color: var(--text-muted);
            padding: 0.35rem 0.75rem;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            border-radius: 4px;
            transition: all 0.2s;
        }}
        .tf-btn.active {{ background: #21262d; color: var(--text-main); }}

        .service-card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1.25rem;
            margin-bottom: 1.25rem;
        }}
        .service-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.75rem;
        }}
        .service-title {{
            display: flex;
            align-items: center;
            gap: 0.6rem;
            font-weight: 600;
            font-size: 1.1rem;
            text-decoration: none;
            color: var(--text-main);
        }}
        .service-icon {{
            width: 22px;
            height: 22px;
            border-radius: 4px;
            object-fit: contain;
        }}
        .service-meta {{ display: flex; align-items: center; gap: 0.75rem; }}
        .badge {{
            font-size: 0.75rem;
            font-weight: 700;
            padding: 0.25rem 0.6rem;
            border-radius: 12px;
            text-transform: uppercase;
        }}
        .badge.up {{ background: rgba(35, 134, 54, 0.2); color: #3fb950; border: 1px solid var(--up); }}
        .badge.down {{ background: rgba(218, 54, 51, 0.2); color: #f85149; border: 1px solid var(--down); }}

        .timer-info {{ font-size: 0.8rem; color: var(--text-muted); margin-bottom: 1rem; }}
        
        .history-bars {{
            display: flex;
            gap: 3px;
            height: 28px;
            align-items: flex-end;
            margin-bottom: 0.5rem;
        }}
        .bar {{
            flex: 1;
            height: 100%;
            border-radius: 2px;
            background-color: var(--up);
            position: relative;
            cursor: pointer;
            transition: opacity 0.15s;
        }}
        .bar:hover {{ opacity: 0.8; }}
        .bar.down {{ background-color: var(--down); }}
        .bar.partial {{ background-color: var(--partial); }}
        .bar.nodata {{ background-color: #21262d; }}

        .history-legend {{
            display: flex;
            justify-content: space-between;
            font-size: 0.75rem;
            color: var(--text-muted);
        }}

        .tooltip {{
            position: absolute;
            bottom: 35px;
            left: 50%;
            transform: translateX(-50%);
            background: #21262d;
            color: var(--text-main);
            border: 1px solid var(--border);
            padding: 0.4rem 0.6rem;
            border-radius: 4px;
            font-size: 0.75rem;
            white-space: nowrap;
            pointer-events: none;
            display: none;
            z-index: 10;
            box-shadow: 0 4px 12px rgba(0,0,0,0.5);
        }}
        .bar:hover .tooltip {{ display: block; }}

        .footer {{
            text-align: center;
            margin-top: 2rem;
            font-size: 0.75rem;
            color: var(--text-muted);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Statut des Services</h1>
            <p>Dernière vérification : {now_str}</p>
        </div>

        <div class="record-card">
            <span class="record-icon">🏆</span>
            <div class="record-content">
                Service en ligne le plus longtemps : <strong>{rec_name}</strong>, 
                up pendant <strong id="record-timer" data-start="{rec_start_ts}" data-end="{rec_end_ts if rec_end_ts else ''}">--</strong>, 
                du {start_dt_str} à {end_dt_str}.
            </div>
        </div>

        <div class="global-banner {global_banner_class}">
            {global_banner_text}
        </div>

        <div class="controls">
            <span style="font-size: 0.9rem; font-weight: 600; color: var(--text-muted);">Historique de disponibilité</span>
            <div class="timeframe-selector">
                <button class="tf-btn" onclick="setTimeframe(7)">7j</button>
                <button class="tf-btn" onclick="setTimeframe(30)">30j</button>
                <button class="tf-btn active" onclick="setTimeframe(90)">90j</button>
            </div>
        </div>

        <div id="services-list">
"""
    for s in services_data:
        status_class = "up" if s["status"] == "UP" else "down"
        status_label = "En ligne" if s["status"] == "UP" else "Inaccessible"
        timer_prefix = "En ligne depuis :" if s["status"] == "UP" else "Hors ligne depuis :"
        
        html += f"""
            <div class="service-card" data-service-id="{s['id']}">
                <div class="service-header">
                    <a href="{s['url']}" target="_blank" class="service-title">
                        <img src="{s['icon']}" alt="" class="service-icon" onerror="this.style.display='none'">
                        <span>{s['name']} ↗</span>
                    </a>
                    <div class="service-meta">
                        <span class="uptime-pct" id="uptime-{s['id']}" style="font-size: 0.85rem; font-weight: 600; color: var(--text-muted);">100%</span>
                        <span class="badge {status_class}">{status_label}</span>
                    </div>
                </div>
                <div class="timer-info">{timer_prefix} <strong data-since="{s['last_change']}">--</strong></div>
                <div class="history-bars" id="bars-{s['id']}"></div>
                <div class="history-legend">
                    <span id="legend-start-{s['id']}">--</span>
                    <span>Aujourd'hui</span>
                </div>
            </div>"""

    html += f"""
        </div>
        <div class="footer">Monitoring autonome par Raspberry Pi 1 B+</div>
    </div>

    <script>
        const rawHistory = {history_json};
        let currentDays = 90;

        function formatDuration(diff) {{
            const days = Math.floor(diff / 86400);
            const hours = Math.floor((diff % 86400) / 3600);
            const mins = Math.floor((diff % 3600) / 60);
            const secs = diff % 60;
            
            let res = "";
            if (days > 0) res += days + "j ";
            if (hours > 0 || days > 0) res += hours + "h ";
            res += mins + "m " + secs + "s";
            return res;
        }}

        function setTimeframe(days) {{
            currentDays = days;
            document.querySelectorAll('.tf-btn').forEach(btn => {{
                btn.classList.toggle('active', btn.innerText === days + 'j');
            }});
            renderHistory();
        }}

        function renderHistory() {{
            const today = new Date();
            
            document.querySelectorAll('.service-card').forEach(card => {{
                const sid = card.getAttribute('data-service-id');
                const container = document.getElementById('bars-' + sid);
                const legendStart = document.getElementById('legend-start-' + sid);
                const uptimeLabel = document.getElementById('uptime-' + sid);
                
                container.innerHTML = '';
                legendStart.innerText = "Il y a " + currentDays + " jours";

                const sHistory = rawHistory[sid] || {{}};
                let totalUp = 0;
                let totalChecks = 0;

                for (let i = currentDays - 1; i >= 0; i--) {{
                    const d = new Date();
                    d.setDate(today.getDate() - i);
                    const dateStr = d.toISOString().split('T')[0];
                    const formattedDate = d.toLocaleDateString('fr-FR', {{ day: 'numeric', month: 'long', year: 'numeric' }});

                    const dayData = sHistory[dateStr] || {{ up: 0, total: 0 }};
                    const bar = document.createElement('div');
                    bar.className = 'bar';

                    if (dayData.total === 0) {{
                        bar.classList.add('nodata');
                        bar.innerHTML = `<div class="tooltip">${{formattedDate}}<br>Aucune donnée</div>`;
                    }} else {{
                        totalUp += dayData.up;
                        totalChecks += dayData.total;
                        const pct = Math.round((dayData.up / dayData.total) * 100);
                        
                        if (pct < 95 && pct > 0) bar.classList.add('partial');
                        else if (pct === 0) bar.classList.add('down');

                        bar.innerHTML = `<div class="tooltip">${{formattedDate}}<br><strong>${{pct}}% d'uptime</strong> (${{dayData.up}}/${{dayData.total}} checks)</div>`;
                    }}
                    container.appendChild(bar);
                }}

                const globalPct = totalChecks > 0 ? ((totalUp / totalChecks) * 100).toFixed(1) : "100.0";
                uptimeLabel.innerText = globalPct + "%";
            }});
        }}

        function updateTimers() {{
            const now = Math.floor(Date.now() / 1000);
            
            // Mis à jour des compteurs individuels par service
            document.querySelectorAll('[data-since]').forEach(el => {{
                const since = parseInt(el.getAttribute('data-since'));
                const diff = Math.max(0, now - since);
                el.innerText = formatDuration(diff);
            }});

            // Mis à jour du compteur du record global
            const recEl = document.getElementById('record-timer');
            if (recEl) {{
                const start = parseInt(recEl.getAttribute('data-start'));
                const endAttr = recEl.getAttribute('data-end');
                const end = endAttr ? parseInt(endAttr) : now;
                const diff = Math.max(0, end - start);
                recEl.innerText = formatDuration(diff);
            }}
        }}

        setInterval(updateTimers, 1000);
        updateTimers();
        renderHistory();
    </script>
</body>
</html>
"""
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

def main():
    now_ts = int(time.time())
    today_str = date.today().strftime("%Y-%m-%d")
    
    old_state = load_state()
    history = clean_old_history(old_state.get("history", {}))
    record = old_state.get("record", {})
    
    new_state = {}
    has_changed = False
    all_up = True
    services_output = []

    for s in SERVICES:
        sid = s["id"]
        is_up = check_service(s["url"])
        if not is_up:
            all_up = False
            
        status_str = "UP" if is_up else "DOWN"
        
        prev = old_state.get("services", {}).get(sid, {})
        prev_status = prev.get("status")
        last_change = prev.get("last_change", now_ts)

        if prev_status != status_str:
            has_changed = True
            
            # Si le service vient de tomber (UP -> DOWN), on vérifie si sa session bat le record
            if prev_status == "UP":
                finished_dur = now_ts - last_change
                if finished_dur >= record.get("duration", 0):
                    record = {
                        "name": s["name"],
                        "start_ts": last_change,
                        "end_ts": now_ts,
                        "duration": finished_dur
                    }
            
            last_change = now_ts

        new_state[sid] = {
            "status": status_str,
            "last_change": last_change,
            "last_check": now_ts
        }

        # Mise à jour de l'historique quotidien
        if sid not in history:
            history[sid] = {}
        if today_str not in history[sid]:
            history[sid][today_str] = {"up": 0, "total": 0}

        history[sid][today_str]["total"] += 1
        if is_up:
            history[sid][today_str]["up"] += 1

        services_output.append({
            "id": sid,
            "name": s["name"],
            "url": s["url"],
            "icon": s.get("icon", ""),
            "status": status_str,
            "last_change": last_change
        })

    # Évaluation des records en cours pour tous les services actuellement UP
    for s in SERVICES:
        sid = s["id"]
        if new_state[sid]["status"] == "UP":
            start_ts = new_state[sid]["last_change"]
            current_dur = now_ts - start_ts
            
            # Si le service qui détient le record est celui-ci et qu'il est toujours en cours
            if record.get("name") == s["name"] and record.get("end_ts") is None:
                record["duration"] = current_dur
                record["start_ts"] = start_ts
            elif current_dur > record.get("duration", 0):
                record = {
                    "name": s["name"],
                    "start_ts": start_ts,
                    "end_ts": None,
                    "duration": current_dur
                }

    # Sauvegarde globale dans le JSON
    save_data = {
        "services": new_state,
        "history": history,
        "record": record,
        "_meta": old_state.get("_meta", {})
    }

    # Génération du fichier HTML complet
    generate_html(services_output, all_up, history, record)

    # Stratégie de push Git
    last_push = old_state.get("_meta", {}).get("last_push", 0)
    should_push = has_changed or (now_ts - last_push >= 3600)

    if should_push:
        save_data["_meta"]["last_push"] = now_ts
        save_state(save_data)
        
        try:
            subprocess.run(["git", "-C", BASE_DIR, "add", "."], check=True)
            msg = "Alerte : Changement d'état" if has_changed else "Mise à jour automatique historique"
            subprocess.run(["git", "-C", BASE_DIR, "commit", "-m", msg], check=True)
            subprocess.run(["git", "-C", BASE_DIR, "push", "origin", "main"], check=True)
            print(f"[{datetime.now()}] Git push effectué ({msg})")
        except Exception as e:
            print(f"[{datetime.now()}] Erreur Git : {e}")
    else:
        save_state(save_data)

if __name__ == "__main__":
    main()
