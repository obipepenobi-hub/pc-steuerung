import subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import wakeonlan
import threading
import json
import os
import sys
import time
import hmac
import py_compile
import urllib.request
import urllib.error

VERSION = "2.4.3"
GITHUB_REPO = "obipepenobi-hub/pc-steuerung"

SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
HTML_PATH = os.path.join(SCRIPT_DIR, 'remote.html')
SECRET_KEY_FILE = os.path.join(SCRIPT_DIR, 'secret_key.txt')

# Wird oeffentlich ueber Tailscale Funnel erreichbar sein - deshalb Pflicht-Key.
# Der Key kommt bei jedem Aufruf als ?key=... (auch fuer die Startseite "/").
# Steht bewusst NICHT im Code, sondern in einer lokalen Datei, die nie ins
# (oeffentliche) Git-Repo committet wird - siehe .gitignore. Das Auto-Update
# ueberschreibt diese Datei nie, weil es nur wol_server.py und remote.html ersetzt.
try:
    with open(SECRET_KEY_FILE, 'r') as f:
        ACCESS_KEY = f.read().strip()
    if not ACCESS_KEY:
        raise ValueError('leer')
except (FileNotFoundError, ValueError):
    sys.exit(
        f"Kein Access-Key gefunden. Bitte {SECRET_KEY_FILE} anlegen "
        "und den geheimen Key (eine Zeile, kein Zeilenumbruch) hineinschreiben."
    )

PCS = {
    'fine': {'mac': 'E0:D5:5E:2C:AB:53', 'ip': '192.168.178.70'},
    'liam': {'mac': 'A8:42:A1:5E:2A:54', 'ip': '192.168.178.22'},
}

# Platzhalter-Watt-Werte fuer die Kosten-Schaetzung - werden ersetzt,
# sobald echte Messwerte (Wattmeter/Smart-Plug) vorliegen.
WATT = {'fine': 185, 'liam': 95}
KWH_PRICE = 0.35

HISTORY_FILE = os.path.expanduser('~/wol_history.json')
TICK_SECONDS = 10
FAIL_THRESHOLD = 3
MAX_HISTORY_DAYS = 7

state_lock = threading.Lock()
fail_count = {'fine': 0, 'liam': 0}

update_lock = threading.Lock()
update_state = {'phase': 'idle', 'pct': 0, 'message': '', 'version': None}


def today_str():
    return time.strftime('%Y-%m-%d')


def now_ms():
    return int(time.time() * 1000)


def load_state():
    default = {
        'date': today_str(),
        'fine': {'online': False, 'sessionSince': None, 'todayMs': 0},
        'liam': {'online': False, 'sessionSince': None, 'todayMs': 0},
        'history': [],
    }
    try:
        with open(HISTORY_FILE, 'r') as f:
            data = json.load(f)
        for pc in ('fine', 'liam'):
            default[pc].update(data.get(pc, {}))
        default['date'] = data.get('date', default['date'])
        default['history'] = data.get('history', [])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return default


def save_state():
    tmp = HISTORY_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, HISTORY_FILE)


state = load_state()


def ping(ip):
    try:
        result = subprocess.run(['ping', '-c', '1', '-W', '2', ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False


def rollover_if_new_day():
    today = today_str()
    if state['date'] == today:
        return
    state['history'].append({
        'date': state['date'],
        'fineMs': state['fine']['todayMs'],
        'liamMs': state['liam']['todayMs'],
    })
    state['history'] = state['history'][-MAX_HISTORY_DAYS:]
    state['fine']['todayMs'] = 0
    state['liam']['todayMs'] = 0
    state['date'] = today


def update_loop():
    last_tick = time.time()
    while True:
        time.sleep(TICK_SECONDS)
        elapsed_ms = min((time.time() - last_tick) * 1000, TICK_SECONDS * 1500)
        last_tick = time.time()

        with state_lock:
            rollover_if_new_day()
            for name, pc in PCS.items():
                alive = ping(pc['ip'])
                s = state[name]
                if alive:
                    fail_count[name] = 0
                    if not s['online']:
                        s['online'] = True
                        s['sessionSince'] = now_ms()
                    s['todayMs'] += elapsed_ms
                else:
                    fail_count[name] += 1
                    if fail_count[name] >= FAIL_THRESHOLD and s['online']:
                        s['online'] = False
                        s['sessionSince'] = None
            save_state()


def key_ok(params):
    given = params.get('key', [''])[0]
    return hmac.compare_digest(given, ACCESS_KEY)


# ---------------------------------------------------------------------------
# Auto-Update ueber GitHub Releases
# ---------------------------------------------------------------------------

def version_tuple(v):
    v = v.strip()
    if v.startswith('v'):
        v = v[1:]
    parts = []
    for p in v.split('.'):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def http_get(url, as_json=False, timeout=10):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'pc-steuerung-updater',
        'Accept': 'application/vnd.github+json' if as_json else '*/*',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data.decode('utf-8')) if as_json else data.decode('utf-8')


def fetch_latest_release():
    return http_get(f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest', as_json=True)


def changelog_lines(body):
    lines = []
    for line in (body or '').splitlines():
        line = line.strip().lstrip('-*').strip()
        if line:
            lines.append(line)
    return lines


def do_update(tag):
    with update_lock:
        update_state.update(phase='downloading', pct=0.05, message='Lade Version ' + tag + ' …', version=tag)
    try:
        base = f'https://raw.githubusercontent.com/{GITHUB_REPO}/{tag}/'
        new_server = http_get(base + 'wol_server.py')
        with update_lock:
            update_state.update(pct=0.4, message='Lade Oberfläche …')
        new_html = http_get(base + 'remote.html')

        with update_lock:
            update_state.update(phase='installing', pct=0.75, message='Installiere …')

        tmp_py = SCRIPT_PATH + '.new'
        with open(tmp_py, 'w', encoding='utf-8') as f:
            f.write(new_server)
        py_compile.compile(tmp_py, doraise=True)

        with open(HTML_PATH, 'w', encoding='utf-8') as f:
            f.write(new_html)
        os.replace(tmp_py, SCRIPT_PATH)

        with update_lock:
            update_state.update(phase='done', pct=1.0, message='Fertig, starte neu …')
        time.sleep(1.5)
        if os.name == 'nt':
            # os.execv verschluckt sich unter Windows an Leerzeichen im Pfad
            # (nicht relevant auf Termux/Linux, aber so bleibt es testbar).
            subprocess.Popen([sys.executable, SCRIPT_PATH])
            os._exit(0)
        else:
            os.execv(sys.executable, [sys.executable, SCRIPT_PATH])
    except Exception as e:
        with update_lock:
            update_state.update(phase='error', message=str(e))


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, obj, content_type='application/json'):
        body = obj if isinstance(obj, bytes) else (obj if content_type != 'application/json' else json.dumps(obj))
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if not key_ok(params):
            if parsed.path in ('/', ''):
                self.reply(403, '<h1>403</h1><p>Fehlender oder falscher Key.</p>', 'text/html')
            else:
                self.reply(403, {'ok': False, 'error': 'unauthorized'})
            return

        if parsed.path in ('/', '/index.html'):
            try:
                with open(HTML_PATH, 'r', encoding='utf-8') as f:
                    self.reply(200, f.read(), 'text/html')
            except FileNotFoundError:
                self.reply(404, {'ok': False, 'error': 'remote.html fehlt'})

        elif parsed.path == '/status':
            with state_lock:
                self.reply(200, {'fine': state['fine']['online'], 'liam': state['liam']['online']})

        elif parsed.path == '/stats':
            with state_lock:
                rollover_if_new_day()
                self.reply(200, {
                    'fine': dict(state['fine']),
                    'liam': dict(state['liam']),
                    'history': state['history'],
                    'config': {'fineWatt': WATT['fine'], 'liamWatt': WATT['liam'], 'kwhPrice': KWH_PRICE},
                    'version': VERSION,
                })

        elif parsed.path == '/wol' and 'pc' in params:
            pc = params['pc'][0]
            if pc in PCS:
                wakeonlan.send_magic_packet(PCS[pc]['mac'])
                self.reply(200, {'ok': True})
            else:
                self.reply(200, {'ok': False})

        elif parsed.path == '/shutdown' and 'pc' in params:
            pc = params['pc'][0]
            if pc in PCS:
                try:
                    ip = PCS[pc]['ip']
                    urllib.request.urlopen(f'http://{ip}:9999/shutdown', timeout=3)
                    self.reply(200, {'ok': True})
                except Exception:
                    self.reply(200, {'ok': False})
            else:
                self.reply(200, {'ok': False})

        elif parsed.path == '/update/check':
            try:
                rel = fetch_latest_release()
                tag = rel.get('tag_name', '')
                available = version_tuple(tag) > version_tuple(VERSION)
                self.reply(200, {
                    'ok': True,
                    'currentVersion': VERSION,
                    'latestVersion': tag,
                    'available': available,
                    'notes': changelog_lines(rel.get('body', '')),
                })
            except Exception as e:
                self.reply(200, {'ok': False, 'error': str(e)})

        elif parsed.path == '/update/install':
            with update_lock:
                busy = update_state['phase'] not in ('idle', 'done', 'error')
            if busy:
                self.reply(200, {'ok': False, 'error': 'already running'})
            else:
                try:
                    rel = fetch_latest_release()
                    tag = rel.get('tag_name', '')
                    if version_tuple(tag) <= version_tuple(VERSION):
                        self.reply(200, {'ok': False, 'error': 'no update available'})
                    else:
                        with update_lock:
                            update_state.update(phase='downloading', pct=0, message='Starte …', version=tag)
                        threading.Thread(target=do_update, args=(tag,), daemon=True).start()
                        self.reply(200, {'ok': True})
                except Exception as e:
                    self.reply(200, {'ok': False, 'error': str(e)})

        elif parsed.path == '/update/progress':
            with update_lock:
                self.reply(200, dict(update_state))

        else:
            self.reply(404, {'ok': False})

    def log_message(self, format, *args):
        pass


threading.Thread(target=update_loop, daemon=True).start()
print(f"Server (v{VERSION}) laeuft auf Port 8080...")
ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
