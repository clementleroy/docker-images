"""Tests du formulaire ingress et de ses contrats Supervisor."""
import io
import copy
import threading
import time
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import configuration
from app import validate_options


OPTIONS = json.loads(Path(__file__).with_name('config.json').read_text())['options']


class ConfigurationTests(unittest.TestCase):
    def test_edit_preserves_global_settings_and_decision_entity(self):
        room = OPTIONS['rooms'][0]
        changed = {key: room[key] for key in ('key', 'name', 'temperature', 'humidity', 'co2', 'contacts')}
        changed['name'] = 'Bureau rénové'
        options = configuration.update_rooms(OPTIONS, [changed, *OPTIONS['rooms'][1:]], validate_options)
        self.assertEqual(options['interval_seconds'], OPTIONS['interval_seconds'])
        self.assertEqual(options['thresholds'], OPTIONS['thresholds'])
        self.assertEqual(options['rooms'][0]['decision_entity'], room['decision_entity'])
        self.assertEqual(options['rooms'][0]['name'], 'Bureau rénové')

    def test_new_room_with_optional_co2_and_contacts(self):
        room = {'key': 'chambre_amis', 'name': 'Chambre d’amis',
                'temperature': 'sensor.room_temperature', 'humidity': 'sensor.room_humidity',
                'co2': '', 'contacts': []}
        result = configuration.update_rooms(OPTIONS, [*OPTIONS['rooms'], room], validate_options)
        self.assertEqual(result['rooms'][-1], room)

    def test_rejects_bad_entity_and_duplicate_keys(self):
        room = {'key': 'nouvelle', 'name': 'Nouvelle', 'temperature': 'light.fake',
                'humidity': 'sensor.humidity', 'co2': '', 'contacts': []}
        with self.assertRaises(ValueError):
            configuration.update_rooms(OPTIONS, [*OPTIONS['rooms'], room], validate_options)
        dupe = {**OPTIONS['rooms'][0], 'name': 'Doublon'}
        with self.assertRaises(ValueError):
            configuration.update_rooms(OPTIONS, [*OPTIONS['rooms'], dupe], validate_options)

    def test_entity_list_filters_on_domain_and_device_class(self):
        states = [
            {'entity_id': 'sensor.temp', 'attributes': {'friendly_name': 'Température', 'device_class': 'temperature'}},
            {'entity_id': 'sensor.other', 'attributes': {'device_class': 'power'}},
            {'entity_id': 'binary_sensor.window', 'attributes': {'device_class': 'window'}},
            {'entity_id': 'binary_sensor.motion', 'attributes': {'device_class': 'motion'}},
        ]
        with patch.object(configuration, 'supervisor_json', return_value=states):
            self.assertEqual([x['entity_id'] for x in configuration.list_entities('opaque-test-token')],
                             ['binary_sensor.window', 'sensor.temp'])

    def test_supervisor_options_are_wrapped_in_options_key(self):
        with patch.object(configuration, 'supervisor_json') as call, \
                patch.object(configuration.threading, 'Thread') as thread:
            configuration.save_and_restart('opaque-test-token', OPTIONS)
            self.assertEqual(call.call_args.args[:2], ('/addons/self/options', 'opaque-test-token'))
            self.assertEqual(call.call_args.kwargs['payload'], {'options': OPTIONS})
            thread.return_value.start.assert_called_once()


class IngressRequestsTests(unittest.TestCase):
    def handler(self, body=None, address='172.30.32.2'):
        class Handler:
            path = '/api/rooms'
            command = 'PUT'
            client_address = (address, 1234)
            def send_response(self, status): self.status = status
            def send_header(self, *args): pass
            def end_headers(self): pass
        handler = Handler()
        data = json.dumps(body or {}).encode()
        handler.headers = {'Content-Length': str(len(data))}
        handler.rfile, handler.wfile = io.BytesIO(data), io.BytesIO()
        return handler

    def test_direct_access_is_denied_before_options_are_read(self):
        handler = self.handler(address='172.30.33.10')
        def forbidden(): self.fail('Accès aux options interdit')
        configuration.handle_request(handler, 'test', forbidden, forbidden, validate_options)
        self.assertEqual(handler.status, 403)

    def test_concurrent_additions_reject_stale_request(self):
        stored = copy.deepcopy(OPTIONS)
        base = copy.deepcopy(stored['rooms'])
        handlers = []
        for key in ('piece_a', 'piece_b'):
            room = {'key': key, 'name': key, 'temperature': 'sensor.temp',
                    'humidity': 'sensor.hum', 'co2': '', 'contacts': []}
            handlers.append(self.handler({'base_rooms': base, 'rooms': base + [room]}))
        def get_options():
            value = copy.deepcopy(stored)
            time.sleep(0.01)
            return value
        def save(options):
            stored.update(copy.deepcopy(options))
        available = [{'entity_id': 'sensor.temp', 'kind': 'temperature'},
                     {'entity_id': 'sensor.hum', 'kind': 'humidity'}]
        with patch.object(configuration, 'list_entities', return_value=available):
            threads = [threading.Thread(target=configuration.handle_request,
                       args=(handler, 'test', get_options, save, validate_options))
                       for handler in handlers]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=2)
        self.assertEqual(sorted(handler.status for handler in handlers), [200, 409])
        self.assertEqual(len(stored['rooms']), len(base) + 1)


if __name__ == '__main__':
    unittest.main()
