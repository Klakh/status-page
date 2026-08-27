#!/usr/bin/env python3
import urllib.request
import json
import time
import os
import subprocess
from datetime import datetime

# --- CONFIGURATION DES SERVICES À SURVEILLER ---
SERVICES = [
    {"id": "jellyfin", "name": "K.tv", "url": "https://example.com"},
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
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def generate_html(services_data):
    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M:%S")
    
    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>État des Services — Keeklah</title>
    <style>
        :root {{ --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8; --up: #22c55e; --down: #ef4444; }}
        body {{ font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 2rem; display: flex; justify-content: center; }}
        .container {{ width: 100%; max-width: 650px; }}
        h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; text-align: center; }}
        .subtitle {{ color: var(--muted); text-align: center; font-size: 0.875rem; margin-bottom: 2rem; }}
        .card {{ background: var(--card); border-radius: 12px; padding: 1rem 1.25rem; margin-bottom: 1rem; display: flex; align-items: center; justify-content: space-between; border: 1px solid #334155; }}
        .info {{ display: flex; flex-direction: column; gap: 0.25rem; }}
        .name {{ font-weight: 600; font-size: 1.1rem; }}
        .timer {{ font-size: 0.85rem; color: var(--muted); }}
        .badge {{ padding: 0.35rem 0.75rem; border-radius: 9999px; font-size: 0.8rem; font-weight: 700; text-transform: uppercase; }}
        .badge.up {{ background: rgba(34, 197, 94, 0.15); color: var(--up); border: 1px solid var(--up); }}
        .badge.down {{ background: rgba(239, 68, 68, 0.15); color: var(--down); border: 1px solid var(--down); }}
        .footer {{ text-align: center; margin-top: 2rem; font-size: 0.75rem; color: var(--muted); }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Statut des Services</h1>
        <p class="subtitle">Dernière mise à jour du script : {now_str}</p>
        <div id="services">
"""
    for s in services_data:
        status_class = "up" if s["status"] == "UP" else "down"
        status_label = "En ligne" if s["status"] == "UP" else "Inaccessible"
        timer_prefix = "En ligne depuis :" if s["status"] == "UP" else "Hors ligne depuis :"
        
        html += f"""
            <div class="card">
                <div class="info">
                    <span class="name">{s['name']}</span>
                    <span class="timer">{timer_prefix} <strong data-since="{s['last_change']}">--</strong></span>
                </div>
                <span class="badge {status_class}">{status_label}</span>
            </div>"""

    html += """
        </div>
        <div class="footer">Monitoring par Raspberry Pi 1 B+</div>
    </div>

    <script>
        function updateTimers() {
            const now = Math.floor(Date.now() / 1000);
            document.querySelectorAll('[data-since]').forEach(el => {
                const since = parseInt(el.getAttribute('data-since'));
                const diff = Math.max(0, now - since);
                
                const days = Math.floor(diff / 86400);
                const hours = Math.floor((diff % 86400) / 3600);
                const mins = Math.floor((diff % 3600) / 60);
                const secs = diff % 60;
                
                let res = "";
                if (days > 0) res += days + "j ";
                if (hours > 0 || days > 0) res += hours + "h ";
                res += mins + "m " + secs + "s";
                
                el.innerText = res;
            });
        }
        setInterval(updateTimers, 1000);
        updateTimers();
    </script>
</body>
</html>
"""
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

def main():
    now_ts = int(time.time())
    old_state = load_state()
    new_state = {}
    has_changed = False
    services_output = []

    for s in SERVICES:
        sid = s["id"]
        is_up = check_service(s["url"])
        status_str = "UP" if is_up else "DOWN"
        
        prev = old_state.get(sid, {})
        prev_status = prev.get("status")
        last_change = prev.get("last_change", now_ts)

        if prev_status != status_str:
            has_changed = True
            last_change = now_ts

        new_state[sid] = {
            "status": status_str,
            "last_change": last_change,
            "last_check": now_ts
        }

        services_output.append({
            "name": s["name"],
            "status": status_str,
            "last_change": last_change
        })

    save_state(new_state)

    # Re-génération systématique de l'HTML
    generate_html(services_output)

    # Push Git uniquement en cas de changement d'état ou si 1h s'est écoulée (Heartbeat)
    last_push = old_state.get("_meta", {}).get("last_push", 0)
    should_push = has_changed or (now_ts - last_push >= 3600)

    if should_push:
        new_state["_meta"] = {"last_push": now_ts}
        save_state(new_state)
        
        try:
            subprocess.run(["git", "-C", BASE_DIR, "add", "."], check=True)
            msg = "Changement d'état détecté" if has_changed else "Mise à jour périodique (heartbeat)"
            subprocess.run(["git", "-C", BASE_DIR, "commit", "-m", msg], check=True)
            subprocess.run(["git", "-C", BASE_DIR, "push", "origin", "main"], check=True)
            print(f"[{datetime.now()}] Git push effectué ({msg})")
        except Exception as e:
            print(f"[{datetime.now()}] Erreur Git : {e}")

if __name__ == "__main__":
    main()
