"""Moteur pur de décision pour les conseils d’aération.

Les appels HA sont laissés au runtime. ``states`` contient les états HA sous
forme ``{entity_id: {"state": ..., "last_reported": ISO, "last_changed": ISO}}``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping

NEEDED = "besoin"
CORRECT = "correct"
URGENT = "urgent"
INTERMEDIATE = "intermediaire"
UNKNOWN = "inconnu"
BAD_WEATHER = {
    "pouring", "rainy", "lightning", "lightning-rainy", "hail", "snowy",
    "snowy-rainy", "exceptional", "windy", "windy-variant",
}


def _dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        try:
            parsed = datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age(state: Mapping[str, Any] | None, now: datetime) -> float:
    reported = _dt((state or {}).get("last_reported"))
    return (now - reported).total_seconds() if reported else math.inf


def _value(states: Mapping[str, Mapping[str, Any]], entity: str | None,
           now: datetime, max_age: float, low: float, high: float) -> float | None:
    if not entity:
        return None
    item = states.get(entity)
    if not item or not 0 <= _age(item, now) < max_age:
        return None
    try:
        value = float(item.get("state"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and low <= value <= high else None


def evaluate_room(config: Mapping[str, Any], states: Mapping[str, Mapping[str, Any]],
                  decision: str | Mapping[str, str] = "À confirmer",
                  phase: Mapping[str, Any] | None = None,
                  now: datetime | str | float | int | None = None) -> dict[str, Any]:
    """Évalue les pistes humidité et CO₂ sans coupler leurs indisponibilités.

    ``decision`` accepte l'ancienne valeur scalaire HA ou une mémoire par piste.
    La mémoire durable est retournée dans ``decisions`` ; ``phase`` ne conserve
    que les confirmations temporaires et doit être vide après redémarrage.
    """
    instant = _dt(now) if now is not None else datetime.now(timezone.utc)
    if instant is None:
        raise ValueError("now doit être un instant ISO valide")
    t = _value(states, config.get("temperature"), instant, 3600, 0, 45)
    h = _value(states, config.get("humidity"), instant, 3600, .01, 100)
    co2_entity = config.get("co2")
    co2_expected = bool(co2_entity) and bool(config.get("co2_expected", True))
    co2 = _value(states, co2_entity, instant, 300, 1, 10000)

    thresholds = config.get("thresholds", {})
    need_minutes = float(thresholds.get("need_minutes", 3))
    correct_minutes = float(thresholds.get("correct_minutes", 5))
    proof_minutes = float(thresholds.get("proof_minutes", 4))
    source_entity = config.get("source_sensor", config.get("outside_source"))
    source_item = states.get(source_entity, {}) if source_entity else {}
    source = source_item.get("state")
    te = he = None
    if source == "jardin":
        te = _value(states, config.get("outdoor_temperature", config.get("outside_temperature")), instant, 21600, -40, 55)
        he = _value(states, config.get("outdoor_humidity", config.get("outside_humidity")), instant, 21600, .01, 100)
    elif source == "meteo":
        weather_item = states.get(config.get("weather"), {})
        if weather_item.get("state") not in (None, "unknown", "unavailable") and 0 <= _age(weather_item, instant) < 10800:
            attributes = weather_item.get("attributes", {})
            try:
                te = float(attributes.get("temperature"))
                he = float(attributes.get("humidity"))
                if not (math.isfinite(te) and -40 <= te <= 55):
                    te = None
                if not (math.isfinite(he) and .01 <= he <= 100):
                    he = None
            except (TypeError, ValueError):
                te = he = None
    co2_signal = UNKNOWN
    if co2_expected:
        if co2 is None:
            co2_signal = UNKNOWN
        elif co2 >= 1500:
            co2_signal = URGENT
        elif co2 >= 1000:
            co2_signal = NEEDED
        elif co2 <= 800:
            co2_signal = CORRECT
        else:
            co2_signal = INTERMEDIATE

    gain = None
    if all(value is not None for value in (t, h, te, he)):
        inside_factor = math.exp(17.62 * t / (243.12 + t))
        outside_factor = math.exp(17.62 * te / (243.12 + te))
        gain = h - he * outside_factor / inside_factor
    humidity_need = gain is not None and h is not None and h >= 60 and gain >= 5
    if humidity_need:
        humidity_signal = NEEDED
    elif h is None or t is None or gain is None:
        humidity_signal = UNKNOWN
    elif h <= 55 or (gain is not None and gain <= 3):
        humidity_signal = CORRECT
    else:
        humidity_signal = INTERMEDIATE

    raw_signals = {"humidity": humidity_signal}
    if co2_expected:
        raw_signals["co2"] = co2_signal
    applicable = ["humidity"] + (["co2"] if co2_expected else [])

    # An old scalar Aérer may represent either underlying cause. Seed every
    # applicable lane to Aérer so migration cannot erase a remembered need.
    legacy = decision if isinstance(decision, str) else None
    if legacy == "Aérer":
        lane_decisions = {track: "Aérer" for track in applicable}
    elif legacy == "Correct":
        lane_decisions = {track: "Correct" for track in applicable}
    elif isinstance(decision, Mapping):
        lane_decisions = {track: decision.get(track, "À confirmer") for track in applicable}
    else:
        lane_decisions = {track: "À confirmer" for track in applicable}
    if not co2_expected:
        lane_decisions["co2"] = "Correct"
        co2_signal = "non_mesure"
        raw_signals["co2"] = "non_mesure"
    old_phases = phase if isinstance(phase, Mapping) else {}
    next_phases: dict[str, dict[str, str]] = {}
    lane_proofs = {
        # Humidity is the evidence sensor. Temperature must remain fresh for
        # calculating the gain, but a silent temperature sensor is not itself
        # required to report again during the confirmation phase.
        "humidity": list(config.get("humidity_proofs") or [config.get("humidity")]),
        "co2": list(config.get("co2_proofs") or ([co2_entity] if co2_expected else [])),
    }
    for track in applicable:
        signal = raw_signals[track]
        old_phase = old_phases.get(track, {})
        phase_since = _dt(old_phase.get("since")) if old_phase.get("signal") == signal else None
        if phase_since is None:
            phase_since = instant
        elapsed = max(0.0, (instant - phase_since).total_seconds())
        proofs = lane_proofs[track]
        cutoff = phase_since.timestamp() + proof_minutes * 60
        proofs_fresh = bool(proofs) and all(
            entity and (item := states.get(entity)) and (reported := _dt(item.get("last_reported")))
            and reported.timestamp() >= cutoff
            for entity in proofs
        )
        current = lane_decisions[track]
        if signal == URGENT or (signal == NEEDED and elapsed >= need_minutes * 60):
            current = "Aérer"
        elif signal == CORRECT and elapsed >= correct_minutes * 60 and proofs_fresh:
            current = "Correct"
        lane_decisions[track] = current
        next_phases[track] = {"signal": signal, "since": phase_since.isoformat()}

    contacts = list(config.get("contacts", []))
    contact_ok = bool(contacts) and all(states.get(e, {}).get("state") in ("on", "off") for e in contacts)
    opened = any(states.get(e, {}).get("state") == "on" for e in contacts)
    weather = states.get(config.get("weather"), {}) if config.get("weather") else {}
    weather_ok = weather.get("state") not in (None, "unknown", "unavailable") and 0 <= _age(weather, instant) < 10800
    bad_weather = weather_ok and weather.get("state") in BAD_WEATHER
    outside_missing = te is None or he is None
    humidity_missing = t is None or h is None or outside_missing
    co2_missing = co2_expected and co2 is None
    missing = humidity_missing or co2_missing or not weather_ok
    confirmed_humidity = lane_decisions["humidity"] == "Aérer"
    confirmed_co2 = co2_expected and lane_decisions.get("co2") == "Aérer"
    usable_humidity = confirmed_humidity and humidity_signal != UNKNOWN
    usable_co2 = confirmed_co2 and co2_signal != UNKNOWN
    useful = usable_humidity or usable_co2
    unresolved_remembered = (confirmed_humidity and not usable_humidity) or (confirmed_co2 and not usable_co2)
    any_pending_need = any(raw_signals[track] in (NEEDED, URGENT) and lane_decisions[track] != "Aérer" for track in applicable)
    all_correct = all(lane_decisions[track] == "Correct" for track in applicable)
    all_inputs_known = all(raw_signals[track] not in (UNKNOWN, URGENT, NEEDED) for track in applicable)
    if useful:
        global_decision = "Aérer"
    elif missing or unresolved_remembered or any_pending_need or not all_correct or not all_inputs_known:
        global_decision = "À confirmer"
    else:
        global_decision = "Correct"
    needs_verification = missing or unresolved_remembered
    cold = t is not None and te is not None and (t < 19 or te < 19)
    signal = (URGENT if co2_signal == URGENT else NEEDED if any(raw_signals[k] == NEEDED for k in applicable)
              else UNKNOWN if any(raw_signals[k] == UNKNOWN for k in applicable) else
              CORRECT if all(raw_signals[k] == CORRECT for k in applicable) else INTERMEDIATE)
    if needs_verification and not useful:
        title = "À vérifier"
    elif useful:
        if usable_co2 and co2_signal in (URGENT, NEEDED):
            reason_short = "CO₂ élevé"
        elif usable_humidity and humidity_signal == NEEDED:
            reason_short = "assèche la pièce"
        else:
            reason_short = "amélioration à confirmer"
        title = f"{'Poursuivre' if opened else 'Aérer'} · {reason_short}"
        if cold:
            title = f"{'Poursuivre' if opened else 'Aérer'} brièvement · {reason_short}"
    elif any_pending_need or global_decision == "À confirmer":
        title = "À confirmer"
    else:
        if opened and contact_ok:
            if h is not None and h > 55 and gain is not None and gain <= 3:
                title = "Aération peu utile · vous pouvez refermer"
            else:
                title = "Humidité satisfaite · vous pouvez refermer" if not co2_expected else "Besoin satisfait · vous pouvez refermer"
        else:
            title = "Humidité : aération peu utile" if not co2_expected else "Peu utile"
    if signal == URGENT and usable_co2:
        color = "🟠"
    elif useful and (bad_weather or not contact_ok or cold or missing):
        color = "🟠"
    elif useful:
        color = "🟢"
    elif needs_verification or any_pending_need or global_decision == "À confirmer":
        color = "⚪"
    else:
        color = "🔵"
    limits = []
    if co2_missing:
        limits.append("CO₂ non mesuré ou indisponible")
    if humidity_missing:
        limits.append("Humidité ou gain extérieur indisponible")
    if not weather_ok:
        limits.append("Météo indisponible ou périmée")
    if bad_weather:
        limits.append("Météo défavorable")
    if not contact_ok:
        limits.append("État de l’ouvrant à vérifier")
    if useful and cold:
        limits.append("Risque de refroidissement")
    reserve = " · ".join(limits)
    is_door = bool(config.get("door", False) or config.get("key") == "buanderie")
    icon_object = "door" if is_door else "window"
    icon = ("mdi:help-circle-outline" if not contact_ok else
            ("mdi:door-open" if opened else "mdi:door-closed") if is_door else
            ("mdi:window-open-variant" if opened else "mdi:window-closed-variant"))
    color_name = "orange" if color == "🟠" else "grey" if color == "⚪" else "green" if color == "🟢" else "blue"
    perimetre = "Humidité + CO₂" if co2_expected else "Humidité seule · CO₂ non mesuré"
    if usable_co2 and co2_signal == URGENT:
        raison = "CO₂ très élevé (urgence, ≥1500 ppm)"
    elif usable_co2 and co2_signal == NEEDED:
        raison = "CO₂ élevé (≥1000 ppm) confirmé"
    elif usable_humidity and humidity_signal == NEEDED:
        raison = "Humidité élevée et air extérieur plus sec : gain estimé ≥5 points"
    elif useful:
        raison = "Besoin mémorisé ; amélioration durable pas encore confirmée"
    elif needs_verification:
        raison = "Mesures requises manquantes, périmées ou incohérentes"
    elif any_pending_need:
        raison = "Nouveau besoin en cours de confirmation"
    elif h is not None and h > 55 and gain is not None and gain <= 3:
        raison = "Air extérieur peu asséchant ; aucun autre besoin mesuré confirmé"
    else:
        raison = "Aucun besoin d’aération confirmé"
    reliability = "Partielle" if (co2_missing or humidity_missing or not weather_ok or not contact_ok) else "Suffisante"
    humidity_label = "Indéterminé" if gain is None else "Assèche la pièce" if gain >= 5 else "Humidifie la pièce" if gain <= -5 else "Effet faible"
    humidity_short = "Assèche" if gain is not None and gain >= 5 else "Humidifie" if gain is not None and gain <= -5 else "Effet faible" if gain is not None else "Indéterminé"
    detail = [f"💧 {humidity_short}"]
    if co2_expected:
        detail[0] += f" · CO₂ {('inconnu' if co2 is None else 'à réduire' if co2 >= 800 else 'bas')}"
    else:
        detail.append(perimetre)
    compact_limits = []
    if useful and cold:
        compact_limits.append("Refroidissement")
    if bad_weather:
        compact_limits.append("Météo défavorable")
    if not contact_ok:
        compact_limits.append("Ouvrant à vérifier")
    if co2_missing or humidity_missing or not weather_ok:
        compact_limits.append("Mesures partielles")
    if compact_limits:
        detail.append("Réserves : " + " · ".join(compact_limits))
    return {
        "decision": global_decision,
        "decisions": lane_decisions,
        "signal": signal,
        "signals": raw_signals,
        "phase": next_phases,
        "signal_since": {key: value["since"] for key, value in next_phases.items()},
        "besoin_co2": confirmed_co2,
        "besoin_humidite": confirmed_humidity,
        "gain": gain,
        "temperature": t,
        "humidity": h,
        "co2_value": co2,
        "verdict": {
            "signal": signal, "titre": title, "couleur": color,
            "co2": "Non mesuré" if not co2_expected else "Indisponible" if co2 is None else ("À réduire · " if co2 >= 800 else "Bas · ") + str(round(co2)) + " ppm",
            "co2_resume": "non mesuré" if not co2_expected else "inconnu" if co2 is None else "à réduire" if co2 >= 800 else "bas",
            "humidite": humidity_label,
            "ouvrant": ("Ouvert · état partiel à vérifier" if opened else "État de l’ouvrant à vérifier") if not contact_ok else "Ouvrant ouvert" if opened else "Ouvrant fermé",
            "reserve": reserve,
            "icon": icon,
            "couleur_nom": color_name,
            "raison": raison,
            "fiabilite": reliability,
            "perimetre": perimetre,
            "limites": limits,
            "besoin_co2": confirmed_co2,
            "besoin_humidite": confirmed_humidity,
            "detail": "\n".join(dict.fromkeys(detail)),
        },
    }
