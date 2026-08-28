#!/usr/bin/env python3
import urllib.request
import json
import time
import os
import subprocess
from datetime import datetime, timedelta

# --- CONFIGURATION DES SERVICES ---
SERVICES = [
    {
        "id": "ktv",
        "name": "K.tv",
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

def clean_old_history(history, now_ts):
    cutoff = now_ts - (180 * 86400)
    cleaned = {}
    for sid, slots in history.items():
        cleaned[sid] = {}
        for ts_str, val in slots.items():
            try:
                if int(ts_str) >= cutoff:
                    cleaned[sid][ts_str] = val
            except ValueError:
                pass
    return cleaned

def generate_html(services_data, global_status, history_data, record_data):
    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M:%S")
    history_json = json.dumps(history_data)
    
    global_banner_class = "up" if global_status else "down"
    global_banner_text = "Tous les systèmes sont opérationnels" if global_status else "Perturbation sur un ou plusieurs services"

    rec_name = record_data.get("name")
    rec_start_ts = record_data.get("start_ts")
    rec_end_ts = record_data.get("end_ts")
    
    has_valid_record = rec_name and rec_start_ts and rec_start_ts > 0
    start_dt_str = datetime.fromtimestamp(rec_start_ts).strftime("%d/%m/%Y à %H:%M") if has_valid_record else "--"
    end_dt_str = datetime.fromtimestamp(rec_end_ts).strftime("%d/%m/%Y à %H:%M") if (has_valid_record and rec_end_ts) else "maintenant"

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
            padding: 0.35rem 0.65rem;
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
        .stats-summary {{
            font-size: 0.825rem;
            font-weight: 600;
            color: var(--text-muted);
            text-align: right;
        }}
        .downtime-tag {{
            font-size: 0.75rem;
            color: var(--down);
            margin-left: 0.4rem;
        }}
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
            gap: 2px;
            height: 28px;
            align-items: flex-end;
            margin-bottom: 0.5rem;
        }}
        .bar {{
            flex: 1;
            height: 100%;
            border-radius: 1.5px;
            background-color: var(--up);
            position: relative;
            cursor: pointer;
            transition: opacity 0.15s;
        }}
        .bar:hover {{ opacity: 0.75; }}
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
            z-index: 20;
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
                Service en ligne le plus longtemps : <strong>{rec_name if has_valid_record else 'Initialisation...'}</strong>
                {" , up pendant <strong id=\"record-timer\" data-start=\"" + str(rec_start_ts) + "\" data-end=\"" + (str(rec_end_ts) if rec_end_ts else "") + "\">--</strong>, du " + start_dt_str + " à " + end_dt_str + "." if has_valid_record else "."}
            </div>
        </div>

        <div class="global-banner {global_banner_class}">
            {global_banner_text}
        </div>

        <div class="controls">
            <span style="font-size: 0.9rem; font-weight: 600; color: var(--text-muted);">Historique (60 bâtonnets)</span>
            <div class="timeframe-selector">
                <button class="tf-btn" onclick="setTimeframe('5h')">5h</button>
                <button class="tf-btn" onclick="setTimeframe('1j')">1j</button>
                <button class="tf-btn" onclick="setTimeframe('7j')">7j</button>
                <button class="tf-btn" onclick="setTimeframe('30j')">30j</button>
                <button class="tf-btn active" onclick="setTimeframe('90j')">90j</button>
                <button class="tf-btn" onclick="setTimeframe('tout')">Tout</button>
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
                        <div class="stats-summary" id="stats-{s['id']}">
                            <span class="uptime-pct">100%</span>
                        </div>
                        <span class="badge {status_class}">{status_label}</span>
                    </div>
                </div>
                <div class="timer-info">{timer_prefix} <strong data-since="{s['last_change']}">--</strong></div>
                <div class="history-bars" id="bars-{s['id']}"></div>
                <div class="history-legend">
                    <span id="legend-start-{s['id']}">--</span>
                    <span>Maintenant</span>
                </div>
            </div>"""

    html += f"""
        </div>
        <div class="footer">Monitoring autonome par Raspberry Pi 1 B+</div>
    </div>

    <script>
        const rawHistory = {history_json};
        const BARS_COUNT = 60;
        let currentTf = '90j';

        function formatDuration(diff) {{
            if (diff <= 0) return "0s";
            const days = Math.floor(diff / 86400);
            const hours = Math.floor((diff % 86400) / 3600);
            const mins = Math.floor((diff % 3600) / 60);
            const secs = Math.floor(diff % 60);
            
            let res = "";
            if (days > 0) res += days + "j ";
            if (hours > 0 || days > 0) res += hours + "h ";
            if (mins > 0 || hours > 0 || days > 0) res += mins + "m ";
            if (secs > 0 || res === "") res += secs + "s";
            return res.trim();
        }}

        function getTimeframeSeconds(tf, firstTs) {{
            const now = Math.floor(Date.now() / 1000);
            switch(tf) {{
                case '5h': return 5 * 3600;
                case '1j': return 24 * 3600;
                case '7j': return 7 * 86400;
                case '30j': return 30 * 86400;
                case '90j': return 90 * 86400;
                case 'tout': 
                    const diff = now - (firstTs || (now - 5 * 3600));
                    return Math.max(diff, 5 * 3600);
                default: return 90 * 86400;
            }}
        }}

        function setTimeframe(tf) {{
            currentTf = tf;
            document.querySelectorAll('.tf-btn').forEach(btn => {{
                btn.classList.toggle('active', btn.innerText.toLowerCase() === tf.toLowerCase());
            }});
            renderHistory();
        }}

        function renderHistory() {{
            const now = Math.floor(Date.now() / 1000);
            
            document.querySelectorAll('.service-card').forEach(card => {{
                const sid = card.getAttribute('data-service-id');
                const container = document.getElementById('bars-' + sid);
                const legendStart = document.getElementById('legend-start-' + sid);
                const statsContainer = document.getElementById('stats-' + sid);
                
                container.innerHTML = '';

                const sHistory = rawHistory[sid] || {{}};
                const tsKeys = Object.keys(sHistory).map(Number).sort((a, b) => a - b);
                const firstTs = tsKeys.length > 0 ? tsKeys[0] : (now - 18000);

                const totalSecs = getTimeframeSeconds(currentTf, firstTs);
                const barSecs = totalSecs / BARS_COUNT;
                const startSecs = now - totalSecs;

                if (currentTf === 'tout') {{
                    const d = new Date(firstTs * 1000);
                    legendStart.innerText = "Depuis le " + d.toLocaleDateString('fr-FR');
                }} else if (currentTf.endsWith('h')) {{
                    legendStart.innerText = "Il y a " + currentTf;
                }} else {{
                    legendStart.innerText = "Il y a " + currentTf.replace('j', ' jours');
                }}

                let totalUpAll = 0;
                let totalChecksAll = 0;
                let totalDowntimeSecs = 0;

                for (let i = 0; i < BARS_COUNT; i++) {{
                    const bStart = startSecs + (i * barSecs);
                    const bEnd = bStart + barSecs;

                    let bUp = 0;
                    let bTotal = 0;

                    for (const ts of tsKeys) {{
                        if (ts >= bStart && ts < bEnd) {{
                            const slot = sHistory[ts];
                            bUp += slot[0];
                            bTotal += slot[1];
                        }}
                    }}

                    totalUpAll += bUp;
                    totalChecksAll += bTotal;

                    const bar = document.createElement('div');
                    bar.className = 'bar';

                    const dStart = new Date(bStart * 1000);
                    const dEnd = new Date(bEnd * 1000);
                    let timeStr = "";
                    if (totalSecs <= 86400) {{
                        timeStr = dStart.toLocaleTimeString('fr-FR', {{hour:'2-digit', minute:'2-digit'}}) + " à " + dEnd.toLocaleTimeString('fr-FR', {{hour:'2-digit', minute:'2-digit'}});
                    }} else {{
                        timeStr = dStart.toLocaleDateString('fr-FR', {{day:'numeric', month:'short'}}) + " " + dStart.toLocaleTimeString('fr-FR', {{hour:'2-digit', minute:'2-digit'}});
                    }}

                    if (bTotal === 0) {{
                        bar.classList.add('nodata');
                        bar.innerHTML = `<div class="tooltip">${{timeStr}}<br>Aucune donnée</div>`;
                    }} else {{
                        const pct = Math.round((bUp / bTotal) * 100);
                        const downRatio = (bTotal - bUp) / bTotal;
                        totalDowntimeSecs += downRatio * barSecs;

                        if (pct < 95 && pct > 0) bar.classList.add('partial');
                        else if (pct === 0) bar.classList.add('down');

                        bar.innerHTML = `<div class="tooltip">${{timeStr}}<br><strong>${{pct}}% d'uptime</strong> (${{bUp}}/${{bTotal}})</div>`;
                    }}
                    container.appendChild(bar);
                }}

                const globalPct = totalChecksAll > 0 ? ((totalUpAll / totalChecksAll) * 100).toFixed(1) : "100.0";
                const dtFormatted = formatDuration(totalDowntimeSecs);
                
                statsContainer.innerHTML = `
                    <span class="uptime-pct">${{globalPct}}%</span>
                    <div class="downtime-tag">Downtime : ${{dtFormatted}}</div>
                `;
            }});
        }}

        function updateTimers() {{
            const now = Math.floor(Date.now() / 1000);
            
            document.querySelectorAll('[data-since]').forEach(el => {{
                const since = parseInt(el.getAttribute('data-since'));
                if (!isNaN(since) && since > 0) {{
                    const diff = Math.max(0, now - since);
                    el.innerText = formatDuration(diff);
                }}
            }});

            const recEl = document.getElementById('record-timer');
            if (recEl) {{
                const start = parseInt(recEl.getAttribute('data-start'));
                const endAttr = recEl.getAttribute('data-end');
                if (!isNaN(start) && start > 0) {{
                    const end = (endAttr && parseInt(endAttr) > 0) ? parseInt(endAttr) : now;
                    const diff = Math.max(0, end - start);
                    recEl.innerText = formatDuration(diff);
                }}
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
    slot_ts = (now_ts // 300) * 300
    
    old_state = load_state()
    history = clean_old_history(old_state.get("history", {}), now_ts)
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

        if prev_status and prev_status != status_str:
            has_changed = True
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

        if sid not in history:
            history[sid] = {}
        
        slot_str = str(slot_ts)
        if slot_str not in history[sid]:
            history[sid][slot_str] = [0, 0]

        history[sid][slot_str][1] += 1
        if is_up:
            history[sid][slot_str][0] += 1

        services_output.append({
            "id": sid,
            "name": s["name"],
            "url": s["url"],
            "icon": s.get("icon", ""),
            "status": status_str,
            "last_change": last_change
        })

    # Auto-initialisation ou mise à jour du record d'uptime
    best_dur = record.get("duration", 0)
    best_service = record.get("name")
    
    for s in SERVICES:
        sid = s["id"]
        if new_state[sid]["status"] == "UP":
            start_ts = new_state[sid]["last_change"]
            current_dur = now_ts - start_ts
            
            if record.get("name") == s["name"] and not record.get("end_ts"):
                record["duration"] = current_dur
                record["start_ts"] = start_ts
            elif current_dur > best_dur or not record.get("start_ts"):
                best_dur = current_dur
                best_service = s["name"]
                record = {
                    "name": s["name"],
                    "start_ts": start_ts,
                    "end_ts": None,
                    "duration": current_dur
                }

    save_data = {
        "services": new_state,
        "history": history,
        "record": record,
        "_meta": old_state.get("_meta", {})
    }

    generate_html(services_output, all_up, history, record)

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
