"""Conseils Air : acquisition HA, moteur pur, publication MQTT et mémoire locale."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import queue
import signal
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from air_engine import evaluate_room

LOG = logging.getLogger('conseils_air')
PREFIX = 'conseils_air'
VERSION = '1.2.0'


def request_json(url, token, timeout=8):
    request = Request(url, headers={'Authorization': 'Bearer ' + token})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def save_decisions(path, decisions):
    """Remplacement atomique ; ne contient que les mémoires des pièces."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(decisions, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_decisions(path):
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict):
            raise ValueError('Mémoire invalide')
        valid = ('Aérer', 'Correct', 'À confirmer')
        result = {}
        for key, decision in value.items():
            if isinstance(decision, str) and decision in valid:
                result[key] = decision  # Mémoire v1, migration lors du calcul.
            elif isinstance(decision, dict) and all(decision.get(track) in valid for track in ('co2', 'humidity')):
                result[key] = {track: decision[track] for track in ('co2', 'humidity')}
        return result
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        LOG.warning('Mémoire illisible : reprise prudente, confirmations recommencées')
        return {}


def validate_options(options):
    rooms = options.get('rooms', [])
    if not rooms:
        raise ValueError('Au moins une pièce est requise')
    keys = []
    for room in rooms:
        key = room['key']
        if not re.fullmatch('[a-z][a-z0-9_]*', key):
            raise ValueError('Identifiant de pièce invalide')
        keys.append(key)
        for field in ('temperature', 'humidity'):
            if not room.get(field, '').startswith('sensor.'):
                raise ValueError('Capteur intérieur invalide')
        if room.get('co2') and not room['co2'].startswith('sensor.'):
            raise ValueError('Capteur CO₂ invalide')
        if not isinstance(room.get('contacts'), list):
            raise ValueError('Liste des ouvrants requise')
    if len(keys) != len(set(keys)):
        raise ValueError('Identifiants de pièces dupliqués')
    interval = options.get('interval_seconds', 15)
    if not 5 <= interval <= 30:
        raise ValueError('Intervalle invalide')
    thresholds = options.get('thresholds', {})
    if thresholds.get('proof_minutes', 4) > thresholds.get('correct_minutes', 5):
        raise ValueError('La preuve doit précéder la confirmation de l’amélioration')
    return options


def discovery(room):
    key = room['key']
    topic = f'{PREFIX}/{key}/state'
    return {
        'name': room['name'], 'unique_id': f'{PREFIX}_{key}',
        'default_entity_id': f'sensor.{PREFIX}_{key}',
        'state_topic': topic, 'value_template': '{{ value_json.titre }}',
        'json_attributes_topic': topic,
        'availability_topic': f'{PREFIX}/availability',
        'payload_available': 'online', 'payload_not_available': 'offline',
        'expire_after': 90, 'icon': 'mdi:air-filter',
        'device': {'identifiers': [PREFIX], 'name': 'Conseils Air',
                   'manufacturer': 'Maison', 'model': 'Conseils d’aération',
                   'sw_version': VERSION},
        'origin': {'name': 'Conseils Air', 'sw_version': VERSION},
    }


class Runtime:
    def __init__(self, options, token, data_dir='/data'):
        self.options = validate_options(options)
        self.token = token
        self.path = Path(data_dir) / 'decisions.json'
        self.decisions = load_decisions(self.path)
        legacy_copy = self.path.with_name('decisions-v1-backup.json')
        if any(isinstance(value, str) for value in self.decisions.values()) and not legacy_copy.exists():
            save_decisions(legacy_copy, self.decisions)
            LOG.info('Migration des mémoires : copie v1 conservée, besoins désormais séparés')
        self.phases = {}  # Aucune confirmation partielle restaurée après arrêt.
        self.stop = threading.Event()
        self.connected = threading.Event()
        self.last_success = 0.0
        self.last_results = {}
        self.failed = False
        self.client = None
        self.generation = 0
        self.last_generation = 0
        self.events = queue.Queue(maxsize=4096)
        self.event_ready = threading.Event()
        self.wake = threading.Event()
        self.cached_states = {}
        self.pool = ThreadPoolExecutor(max_workers=6)

    def ha_state(self, entity):
        try:
            return entity, request_json('http://supervisor/core/api/states/' + quote(entity, safe='.'), self.token)
        except HTTPError as error:
            if error.code == 404:
                return entity, {'state': 'unavailable'}
            raise

    def entity_ids(self):
        entities = {self.options[field] for field in
                    ('source_sensor', 'outdoor_temperature', 'outdoor_humidity', 'weather')}
        for room in self.options['rooms']:
            entities.update(room[field] for field in ('temperature', 'humidity', 'co2') if room.get(field))
            if room['key'] not in self.decisions and room.get('decision_entity'):
                entities.add(room['decision_entity'])
            entities.update(room['contacts'])
        return sorted(entities)

    def publish(self, topic, value, retain=False):
        if not self.connected.is_set():
            raise ConnectionError('MQTT déconnecté')
        info = self.client.publish(topic, json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value,
                                   qos=1, retain=retain)
        info.wait_for_publish(timeout=5)
        if not info.is_published():
            raise ConnectionError('Publication MQTT non confirmée')

    def mqtt_start(self):
        import paho.mqtt.client as mqtt
        # Découverte du broker fournie par Supervisor : aucun secret dans les options.
        service = request_json('http://supervisor/services/mqtt', self.token)['data']
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=PREFIX)
        self.client.username_pw_set(service['username'], service['password'])
        self.client.will_set(f'{PREFIX}/availability', 'offline', qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                self.connected.set()
                # Toujours hors ligne jusqu’à un calcul complet et publié.
                client.publish(f'{PREFIX}/availability', 'offline', qos=1, retain=True)
                for room in self.options['rooms']:
                    client.publish(f'homeassistant/sensor/{PREFIX}/{room["key"]}/config',
                                   json.dumps(discovery(room), ensure_ascii=False), qos=1, retain=True)
                LOG.info('Connexion MQTT établie')
            else:
                self.connected.clear()
                LOG.warning('Connexion MQTT refusée')

        def on_disconnect(client, userdata, flags, reason_code, properties):
            self.connected.clear()
            self.generation += 1
            self.wake.set()
            LOG.warning('Connexion MQTT interrompue ; confirmations réinitialisées')

        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect
        self.client.connect_async(service['host'], int(service.get('port', 1883)), keepalive=30)
        self.client.loop_start()

    def watch_states(self):
        """Observe aussi les excursions entre deux sondages de fraîcheur."""
        import websocket
        wanted = set(self.entity_ids())
        while not self.stop.is_set():
            stream = None
            try:
                stream = websocket.create_connection('ws://supervisor/core/websocket', timeout=10,
                                                      suppress_origin=True)
                if json.loads(stream.recv()).get('type') != 'auth_required':
                    raise ConnectionError('Protocole HA inattendu')
                stream.send(json.dumps({'type': 'auth', 'access_token': self.token}))
                if json.loads(stream.recv()).get('type') != 'auth_ok':
                    raise ConnectionError('Authentification refusée')
                stream.send(json.dumps({'id': 1, 'type': 'subscribe_events', 'event_type': 'state_changed'}))
                if not json.loads(stream.recv()).get('success'):
                    raise ConnectionError('Abonnement refusé')
                self.event_ready.set()
                self.wake.set()
                stream.settimeout(1)
                while not self.stop.is_set():
                    try:
                        message = json.loads(stream.recv())
                    except websocket.WebSocketTimeoutException:
                        continue
                    event = message.get('event', {})
                    data = event.get('data', {})
                    if data.get('entity_id') in wanted:
                        self.events.put_nowait(data)
                        self.wake.set()
            except Exception as error:
                if not self.stop.is_set():
                    LOG.warning('Flux des mesures interrompu (%s)', type(error).__name__)
            finally:
                self.event_ready.clear()
                self.generation += 1
                self.wake.set()
                if stream:
                    stream.close()
            self.stop.wait(5)

    def cycle(self, repoll=True):
        generation = self.generation
        if generation != self.last_generation:
            self.phases.clear()
            self.last_generation = generation
        states = dict(self.pool.map(self.ha_state, self.entity_ids())) if repoll else dict(self.cached_states)
        now = time.time()
        decisions = dict(self.decisions)
        # Initialiser avant le rejeu : un pic urgent ne doit pas masquer une
        # ancienne demande d’humidité présente uniquement dans le helper HA.
        for room in self.options['rooms']:
            key = room['key']
            if key not in decisions:
                seed = states.get(room.get('decision_entity', ''), {}).get('state')
                decisions[key] = seed if seed in ('Aérer', 'Correct') else 'À confirmer'
        phases = dict(self.phases)
        # Rejouer les transitions reçues, même si le sondage final est revenu à
        # la valeur initiale. Ainsi une excursion brève interrompt sa confirmation.
        observed = dict(self.cached_states or states)
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            entity = event['entity_id']
            observed[entity] = event.get('new_state') or {'state': 'unavailable'}
            for room in self.options['rooms']:
                key = room['key']
                result = evaluate_room({**self.options, **room}, observed,
                                       decision=decisions.get(key, 'À confirmer'),
                                       phase=phases.get(key), now=now)
                # Les événements assurent les remises à zéro, le cycle final
                # seul confirme les durées et écrit les décisions.
                phases[key] = result['phase']
                if result['signals']['co2'] == 'urgent':
                    previous = decisions.get(key, 'À confirmer')
                    tracks = dict(previous) if isinstance(previous, dict) else dict.fromkeys(('co2', 'humidity'), previous)
                    decisions[key] = {**tracks, 'co2': 'Aérer'}
            incoming = observed[entity]
            current = states.get(entity, {})
            if incoming.get('last_updated', '') >= current.get('last_updated', ''):
                states[entity] = incoming
        results = {}
        for room in self.options['rooms']:
            key = room['key']
            config = {**self.options, **room}
            seed = states.get(room.get('decision_entity', ''), {}).get('state')
            initial = decisions.get(key, seed if seed in ('Aérer', 'Correct') else 'À confirmer')
            result = evaluate_room(config, states, decision=initial,
                                   phase=phases.get(key), now=now)
            decisions[key] = result['decisions']
            phases[key] = result['phase']
            results[key] = result
        if generation != self.generation:
            self.phases.clear()
            raise ConnectionError('Connexion interrompue pendant le calcul')
        # La mémoire précède la publication ; un défaut disque invalide le cycle.
        if decisions != self.decisions:
            save_decisions(self.path, decisions)
        for room in self.options['rooms']:
            key = room['key']
            result = results[key]
            payload = {**result['verdict'], 'decision': result['decision'],
                       'signal': result['signal'], 'updated_at': now,
                       'decisions': result['decisions'], 'signals': result['signals'],
                       'phases': result['phase'],
                       'engine': VERSION, 'ouvrant_icon': result['verdict']['icon']}
            payload.pop('icon', None)
            self.publish(f'{PREFIX}/{key}/state', payload)
            previous = self.last_results.get(key, {})
            if previous.get('decision') != result['decision']:
                LOG.info('%s : %s (%s)', room['name'], result['decision'], result['signal'])
        if generation != self.generation:
            self.phases.clear()
            raise ConnectionError('Connexion interrompue pendant la publication')
        self.publish(f'{PREFIX}/availability', 'online', retain=True)
        self.cached_states = states
        self.decisions, self.phases, self.last_results = decisions, phases, results
        self.last_success = time.monotonic()
        if self.failed:
            LOG.info('Acquisition et publication rétablies')
        self.failed = False

    def healthy(self):
        return self.connected.is_set() and self.event_ready.is_set() and time.monotonic() - self.last_success < 90

    def serve_health(self):
        runtime = self

        from configuration import handle_request, current_options, save_and_restart

        class Handler(BaseHTTPRequestHandler):
            def dispatch(self):
                if self.path == '/health' and self.command == 'GET':
                    healthy = runtime.healthy()
                    self.send_response(200 if healthy else 503)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'healthy': healthy}).encode())
                    return
                if not handle_request(self, runtime.token,
                                      lambda: current_options(runtime.token),
                                      lambda options: save_and_restart(runtime.token, options),
                                      validate_options,
                                      get_results=lambda: runtime.last_results if runtime.healthy() else {}):
                    self.send_error(404)

            do_GET = dispatch
            do_PUT = dispatch

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('0.0.0.0', 8099), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def run(self):
        server = self.serve_health()
        try:
            self.mqtt_start()
            threading.Thread(target=self.watch_states, daemon=True).start()
            next_refresh = 0
            while not self.stop.is_set():
                if self.connected.is_set() and self.event_ready.is_set():
                    try:
                        refresh = time.monotonic() >= next_refresh
                        self.cycle(repoll=refresh or not self.cached_states)
                        if refresh:
                            next_refresh = time.monotonic() + self.options.get('interval_seconds', 15)
                    except Exception as error:
                        # Ni URL, ni jeton, ni contenu HTTP dans les logs.
                        if not self.failed:
                            LOG.warning('Cycle interrompu (%s), conseils indisponibles', type(error).__name__)
                        self.failed = True
                        self.phases.clear()
                        try:
                            self.publish(f'{PREFIX}/availability', 'offline', retain=True)
                        except Exception:
                            pass
                else:
                    self.phases.clear()
                    if self.connected.is_set():
                        try:
                            self.publish(f'{PREFIX}/availability', 'offline', retain=True)
                        except Exception:
                            pass
                    next_refresh = 0
                self.wake.wait(timeout=max(1, min(15, next_refresh - time.monotonic())))
                self.wake.clear()
        finally:
            if self.client:
                try:
                    self.publish(f'{PREFIX}/availability', 'offline', retain=True)
                except Exception:
                    pass
                self.client.disconnect()
                self.client.loop_stop()
            self.pool.shutdown(wait=True, cancel_futures=True)
            server.shutdown()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    options = json.loads(Path('/data/options.json').read_text())
    runtime = Runtime(options, os.environ['SUPERVISOR_TOKEN'])
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *args: runtime.stop.set())
    runtime.run()


if __name__ == '__main__':
    main()
