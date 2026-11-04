"""Tests du suivi des excursions d’état entre deux cycles de sondage."""
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import Runtime


T0 = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def stamp(when):
    return when.isoformat()


def ha_state(value, when=T0, **attributes):
    return {
        "state": str(value), "last_reported": stamp(when),
        "last_changed": stamp(when), "last_updated": stamp(when),
        **attributes,
    }


def track_phases(co2_signal, when):
    return {'co2': {'signal': co2_signal, 'since': stamp(when)},
            'humidity': {'signal': 'correct', 'since': stamp(when)}}


def snapshot(when=T0, humidity=50, co2=700):
    return {
        "sensor.temp": ha_state(20, when),
        "sensor.hum": ha_state(humidity, when),
        "sensor.co2": ha_state(co2, when),
        "sensor.source": ha_state("jardin", when),
        "sensor.out_t": ha_state(20, when),
        "sensor.out_h": ha_state(45, when),
        "weather.home": ha_state("sunny", when, attributes={"temperature": 20, "humidity": 45}),
        "binary_sensor.window": ha_state("off", when),
        "input_select.decision": ha_state("À confirmer", when),
    }


class RuntimeEventTests(unittest.TestCase):
    def make_runtime(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        options = {
            "interval_seconds": 15,
            "source_sensor": "sensor.source",
            "outdoor_temperature": "sensor.out_t",
            "outdoor_humidity": "sensor.out_h",
            "weather": "weather.home",
            "rooms": [{
                "key": "bureau", "name": "Bureau",
                "temperature": "sensor.temp", "humidity": "sensor.hum",
                "co2": "sensor.co2", "contacts": ["binary_sensor.window"],
                "decision_entity": "input_select.decision",
            }],
        }
        runtime = Runtime(options, "test-token", data_dir=directory.name)
        self.addCleanup(runtime.pool.shutdown, wait=True)
        runtime.connected.set()
        runtime.publish = lambda topic, value, retain=False: published.append((topic, value, retain))
        published = []
        return runtime, published

    @staticmethod
    def enqueue(runtime, entity, value, when):
        runtime.events.put({
            "entity_id": entity,
            "new_state": ha_state(value, when),
        })

    def test_correct_need_then_correct_between_polls_keeps_aerer_and_resets_phase(self):
        runtime, published = self.make_runtime()
        runtime.cached_states = snapshot(T0 + timedelta(minutes=2), humidity=50, co2=700)
        runtime.decisions = {"bureau": "Aérer"}
        runtime.phases = {"bureau": track_phases("correct", T0)}
        runtime.events.put({
            "entity_id": "sensor.co2",
            "new_state": ha_state(1100, T0 + timedelta(minutes=1)),
        })
        runtime.events.put({
            "entity_id": "sensor.co2",
            "new_state": ha_state(700, T0 + timedelta(minutes=2)),
        })
        with patch("app.time.time", return_value=(T0 + timedelta(minutes=5)).timestamp()):
            runtime.cycle(repoll=False)

        self.assertEqual(runtime.decisions["bureau"]["co2"], "Aérer")
        self.assertEqual(runtime.last_results["bureau"]["signal"], "correct")
        self.assertEqual(runtime.phases["bureau"]["co2"]["since"], stamp(T0 + timedelta(minutes=5)))
        self.assertEqual([topic for topic, _, _ in published], ["conseils_air/bureau/state", "conseils_air/availability"])

    def test_need_correct_need_between_polls_does_not_confirm_early(self):
        runtime, _ = self.make_runtime()
        runtime.cached_states = snapshot(T0 + timedelta(minutes=2), humidity=50, co2=1100)
        runtime.decisions = {"bureau": "À confirmer"}
        runtime.phases = {"bureau": track_phases("besoin", T0)}
        self.enqueue(runtime, "sensor.co2", 700, T0 + timedelta(minutes=1))
        self.enqueue(runtime, "sensor.co2", 1100, T0 + timedelta(minutes=2))
        with patch("app.time.time", return_value=(T0 + timedelta(minutes=3)).timestamp()):
            runtime.cycle(repoll=False)

        self.assertEqual(runtime.decisions["bureau"]["co2"], "À confirmer")
        self.assertEqual(runtime.last_results["bureau"]["signal"], "besoin")
        self.assertEqual(runtime.phases["bureau"]["co2"]["since"], stamp(T0 + timedelta(minutes=3)))

    def test_urgent_excursion_is_remembered_when_final_polled_state_is_low(self):
        runtime, _ = self.make_runtime()
        runtime.cached_states = snapshot(T0 + timedelta(seconds=20), humidity=50, co2=700)
        runtime.decisions = {"bureau": "Correct"}
        runtime.phases = {"bureau": track_phases("correct", T0)}
        self.enqueue(runtime, "sensor.co2", 1600, T0 + timedelta(seconds=10))
        self.enqueue(runtime, "sensor.co2", 700, T0 + timedelta(seconds=20))
        with patch("app.time.time", return_value=(T0 + timedelta(seconds=30)).timestamp()):
            runtime.cycle(repoll=False)

        self.assertEqual(runtime.decisions["bureau"]["co2"], "Aérer")
        self.assertEqual(runtime.last_results["bureau"]["signal"], "correct")
        self.assertEqual(runtime.phases["bureau"]["co2"]["since"], stamp(T0 + timedelta(seconds=30)))

    def test_first_urgent_replay_preserves_humidity_seed_from_ha(self):
        runtime, _ = self.make_runtime()
        runtime.cached_states = snapshot(T0 + timedelta(seconds=20), humidity=50, co2=700)
        runtime.cached_states['input_select.decision']['state'] = 'Aérer'
        self.enqueue(runtime, 'sensor.co2', 1600, T0 + timedelta(seconds=10))
        self.enqueue(runtime, 'sensor.co2', 700, T0 + timedelta(seconds=20))
        with patch('app.time.time', return_value=(T0 + timedelta(seconds=30)).timestamp()):
            runtime.cycle(repoll=False)
        self.assertEqual(runtime.decisions['bureau'], {'humidity': 'Aérer', 'co2': 'Aérer'})

    def test_generation_change_during_ha_poll_rejects_cycle_and_clears_phases(self):
        runtime, published = self.make_runtime()
        runtime.decisions = {"bureau": "Aérer"}
        runtime.phases = {"bureau": track_phases("correct", T0 - timedelta(hours=1))}
        states = snapshot()
        changed = False

        def poll(entity):
            nonlocal changed
            if not changed:
                runtime.generation += 1
                changed = True
            return entity, states[entity]

        with patch.object(runtime, "ha_state", side_effect=poll), \
             patch("app.time.time", return_value=T0.timestamp()):
            with self.assertRaises(ConnectionError):
                runtime.cycle(repoll=True)

        self.assertEqual(runtime.phases, {})
        self.assertEqual(published, [])

    def test_generation_change_during_publish_rejects_cycle_and_next_cycle_resets_phase(self):
        runtime, published = self.make_runtime()
        runtime.cached_states = snapshot()
        runtime.decisions = {"bureau": "Aérer"}
        runtime.phases = {"bureau": track_phases("correct", T0 - timedelta(hours=1))}
        changed = False

        def publish(topic, value, retain=False):
            nonlocal changed
            published.append((topic, value, retain))
            if topic == "conseils_air/bureau/state" and not changed:
                runtime.generation += 1
                changed = True

        runtime.publish = publish
        with patch("app.time.time", return_value=T0.timestamp()):
            with self.assertRaises(ConnectionError):
                runtime.cycle(repoll=False)
        self.assertEqual(runtime.phases, {})

        later = T0 + timedelta(minutes=1)
        runtime.publish = lambda topic, value, retain=False: published.append((topic, value, retain))
        with patch("app.time.time", return_value=later.timestamp()):
            runtime.cycle(repoll=False)
        self.assertEqual(runtime.phases["bureau"]["co2"]["since"], stamp(later))
        self.assertEqual(runtime.decisions["bureau"]["co2"], "Aérer")
        self.assertTrue(all(topic.startswith("conseils_air/") for topic, _, _ in published))


if __name__ == "__main__":
    unittest.main()
