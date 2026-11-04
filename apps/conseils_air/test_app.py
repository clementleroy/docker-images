"""Contrats du transport : mémoire, indisponibilité et publication sans action."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import Runtime, discovery, load_decisions, save_decisions, validate_options

OPTIONS = json.loads(Path(__file__).with_name('config.json').read_text())['options']


class TransportTests(unittest.TestCase):
    def test_restart_recovers_decisions_but_no_partial_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            save_decisions(Path(directory)/'decisions.json', {'bureau': 'Aérer'})
            runtime = Runtime(OPTIONS, 'fake', directory)
            self.assertEqual(runtime.decisions, {'bureau': 'Aérer'})
            self.assertEqual(runtime.phases, {})
            runtime.pool.shutdown()

    def test_v1_memory_is_backed_up_and_v2_tracks_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'decisions.json'
            save_decisions(path, {'bureau': 'Aérer'})
            first = Runtime(OPTIONS, 'fake', directory)
            self.assertEqual(load_decisions(path.with_name('decisions-v1-backup.json')), {'bureau': 'Aérer'})
            first.pool.shutdown()
            tracks = {'bureau': {'co2': 'Correct', 'humidity': 'Aérer'}}
            save_decisions(path, tracks)
            second = Runtime(OPTIONS, 'fake', directory)
            self.assertEqual(second.decisions, tracks)
            self.assertEqual(second.phases, {})
            self.assertEqual(load_decisions(path.with_name('decisions-v1-backup.json')), {'bureau': 'Aérer'})
            second.pool.shutdown()

    def test_invalid_memory_and_duplicate_rooms(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'decisions.json'
            path.write_text('{broken')
            self.assertEqual(load_decisions(path), {})
        with self.assertRaises(ValueError):
            validate_options({**OPTIONS, 'rooms': OPTIONS['rooms']*2})

    def test_discovery_has_availability_expiration_and_stable_id(self):
        payload = discovery(OPTIONS['rooms'][0])
        self.assertEqual(payload['default_entity_id'], 'sensor.conseils_air_bureau')
        self.assertEqual(payload['expire_after'], 90)
        self.assertEqual(payload['availability_topic'], 'conseils_air/availability')
        self.assertNotIn('command_topic', payload)

    def test_failed_acquisition_does_not_publish_or_modify_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(OPTIONS, 'fake', directory)
            with patch.object(runtime, 'ha_state', side_effect=TimeoutError), patch.object(runtime, 'publish') as publish:
                with self.assertRaises(TimeoutError):
                    runtime.cycle()
                publish.assert_not_called()
                self.assertFalse(runtime.healthy())
                self.assertFalse(runtime.path.exists())
            runtime.pool.shutdown()

    def test_cycle_persists_and_publishes_engine_results_before_online(self):
        import time
        now = time.time()
        from datetime import datetime, timezone
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        states = {entity: {'state': 'off', 'last_reported': stamp} for entity in Runtime(OPTIONS,'fake').entity_ids()}
        for room in OPTIONS['rooms']:
            states[room['temperature']]['state'] = '21'
            states[room['humidity']]['state'] = '65'
            states[room['decision_entity']]['state'] = 'Aérer'
            if room['co2']:
                states[room['co2']]['state'] = '1100'
        states[OPTIONS['source_sensor']]['state'] = 'jardin'
        states[OPTIONS['outdoor_temperature']]['state'] = '10'
        states[OPTIONS['outdoor_humidity']]['state'] = '80'
        states[OPTIONS['weather']]['state'] = 'cloudy'
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(OPTIONS, 'fake', directory)
            runtime.connected.set()
            runtime.event_ready.set()
            with patch.object(runtime, 'ha_state', side_effect=lambda entity: (entity, states[entity])), patch.object(runtime, 'publish') as publish:
                runtime.cycle()
                self.assertTrue(runtime.healthy())
                self.assertEqual(len(runtime.last_results), 2)
                self.assertEqual(load_decisions(runtime.path)['bureau'], {'co2': 'Aérer', 'humidity': 'Aérer'})
                self.assertEqual(publish.call_args_list[-1].args, ('conseils_air/availability', 'online'))
            runtime.pool.shutdown()


if __name__ == '__main__':
    unittest.main()
