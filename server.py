#!/usr/bin/env python3
"""BingTok local server – single-session."""

import http.server
import json
import os
import uuid
import time
import socket
import subprocess
import shutil
from urllib.parse import urlparse, unquote

FFMPEG = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR  = os.path.join(BASE_DIR, 'uploads')
PRESET_DIR  = os.path.join(BASE_DIR, 'presets')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PRESET_DIR, exist_ok=True)

VIDEO_EXTS = {'.mp4', '.mov', '.webm', '.mkv', '.avi', '.m4v', '.MP4', '.MOV', '.M4V'}

_rooms      = {}   # room_id -> {config, events, clients}
_public_url = None # set by cloudflared tunnel detection

ROOMS_DIR = os.path.join(BASE_DIR, 'uploads', '_rooms')
os.makedirs(ROOMS_DIR, exist_ok=True)

def _room_config_path(room_id):
    return os.path.join(ROOMS_DIR, f'{room_id}.json')

def _save_room_config(room_id, config):
    try:
        with open(_room_config_path(room_id), 'w') as f:
            json.dump(config, f)
    except Exception:
        pass

def _load_room_config(room_id):
    try:
        with open(_room_config_path(room_id)) as f:
            return json.load(f)
    except Exception:
        return None

def _delete_room_config(room_id):
    try:
        os.remove(_room_config_path(room_id))
    except Exception:
        pass

def get_room(room_id):
    if room_id not in _rooms:
        _rooms[room_id] = {'config': _load_room_config(room_id), 'events': [], 'clients': []}
    return _rooms[room_id]

def room_broadcast(room_id, event_data):
    room = get_room(room_id)
    msg  = ('data: ' + json.dumps(event_data) + '\n\n').encode()
    dead = []
    for wf in room['clients']:
        try:
            wf.write(msg); wf.flush()
        except Exception:
            dead.append(wf)
    for d in dead:
        room['clients'].remove(d)

MIME = {
    '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm',
    '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo', '.m4v': 'video/mp4',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript', '.css': 'text/css',
}


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def parse_multipart(body, boundary):
    results = []
    sep = b'--' + boundary
    parts = body.split(sep)
    for part in parts[1:]:
        if part[:2] == b'--' or not part.strip():
            continue
        if b'\r\n\r\n' not in part:
            continue
        hdr_block, content = part.split(b'\r\n\r\n', 1)
        if content.endswith(b'\r\n'):
            content = content[:-2]
        hdr = hdr_block.decode('utf-8', errors='replace')
        if 'filename=' not in hdr:
            continue
        filename = None
        for line in hdr.splitlines():
            if 'Content-Disposition' in line and 'filename=' in line:
                for tok in line.split(';'):
                    tok = tok.strip()
                    if tok.lower().startswith('filename='):
                        filename = tok[9:].strip().strip('"\'')
        if filename:
            results.append((filename, content))
    return results


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, filepath, mime):
        try:
            size = os.path.getsize(filepath)
        except OSError:
            self.send_error(404)
            return
        range_hdr = self.headers.get('Range', '')
        if range_hdr.startswith('bytes='):
            parts = range_hdr[6:].split('-')
            start = int(parts[0]) if parts[0] else 0
            end   = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
            end   = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            with open(filepath, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(size))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def read_body(self):
        n = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(n) if n else b''

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _room_id(self):
        from urllib.parse import parse_qs
        qs = parse_qs(urlparse(self.path).query)
        return qs.get('room', [None])[0]

    def do_GET(self):
        from urllib.parse import parse_qs
        path = unquote(urlparse(self.path).path)
        room_id = self._room_id()

        if path in ('/', '/test', '/admin'):
            self.send_file(os.path.join(BASE_DIR, 'index.html'),
                           'text/html; charset=utf-8')
            return

        if path == '/rooms':
            # Check cookie for auth
            cookie_hdr = self.headers.get('Cookie', '')
            authed = 'bingtok_auth=1' in cookie_hdr
            if not authed:
                body = b'''<!DOCTYPE html><html><head><meta charset="utf-8">
<title>BingTok Rooms</title>
<style>body{background:#111;color:#eee;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{display:flex;flex-direction:column;gap:12px;background:#1a1a1a;padding:32px;border-radius:12px;min-width:300px}
h2{margin:0;color:#fe2c55}input{padding:10px;border-radius:8px;border:1px solid #333;background:#222;color:#eee;font-size:1rem}
button{padding:10px;background:#fe2c55;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:1rem;font-weight:700}
</style></head><body>
<form method="POST" action="/rooms-login">
<h2>BingTok Admin</h2>
<input type="password" name="pw" placeholder="Wachtwoord" autofocus>
<button type="submit">Inloggen</button>
</form></body></html>'''
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Build rooms overview
            base = _public_url or f'http://{local_ip()}:{PORT}'
            rows = ''
            for rid, room in _rooms.items():
                cfg = room.get('config') or {}
                name = cfg.get('sessionName') or cfg.get('testName') or '—'
                nevents = len(room.get('events', []))
                nclients = len(room.get('clients', []))
                rows += f'''<tr>
<td><code>{rid}</code></td>
<td>{name}</td>
<td>{nevents} events</td>
<td>{"🟢 " + str(nclients) + " live" if nclients else "⚪ geen"}</td>
<td>
  <a href="{base}/admin?room={rid}" target="_blank">Admin</a> &nbsp;
  <a href="{base}/test?room={rid}" target="_blank">Test</a> &nbsp;
  <button onclick="stopRoom('{rid}')" style="background:#333;color:#fe2c55;border:1px solid #fe2c55;border-radius:6px;padding:3px 10px;cursor:pointer;font-size:.8rem">Stop</button>
</td>
</tr>'''
            if not rows:
                rows = '<tr><td colspan="5" style="color:#666;text-align:center;padding:24px">Geen actieve sessies</td></tr>'
            body = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<title>BingTok Rooms</title>
<style>body{{background:#111;color:#eee;font-family:sans-serif;padding:32px;margin:0}}
h2{{color:#fe2c55;margin-top:0}}table{{width:100%;border-collapse:collapse;background:#1a1a1a;border-radius:12px;overflow:hidden}}
th{{text-align:left;padding:12px 16px;background:#222;color:#888;font-size:.75rem;text-transform:uppercase;letter-spacing:.5px}}
td{{padding:12px 16px;border-top:1px solid #222;font-size:.9rem}}
a{{color:#fe2c55;text-decoration:none}}code{{background:#222;padding:2px 6px;border-radius:4px;font-size:.85rem}}
.meta{{color:#666;font-size:.8rem;margin-top:16px}}</style></head>
<body>
<h2>BingTok — Actieve Sessies</h2>
<table><thead><tr><th>Room ID</th><th>Sessienaam</th><th>Events</th><th>Status</th><th>Acties</th></tr></thead>
<tbody>{rows}</tbody></table>
<p class="meta">Vernieuwt automatisch elke 15 seconden &nbsp;·&nbsp; <a href="/rooms">↺ Nu vernieuwen</a> &nbsp;·&nbsp; <a href="/rooms-logout">Uitloggen</a></p>
<script>
async function stopRoom(id) {{
  if (!confirm('Sessie ' + id + ' stoppen?')) return;
  await fetch('/api/room/stop?room=' + id, {{method: 'POST'}});
  location.reload();
}}
setInterval(() => location.reload(), 15000);
</script>
</body></html>'''.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/rooms-logout':
            self.send_response(302)
            self.send_header('Location', '/rooms')
            self.send_header('Set-Cookie', 'bingtok_auth=; Path=/; Max-Age=0')
            self.end_headers()
            return

        if path == '/api/local-ip':
            self.send_json({'ip': local_ip(), 'port': PORT})
            return

        if path == '/api/public-url':
            self.send_json({'url': _public_url})
            return

        if path == '/api/room/new':
            new_id = uuid.uuid4().hex[:8]
            get_room(new_id)  # initialise
            self.send_json({'room': new_id})
            return

        if path == '/api/rooms':
            self.send_json(list(_rooms.keys()))
            return

        if path == '/api/presets':
            presets = []
            for name in sorted(os.listdir(PRESET_DIR)):
                if name.startswith('.'):
                    continue
                p = os.path.join(PRESET_DIR, name)
                if os.path.isdir(p):
                    presets.append(name)
            self.send_json(presets)
            return

        if path == '/api/preset':
            from urllib.parse import parse_qs
            qs   = parse_qs(urlparse(self.path).query)
            name = qs.get('name', [None])[0]
            if not name or '/' in name or '..' in name:
                self.send_json({'error': 'invalid name'}, 400); return
            p = os.path.join(PRESET_DIR, name)
            if not os.path.isdir(p):
                self.send_json({'error': 'not found'}, 404); return
            result = {}
            for cat in ('test', 'filler', 'filler-data'):
                cat_dir = os.path.join(p, cat)
                files = []
                if os.path.isdir(cat_dir):
                    for f in sorted(os.listdir(cat_dir)):
                        if os.path.splitext(f)[1] in VIDEO_EXTS:
                            import hashlib
                            uid       = hashlib.md5(f'{name}/{cat}/{f}'.encode()).hexdigest()[:12]
                            cache_key = f'p_{uid}.mp4'
                            cached    = os.path.join(UPLOAD_DIR, cache_key)
                            url = f'/uploads/{cache_key}' if os.path.exists(cached) else None
                            files.append({'name': f, 'url': url, 'cached': url is not None,
                                          'src': f'/presets/{name}/{cat}/{f}'})
                result[cat] = files
            self.send_json(result)
            return

        if path.startswith('/presets/'):
            parts = path[len('/presets/'):].split('/')
            if len(parts) != 3 or '..' in parts:
                self.send_error(400); return
            preset_name, cat, filename = parts
            fp   = os.path.join(PRESET_DIR, preset_name, cat, filename)
            mime = MIME.get(os.path.splitext(filename)[1].lower(), 'application/octet-stream')
            self.send_file(fp, mime)
            return

        if path.startswith('/uploads/'):
            name = path[len('/uploads/'):]
            if '/' in name or '..' in name:
                self.send_error(400); return
            fp = os.path.join(UPLOAD_DIR, name)
            mime = MIME.get(os.path.splitext(name)[1].lower(), 'application/octet-stream')
            self.send_file(fp, mime)
            return

        if path == '/api/config':
            room = get_room(room_id) if room_id else {}
            self.send_json(room.get('config') or {} if room_id else {})
            return

        if path == '/api/events':
            room = get_room(room_id) if room_id else {'events': []}
            self.send_json(room.get('events', []))
            return

        if path == '/api/stream':
            if not room_id:
                self.send_error(400, 'room required'); return
            room = get_room(room_id)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            for ev in room['events']:
                try:
                    self.wfile.write(('data: ' + json.dumps(ev) + '\n\n').encode())
                except Exception:
                    return
            room['clients'].append(self.wfile)
            try:
                while True:
                    time.sleep(20)
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
            except Exception:
                if self.wfile in room['clients']:
                    room['clients'].remove(self.wfile)
            return

        self.send_error(404)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        room_id = self._room_id()

        if path == '/rooms-login':
            body = self.read_body().decode('utf-8', errors='replace')
            from urllib.parse import parse_qs as _pqs
            pw = _pqs(body).get('pw', [None])[0]
            if pw == 'BingTokAdmin!Fantasm':
                self.send_response(302)
                self.send_header('Location', '/rooms')
                self.send_header('Set-Cookie', 'bingtok_auth=1; Path=/; HttpOnly; SameSite=Strict')
                self.end_headers()
            else:
                self.send_response(302)
                self.send_header('Location', '/rooms')
                self.end_headers()
            return

        if path == '/api/room/stop':
            if not room_id or room_id not in _rooms:
                self.send_json({'error': 'not found'}, 404); return
            room = _rooms[room_id]
            room_broadcast(room_id, {'type': 'reset'})
            for wf in room['clients']:
                try: wf.flush()
                except: pass
            _delete_room_config(room_id)
            del _rooms[room_id]
            self.send_json({'ok': True})
            return

        if path == '/api/preset/compress':
            body = json.loads(self.read_body())
            preset_name = body.get('preset', '')
            cat         = body.get('cat', '')
            filename    = body.get('file', '')
            if not preset_name or not cat or not filename or '..' in preset_name + cat + filename:
                self.send_json({'error': 'invalid'}, 400); return
            src        = os.path.join(PRESET_DIR, preset_name, cat, filename)
            import hashlib
            uid        = hashlib.md5(f'{preset_name}/{cat}/{filename}'.encode()).hexdigest()[:12]
            cache_key  = f'p_{uid}.mp4'
            out        = os.path.join(UPLOAD_DIR, cache_key)
            if not os.path.exists(out):
                if FFMPEG and os.path.exists(FFMPEG):
                    subprocess.run([
                        FFMPEG, '-y', '-i', src,
                        '-vf', 'scale=720:-2',
                        '-c:v', 'libx264', '-crf', '28', '-preset', 'fast',
                        '-c:a', 'aac', '-b:a', '128k',
                        '-movflags', '+faststart',
                        out
                    ], capture_output=True)
                else:
                    import shutil as _sh; _sh.copy2(src, out)
            self.send_json({'url': f'/uploads/{cache_key}'})

        elif path == '/api/config':
            if not room_id:
                self.send_json({'error': 'room required'}, 400); return
            room = get_room(room_id)
            room['config'] = json.loads(self.read_body())
            room['events'] = []
            _save_room_config(room_id, room['config'])
            self.send_json({'ok': True})

        elif path == '/api/event':
            if not room_id:
                self.send_json({'error': 'room required'}, 400); return
            room  = get_room(room_id)
            event = json.loads(self.read_body())
            event.setdefault('at', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
            room['events'].append(event)
            room_broadcast(room_id, event)
            self.send_json({'ok': True})

        elif path == '/api/public-url':
            global _public_url
            body = json.loads(self.read_body())
            _public_url = body.get('url')
            self.send_json({'ok': True})

        elif path == '/api/reset':
            if not room_id:
                self.send_json({'error': 'room required'}, 400); return
            room = get_room(room_id)
            room['config'] = None
            room['events'] = []
            _delete_room_config(room_id)
            room_broadcast(room_id, {'type': 'reset'})
            self.send_json({'ok': True})

        elif path == '/api/upload':
            ct = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in ct:
                self.send_error(400, 'Expected multipart/form-data'); return
            boundary = ct.split('boundary=')[-1].strip().encode()
            body = self.read_body()
            files = parse_multipart(body, boundary)
            saved = []
            for filename, data in files:
                ext  = os.path.splitext(filename)[1].lower() or '.mp4'
                uid  = uuid.uuid4().hex[:10]
                # Write original to a temp file, then compress to mp4
                tmp  = os.path.join(UPLOAD_DIR, uid + '_tmp' + ext)
                out  = os.path.join(UPLOAD_DIR, uid + '.mp4')
                with open(tmp, 'wb') as f:
                    f.write(data)
                if FFMPEG and os.path.exists(FFMPEG):
                    result = subprocess.run([
                        FFMPEG, '-y', '-i', tmp,
                        '-vf', 'scale=720:-2',          # 720p breedte
                        '-c:v', 'libx264', '-crf', '28', '-preset', 'fast',
                        '-c:a', 'aac', '-b:a', '128k',
                        '-movflags', '+faststart',       # moov atom vooraan = snelle streaming
                        out
                    ], capture_output=True)
                    os.remove(tmp)
                    if result.returncode != 0:
                        # ffmpeg failed, fall back to original
                        os.rename(tmp if os.path.exists(tmp) else out, out) if not os.path.exists(out) else None
                else:
                    os.rename(tmp, out)
                saved.append({'original': filename, 'id': uid + '.mp4', 'url': '/uploads/' + uid + '.mp4'})
            self.send_json({'files': saved})

        else:
            self.send_error(404)


PORT = int(os.environ.get("PORT", 8888))

def start_cloudflared():
    """Start cloudflared tunnel and detect the public URL."""
    import threading, re
    cf = shutil.which('cloudflared')
    if not cf:
        return
    def _run():
        global _public_url
        try:
            proc = subprocess.Popen(
                [cf, 'tunnel', '--url', f'http://localhost:{PORT}'],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout:
                m = re.search(r'https://[a-z0-9\-]+\.trycloudflare\.com', line)
                if m:
                    _public_url = m.group(0)
                    print(f'  Publieke link →  {_public_url}/test')
                    break
        except Exception as e:
            print(f'  cloudflared fout: {e}')
    threading.Thread(target=_run, daemon=True).start()

if __name__ == '__main__':
    IP  = local_ip()
    srv = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'\n  BingTok server')
    print(f'  ─────────────────────────────────────────')
    print(f'  Setup    →  http://localhost:{PORT}/')
    print(f'  Lokaal   →  http://{IP}:{PORT}/test')
    print(f'  Tunnel   →  wordt gestart…')
    print(f'  ─────────────────────────────────────────')
    print(f'  Ctrl+C to stop\n')
    start_cloudflared()
    srv.serve_forever()
