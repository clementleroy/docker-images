#!/usr/bin/env python3
"""Run inside the fixed image, without host ports, camera access or real credentials."""
import http.server
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request


def main():
    if any(pathlib.Path('/config', name).exists() for name in ('config.yml', 'config.yaml', 'frigate.db')):
        raise RuntimeError('Use a disposable container without the production /config mount')
    native_path = pathlib.Path('/usr/local/nginx/conf/nginx.conf')
    native = native_path.read_text()
    # Validate the COMPLETE native configuration first: map parsing sets hash defaults.
    # Only the disposable container's listen file is replaced to avoid needing TLS keys.
    listen = native_path.with_name('listen.conf')
    original_listen = listen.read_bytes() if listen.exists() else None
    base_path = native_path.with_name('base_path.conf')
    created_base_path = not base_path.exists()
    try:
        listen.write_text('listen 127.0.0.1:18080;\n')
        if created_base_path:
            base_path.write_text('')  # native generated default with no FRIGATE_BASE_PATH
        validation = subprocess.run(['nginx', '-t'], capture_output=True, timeout=10)
        if validation.returncode:
            raise AssertionError(validation.stderr.decode())
    finally:
        if created_base_path:
            base_path.unlink(missing_ok=True)
        if original_listen is None:
            listen.unlink(missing_ok=True)
        else:
            listen.write_bytes(original_listen)
    map_block = re.search(r'    map \$uri \$go2rtc_stream_proxy \{\n.*?\n    \}', native, re.S)
    assert map_block, 'missing stream marker default'
    locations = []
    for path in ['/live/mse/api/ws', '/live/webrtc/api/ws', '/api/go2rtc/webrtc']:
        match = re.search(r'        location ' + re.escape(path) + r' \{\n.*?\n        \}', native, re.S)
        assert match, path
        locations.append(match.group())
    seen = []

    class Backend(http.server.BaseHTTPRequestHandler):
        def answer(self):
            if self.path == '/auth':
                original = self.headers.get('X-Original-URL')
                seen.append((original, self.headers.get('X-Go2rtc-Stream-Proxy')))
                status = 401 if 'deny=1' in original else 200
            else:
                status = 200
            self.send_response(status)
            self.send_header('Content-Length', '0')
            self.end_headers()

        do_GET = do_POST = answer

        def log_message(self, *_args):
            pass

    servers = [http.server.ThreadingHTTPServer(('127.0.0.1', port), Backend) for port in (5001, 1984)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            folder = pathlib.Path(folder)
            (folder / 'logs').mkdir()
            for include in pathlib.Path('/usr/local/nginx/conf').glob('*.conf'):
                if include.name != 'nginx.conf':
                    shutil.copy2(include, folder / include.name)
            config = folder / 'nginx.conf'
            log = folder / 'error.log'
            config.write_text('''daemon off;
worker_processes 1;
pid PID;
error_log LOG warn;
events { worker_connections 64; }
http {
MAP
  upstream go2rtc { server 127.0.0.1:1984; }
  upstream frigate_api { server 127.0.0.1:5001; }
  server {
    listen 127.0.0.1:18080;
    include /usr/local/nginx/conf/auth_location.conf;
    location /api/stats {
      include /usr/local/nginx/conf/auth_request.conf;
      proxy_pass http://frigate_api;
    }
LOCATIONS
  }
}
'''.replace('PID', str(folder / 'nginx.pid')).replace('LOG', str(log))
                              .replace('MAP', map_block.group()).replace('LOCATIONS', '\n'.join(locations)))
            args = ['nginx', '-p', str(folder) + '/', '-c', str(config)]
            validation = subprocess.run(args + ['-t'], capture_output=True, timeout=10)
            if validation.returncode:
                raise AssertionError(validation.stderr.decode())
            process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(30):
                    try:
                        opener.open('http://127.0.0.1:18080/api/stats', timeout=1).close()
                        break
                    except urllib.error.URLError:
                        time.sleep(0.1)
                else:
                    raise AssertionError('isolated nginx failed to start')
                seen.clear()
                cases = [('/api/stats', 'GET', '1', '0', 200),
                         ('/live/mse/api/ws?src=test', 'GET', '0', '1', 200),
                         ('/live/webrtc/api/ws?src=test', 'GET', '0', '1', 200),
                         ('/api/go2rtc/webrtc?src=test', 'POST', '0', '1', 200),
                         ('/live/mse/api/ws?src=test&deny=1', 'GET', '0', '1', 401),
                         ('/api/stats?deny=1', 'GET', '1', '0', 401)]
                for path, method, forged, expected, status in cases:
                    request = urllib.request.Request('http://127.0.0.1:18080' + path, method=method,
                                                     headers={'X-Go2rtc-Stream-Proxy': forged})
                    try:
                        with opener.open(request, timeout=3) as response:
                            actual_status = response.status
                    except urllib.error.HTTPError as error:
                        actual_status = error.code
                    assert actual_status == status, (path, actual_status, status)
                    assert seen[-1] == (path, expected), (path, seen[-1], expected)
                assert 'uninitialized' not in log.read_text()
                print(json.dumps({'native_full_config_valid': True, 'nginx_cases': len(cases), 'all_passed': True,
                                  'client_headers_cannot_override': True, 'uninitialized_warnings': 0}))
            finally:
                process.terminate()
                process.communicate(timeout=5)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
