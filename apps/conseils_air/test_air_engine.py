import unittest
from datetime import datetime, timedelta, timezone

from air_engine import evaluate_room


NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def item(value, when=NOW, **attrs):
    return {"state": str(value), "last_reported": when.isoformat(), **attrs}


def states(*, humidity=50, co2=700, when=NOW, source="jardin"):
    return {
        "temp": item(20, when), "hum": item(humidity, when), "co2": item(co2, when),
        "out_t": item(20, when), "out_h": item(45, when), "source": item(source, when),
        "weather": item("sunny", when, attributes={"temperature": 20, "humidity": 45}),
        "window": item("off", when),
    }


CONFIG = {
    "key": "bureau", "temperature": "temp", "humidity": "hum", "co2": "co2",
    "source_sensor": "source", "outdoor_temperature": "out_t",
    "outdoor_humidity": "out_h", "weather": "weather", "contacts": ["window"],
}


class AirEngineTests(unittest.TestCase):
    def test_co2_need_is_independent_of_missing_humidity_outside_and_weather(self):
        values = states(co2=1100)
        for entity in ("temp", "hum", "out_t", "out_h", "weather"):
            values[entity]["state"] = "unavailable"
        start = evaluate_room(CONFIG, values, now=NOW)
        self.assertEqual(start["signals"]["co2"], "besoin")
        self.assertEqual(start["decisions"]["co2"], "À confirmer")
        confirmed = evaluate_room(CONFIG, values, start["decisions"], start["phase"], NOW + timedelta(minutes=3))
        self.assertEqual(confirmed["decision"], "Aérer")
        self.assertTrue(confirmed["besoin_co2"])
        self.assertIn("CO₂ élevé", confirmed["verdict"]["titre"])
        self.assertIn("Humidité ou gain extérieur indisponible", confirmed["verdict"]["limites"])
        self.assertEqual(confirmed["verdict"]["fiabilite"], "Partielle")

    def test_detail_is_compact_while_reason_and_limits_keep_context(self):
        values = states(humidity=65, co2=1100)
        phase = {"humidity": {"signal": "besoin", "since": (NOW - timedelta(minutes=3)).isoformat()},
                 "co2": {"signal": "besoin", "since": (NOW - timedelta(minutes=3)).isoformat()}}
        result = evaluate_room(CONFIG, values, {"humidity": "Aérer", "co2": "Aérer"}, phase, NOW)
        self.assertEqual(result["verdict"]["detail"], "💧 Assèche · CO₂ à réduire")
        self.assertIn("CO₂ élevé", result["verdict"]["raison"])

    def test_sensor_failure_preserves_lane_memory_and_never_advises_refermer(self):
        previous = {"co2": "Aérer", "humidity": "Correct"}
        values = states(humidity=50, co2=700)
        values["co2"]["state"] = "unavailable"
        result = evaluate_room(CONFIG, values, previous, now=NOW)
        self.assertEqual(result["decisions"], previous)
        self.assertEqual(result["decision"], "À confirmer")
        self.assertEqual(result["verdict"]["titre"], "À vérifier")

        # A confirmed humidity need remains a usable independent explanation.
        humidity_need = {"co2": "Aérer", "humidity": "Aérer"}
        values = states(humidity=65, co2=700)
        values["co2"]["state"] = "unavailable"
        result = evaluate_room(CONFIG, values, humidity_need, now=NOW)
        self.assertEqual(result["decision"], "Aérer")
        self.assertIn("assèche la pièce", result["verdict"]["titre"])
        self.assertIn("CO₂ non mesuré ou indisponible", result["verdict"]["limites"])

    def test_alternating_needs_use_independent_three_minute_phases(self):
        values = states(humidity=50, co2=1100)
        at_start = evaluate_room(CONFIG, values, now=NOW)
        humidity_starts = states(humidity=65, co2=1100, when=NOW + timedelta(minutes=2))
        at_two = evaluate_room(CONFIG, humidity_starts, at_start["decisions"], at_start["phase"], NOW + timedelta(minutes=2))
        self.assertEqual(at_two["decisions"]["humidity"], "À confirmer")
        self.assertEqual(at_two["phase"]["humidity"]["since"], (NOW + timedelta(minutes=2)).isoformat())
        co2_confirms = evaluate_room(CONFIG, humidity_starts, at_two["decisions"], at_two["phase"], NOW + timedelta(minutes=3))
        self.assertEqual(co2_confirms["decisions"]["co2"], "Aérer")
        self.assertEqual(co2_confirms["decisions"]["humidity"], "À confirmer")
        humidity_confirms = evaluate_room(CONFIG, humidity_starts, co2_confirms["decisions"], co2_confirms["phase"], NOW + timedelta(minutes=5))
        self.assertEqual(humidity_confirms["decisions"]["humidity"], "Aérer")
        self.assertEqual(humidity_confirms["decision"], "Aérer")

    def test_urgent_co2_is_immediate_and_a_brief_drop_does_not_erase_memory(self):
        urgent = states(co2=1500)
        urgent["hum"]["state"] = "unavailable"
        urgent["temp"]["state"] = "unavailable"
        first = evaluate_room(CONFIG, urgent, now=NOW)
        self.assertEqual(first["decisions"]["co2"], "Aérer")
        self.assertEqual(first["decision"], "Aérer")
        self.assertEqual(first["signals"]["co2"], "urgent")
        rebound = states(humidity=50, co2=700, when=NOW + timedelta(seconds=30))
        after = evaluate_room(CONFIG, rebound, first["decisions"], first["phase"], NOW + timedelta(seconds=30))
        self.assertEqual(after["decisions"]["co2"], "Aérer")
        self.assertEqual(after["decision"], "Aérer")

    def test_persisted_need_with_low_current_co2_is_labeled_as_pending_improvement(self):
        values = states(humidity=50, co2=700)
        phase = {"co2": {"signal": "correct", "since": (NOW - timedelta(minutes=2)).isoformat()},
                 "humidity": {"signal": "correct", "since": NOW.isoformat()}}
        result = evaluate_room(CONFIG, values, {"co2": "Aérer", "humidity": "Correct"}, phase, NOW)
        self.assertIn("amélioration à confirmer", result["verdict"]["titre"])
        self.assertNotIn("CO₂ élevé", result["verdict"]["titre"])
        self.assertIn("amélioration durable pas encore confirmée", result["verdict"]["raison"])

    def test_restart_resets_both_phases_but_preserves_both_decisions(self):
        decisions = {"co2": "Aérer", "humidity": "Aérer"}
        values = states(humidity=50, co2=700)
        restarted = evaluate_room(CONFIG, values, decisions, None, NOW + timedelta(minutes=10))
        self.assertEqual(restarted["decisions"], decisions)
        self.assertEqual(restarted["phase"]["co2"]["since"], (NOW + timedelta(minutes=10)).isoformat())
        self.assertEqual(restarted["phase"]["humidity"]["since"], (NOW + timedelta(minutes=10)).isoformat())
        self.assertEqual(restarted["decision"], "Aérer")

    def test_each_lane_requires_its_own_proofs_for_correct(self):
        phase_time = NOW - timedelta(minutes=10)
        decisions = {"co2": "Aérer", "humidity": "Aérer"}
        phase = {name: {"signal": "correct", "since": phase_time.isoformat()} for name in decisions}
        values = states(humidity=50, co2=700, when=NOW - timedelta(minutes=4))
        values["temp"]["last_reported"] = (NOW - timedelta(minutes=7)).isoformat()
        values["hum"]["last_reported"] = (NOW - timedelta(minutes=7)).isoformat()
        result = evaluate_room(CONFIG, values, decisions, phase, NOW)
        self.assertEqual(result["decisions"]["co2"], "Correct")
        self.assertEqual(result["decisions"]["humidity"], "Aérer")
        values["hum"]["last_reported"] = NOW.isoformat()
        result = evaluate_room(CONFIG, values, result["decisions"], result["phase"], NOW)
        self.assertEqual(result["decisions"]["humidity"], "Correct")
        self.assertEqual(values["temp"]["last_reported"], (NOW - timedelta(minutes=7)).isoformat())
        self.assertEqual(result["decision"], "Correct")

    def test_no_co2_room_has_humidity_only_state_and_explicit_scope(self):
        config = {**CONFIG, "co2": None}
        values = states(humidity=50, co2=700)
        phase = {"humidity": {"signal": "correct", "since": (NOW - timedelta(minutes=10)).isoformat()}}
        result = evaluate_room(config, values, "Aérer", phase, NOW)
        self.assertEqual(result["signals"]["co2"], "non_mesure")
        self.assertEqual(result["decisions"]["co2"], "Correct")
        self.assertEqual(result["decision"], "Correct")
        self.assertEqual(result["verdict"]["perimetre"], "Humidité seule · CO₂ non mesuré")
        self.assertIn("CO₂ non mesuré", result["verdict"]["detail"])
        self.assertIn("\n", result["verdict"]["detail"])
        self.assertEqual(result["verdict"]["titre"], "Humidité : aération peu utile")
        self.assertEqual(result["verdict"]["detail"], "💧 Assèche\nHumidité seule · CO₂ non mesuré")
        values["window"]["state"] = "on"
        opened = evaluate_room(config, values, result["decisions"], result["phase"], NOW)
        self.assertEqual(opened["verdict"]["titre"], "Humidité satisfaite · vous pouvez refermer")

    def test_cold_and_bad_weather_are_reserves_not_partial_measurements(self):
        values = states(humidity=65, co2=700)
        for entity in ("temp", "out_t"):
            values[entity]["state"] = "18"
        values["weather"]["state"] = "pouring"
        phase = {"humidity": {"signal": "besoin", "since": (NOW - timedelta(minutes=3)).isoformat()},
                 "co2": {"signal": "correct", "since": (NOW - timedelta(minutes=10)).isoformat()}}
        result = evaluate_room(CONFIG, values, {"humidity": "Aérer", "co2": "Correct"}, phase, NOW)
        self.assertEqual(result["verdict"]["fiabilite"], "Suffisante")
        self.assertIn("Météo défavorable", result["verdict"]["limites"])
        self.assertIn("Risque de refroidissement", result["verdict"]["limites"])
        self.assertIn("Refroidissement", result["verdict"]["detail"])

    def test_opening_title_requires_a_known_contact(self):
        values = states(humidity=50, co2=700)
        phase = {"humidity": {"signal": "correct", "since": (NOW - timedelta(minutes=10)).isoformat()},
                 "co2": {"signal": "correct", "since": (NOW - timedelta(minutes=10)).isoformat()}}
        decisions = {"humidity": "Correct", "co2": "Correct"}
        values["window"]["state"] = "unknown"
        unknown = evaluate_room(CONFIG, values, decisions, phase, NOW)
        self.assertEqual(unknown["verdict"]["titre"], "Peu utile")
        values["window"]["state"] = "on"
        opened = evaluate_room(CONFIG, values, decisions, phase, NOW)
        self.assertEqual(opened["verdict"]["titre"], "Besoin satisfait · vous pouvez refermer")

    def test_ineffective_drying_does_not_claim_humidity_is_satisfactory(self):
        for equipped in (True, False):
            with self.subTest(co2=equipped):
                config = {**CONFIG, 'co2': 'co2' if equipped else ''}
                values = states(humidity=70, co2=700)
                values['out_h'] = item(80)
                values['window'] = item('on')
                start = evaluate_room(config, values, 'Aérer', now=NOW)
                later = NOW + timedelta(minutes=5)
                values['hum'] = item(70, later)
                values['co2'] = item(700, later)
                result = evaluate_room(config, values, start['decisions'], start['phase'], later)
                self.assertEqual(result['decision'], 'Correct')
                self.assertEqual(result['verdict']['titre'], 'Aération peu utile · vous pouvez refermer')
                self.assertIn('peu asséchant', result['verdict']['raison'])
                self.assertEqual(result['humidity'], 70)

    def test_scalar_legacy_aerer_migrates_conservatively_to_all_lanes(self):
        result = evaluate_room(CONFIG, states(humidity=50, co2=700), "Aérer", now=NOW)
        self.assertEqual(result["decisions"], {"humidity": "Aérer", "co2": "Aérer"})
        self.assertEqual(result["decision"], "Aérer")

    def test_numeric_epoch_weather_attributes_freshness_and_nan(self):
        epoch = NOW.timestamp()
        values = states(humidity=50, source="meteo")
        result = evaluate_room(CONFIG, values, now=epoch)
        self.assertIsNotNone(result["gain"])
        values["weather"]["last_reported"] = (NOW - timedelta(hours=3)).isoformat()
        stale = evaluate_room(CONFIG, values, now=epoch)
        self.assertIsNone(stale["gain"])
        self.assertEqual(stale["signals"]["humidity"], "inconnu")
        values = states(humidity="nan")
        self.assertEqual(evaluate_room(CONFIG, values, now=epoch)["signals"]["humidity"], "inconnu")
        with self.assertRaises(ValueError):
            evaluate_room(CONFIG, states(), now=float("nan"))


if __name__ == "__main__":
    unittest.main()
