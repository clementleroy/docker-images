"""Interface ingress de configuration pour Conseils Air.

Le serveur HTTP appelant délègue ici les routes autres que /health. Le token
Supervisor reste côté serveur et n'est jamais inclus dans une réponse HTML/JSON.
"""
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_BODY = 256 * 1024
_SAVE_LOCK = threading.Lock()


def supervisor_json(path, token, method='GET', payload=None):
    """Appelle une API Supervisor sans exposer le jeton ni le corps sensible."""
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    request = Request('http://supervisor' + path, data=data, method=method,
                      headers={'Authorization': 'Bearer ' + token,
                               'Content-Type': 'application/json'})
    with urlopen(request, timeout=12) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def list_entities(token):
    """Retourne les états HA utiles à l'UI, avec noms lisibles et catégories."""
    response = supervisor_json('/core/api/states', token)
    if not isinstance(response, list):
        raise ValueError('Réponse des entités Home Assistant invalide')
    result = []
    for item in response:
        if not isinstance(item, dict):
            continue
        entity_id = item.get('entity_id', '')
        attrs = item.get('attributes') or {}
        domain = entity_id.split('.', 1)[0]
        device_class = attrs.get('device_class')
        kind = None
        if domain == 'sensor' and device_class in ('temperature', 'humidity', 'carbon_dioxide'):
            kind = {'temperature': 'temperature', 'humidity': 'humidity',
                    'carbon_dioxide': 'co2'}[device_class]
        elif domain == 'binary_sensor' and device_class in ('window', 'door', 'opening'):
            kind = 'contact'
        if kind:
            result.append({'entity_id': entity_id,
                           'name': str(attrs.get('friendly_name') or entity_id),
                           'kind': kind, 'unit': str(attrs.get('unit_of_measurement') or '')})
    return sorted(result, key=lambda row: (row['kind'], row['name'].casefold(), row['entity_id']))


def room_advice(token, rooms):
    """Lit les conseils déjà publiés pour les pièces configurées."""
    ids = {'sensor.conseils_air_' + room['key'] for room in rooms}
    return {item['entity_id']: {'state': item.get('state', 'unknown'),
                                'detail': (item.get('attributes') or {}).get('detail', ''),
                                'perimetre': (item.get('attributes') or {}).get('perimetre', '')}
            for item in supervisor_json('/core/api/states', token)
            if isinstance(item, dict) and item.get('entity_id') in ids}


def current_options(token):
    response = supervisor_json('/addons/self/info', token)
    data = response.get('data', {})
    options = data.get('options')
    if not isinstance(options, dict):
        raise ValueError('Options Supervisor indisponibles')
    return options


def save_and_restart(token, options):
    """Enregistre puis redémarre l'add-on après l'envoi de la réponse HTTP."""
    supervisor_json('/addons/self/options', token, method='POST', payload={'options': options})

    def restart():
        time.sleep(1.5)
        try:
            supervisor_json('/addons/self/restart', token, method='POST', payload={})
        except Exception:
            # L'app est peut-être déjà arrêtée par un redémarrage déclenché par options.
            pass
    threading.Thread(target=restart, name='conseils-air-restart', daemon=True).start()


def _clean_room(room, previous):
    if not isinstance(room, dict):
        raise ValueError('Pièce invalide')
    name = room.get('name')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
        raise ValueError('Donnez un nom de pièce de 1 à 64 caractères')
    key = room.get('key')
    if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', key):
        raise ValueError('Identifiant de pièce invalide')
    clean = {'key': key, 'name': name.strip()}
    for field in ('temperature', 'humidity'):
        value = room.get(field)
        if not isinstance(value, str) or not re.fullmatch(r'sensor\.[a-z0-9_]+', value):
            raise ValueError('Choisissez un capteur de température et d’humidité')
        clean[field] = value
    co2 = room.get('co2', '')
    if not isinstance(co2, str) or (co2 and not re.fullmatch(r'sensor\.[a-z0-9_]+', co2)):
        raise ValueError('Capteur CO₂ invalide')
    clean['co2'] = co2
    contacts = room.get('contacts', [])
    if not isinstance(contacts, list) or len(contacts) > 32 or any(
            not isinstance(value, str) or not re.fullmatch(r'binary_sensor\.[a-z0-9_]+', value)
            for value in contacts):
        raise ValueError('Liste des contacts invalide')
    clean['contacts'] = list(dict.fromkeys(contacts))
    if previous and previous.get('key') == key and previous.get('decision_entity'):
        clean['decision_entity'] = previous['decision_entity']
    return clean


def update_rooms(options, rooms, validate_options):
    """Valide et remplace les pièces en conservant les autres options intactes."""
    if not isinstance(options, dict) or not isinstance(rooms, list) or not rooms:
        raise ValueError('Ajoutez au moins une pièce')
    old_by_key = {room.get('key'): room for room in options.get('rooms', [])}
    clean = [_clean_room(room, old_by_key.get(room.get('key'))) for room in rooms]
    keys = [room['key'] for room in clean]
    if len(keys) != len(set(keys)):
        raise ValueError('Deux pièces ont le même identifiant')
    updated = {**options, 'rooms': clean}
    return validate_options(updated)


def handle_request(handler, token, get_options, save_options, validate_options, get_results=lambda: {}):
    """Traite les routes ingress; retourne True si la requête a été prise."""
    path = handler.path.split('?', 1)[0]
    # L'ingress Supervisor est le seul client autorisé à utiliser l'interface.
    if path.startswith('/api/') or path in ('/', '/index.html'):
        if handler.client_address[0] != '172.30.32.2':
            _respond(handler, 403, {'error': 'Accès réservé à Home Assistant'})
            return True
    if path in ('/', '/index.html') and handler.command == 'GET':
        _respond(handler, 200, HTML, 'text/html; charset=utf-8')
        return True
    if path == '/api/config' and handler.command == 'GET':
        rooms = get_options().get('rooms', [])
        advice = {}
        try:
            for key, result in get_results().items():
                title = (result.get('verdict') or {}).get('titre')
                if title:
                    advice['sensor.conseils_air_' + key] = {'state': title, 'detail': result['verdict'].get('detail', ''), 'perimetre': result['verdict'].get('perimetre', '')}
        except Exception:
            pass
        if not advice:
            try:
                advice = room_advice(token, rooms)
            except Exception:
                pass
        _respond(handler, 200, {'rooms': rooms, 'advice': advice})
        return True
    if path == '/api/entities' and handler.command == 'GET':
        try:
            _respond(handler, 200, {'entities': list_entities(token)})
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            _respond(handler, 502, {'error': 'Impossible de charger les capteurs Home Assistant'})
        return True
    if path == '/api/rooms' and handler.command == 'PUT':
        try:
            length = int(handler.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                raise ValueError('Requête invalide')
            body = json.loads(handler.rfile.read(length))
            with _SAVE_LOCK:
                current = get_options()
                if body.get('base_rooms') != current.get('rooms', []):
                    _respond(handler, 409, {'error': 'La configuration a changé. Rechargez la page avant de réessayer.'})
                    return True
                submitted = body.get('rooms')
                old_keys = {room.get('key') for room in current.get('rooms', [])}
                new_keys = {room.get('key') for room in submitted} if isinstance(submitted, list) else set()
                if not old_keys.issubset(new_keys):
                    raise ValueError('La suppression d’une pièce n’est pas disponible')
                options = update_rooms(current, submitted, validate_options)
                available = {item['entity_id']: item['kind'] for item in list_entities(token)}
                expected = {'temperature': 'temperature', 'humidity': 'humidity', 'co2': 'co2'}
                previous_rooms = {room['key']: room for room in current.get('rooms', [])}
                for room in options['rooms']:
                    previous = previous_rooms.get(room['key'], {})
                    for field, kind in expected.items():
                        entity = room.get(field)
                        if entity and entity != previous.get(field) and available.get(entity) != kind:
                            raise ValueError('Un capteur choisi est indisponible ou de type incompatible')
                    unchanged_contacts = set(previous.get('contacts', []))
                    if any(available.get(entity) != 'contact' and entity not in unchanged_contacts
                           for entity in room['contacts']):
                        raise ValueError('Un ouvrant choisi est indisponible ou de type incompatible')
                save_options(options)
            _respond(handler, 200, {'saved': True})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            _respond(handler, 400, {'error': str(error)})
        except Exception as error:
            _respond(handler, 502, {'error': 'Supervisor n’a pas pu enregistrer les options'})
        return True
    if path.startswith('/api/'):
        _respond(handler, 404, {'error': 'Route inconnue'})
        return True
    return False


def _respond(handler, status, body, content_type='application/json; charset=utf-8'):
    data = body.encode('utf-8') if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.end_headers()
    handler.wfile.write(data)


HTML = r'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conseils Air · Pièces</title>
<style>
:root{color-scheme:light dark;--bg:Canvas;--fg:CanvasText;--line:color-mix(in srgb,CanvasText 20%,transparent);--accent:#167c65;--soft:color-mix(in srgb,var(--accent) 10%,Canvas)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}main{max-width:850px;margin:auto;padding:clamp(16px,4vw,36px)}h1{font-size:clamp(1.5rem,4vw,2rem);margin:0}header{margin-bottom:24px}header p,.muted{color:color-mix(in srgb,var(--fg) 70%,transparent)}.room,.editor{border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}.room{display:flex;align-items:center;justify-content:space-between;gap:16px}.room strong{display:block;font-size:1.1rem}.room small{display:block;color:color-mix(in srgb,var(--fg) 68%,transparent);overflow-wrap:anywhere}.actions{display:flex;gap:8px;flex-shrink:0}button{font:inherit;font-weight:600;border:1px solid var(--line);border-radius:9px;padding:10px 14px;background:var(--bg);color:var(--fg);cursor:pointer;min-height:44px}button.primary{background:var(--accent);color:white;border-color:var(--accent)}button.danger{color:#b42318}.top-actions{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}.editor{background:var(--soft)}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}label{display:block;font-weight:600}select,input{display:block;width:100%;margin-top:5px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit;min-height:44px}.wide{grid-column:1/-1}.checklist{max-height:280px;overflow-y:auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-top:7px}.check{display:flex;gap:9px;align-items:start;font-weight:400;border:1px solid var(--line);padding:8px;border-radius:8px;overflow-wrap:anywhere}.check input{width:20px;min-height:20px;margin:2px 0 0}.notice{padding:12px;border-radius:9px;background:var(--soft);margin:12px 0}.error{color:#b42318}.empty{padding:20px;border:1px dashed var(--line);border-radius:12px}.editor h2{margin-top:0}.hidden{display:none!important}
@media(max-width:600px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}.room{align-items:flex-start;flex-direction:column}.actions{width:100%}.actions button{flex:1}.checklist{grid-template-columns:1fr}}
</style></head><body><main><header><h1>Pièces et capteurs</h1><p>Choisissez les mesures de chaque pièce. Température et humidité sont requises; CO₂ et ouvrants restent facultatifs.</p></header>
<div id="status" class="notice" role="status">Chargement des pièces…</div><div class="top-actions" id="add-actions"><button class="primary" id="add">Ajouter une pièce</button></div>
<section id="rooms" aria-live="polite"></section>
<section id="editor" class="editor hidden" aria-labelledby="form-title"><h2 id="form-title">Nouvelle pièce</h2><form id="form"><div class="grid">
<label>Nom de la pièce<input id="name" required maxlength="64" autocomplete="off" placeholder="Ex. Chambre d’amis"></label>
<label>Température<select id="temperature" required></select></label><label>Humidité<select id="humidity" required></select></label><label>CO₂ <span class="muted">(facultatif)</span><select id="co2"></select></label>
<div class="wide"><label>Fenêtres et portes <span class="muted">(facultatif)</span></label><input id="contact-search" type="search" placeholder="Rechercher une fenêtre ou une porte" aria-label="Rechercher un ouvrant"><div id="contacts" class="checklist"></div></div></div><p id="form-error" class="error" role="alert"></p><div class="top-actions"><button class="primary" type="submit">Enregistrer</button><button type="button" id="cancel">Annuler</button></div></form></section>
</main><script>
const $=id=>document.getElementById(id);let rooms=[],entities=[],advice={},baseRooms=[],editing=null;
function status(message,error=false){$('status').textContent=message;$('status').classList.toggle('error',error)}
function roomKey(name){let s=name.normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'');if(!s)s='piece';if(!/^[a-z]/.test(s))s='piece_'+s;return s.slice(0,48)}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function labelFor(id){return entities.find(e=>e.entity_id===id)?.name||id}
function render(){let root=$('rooms');root.replaceChildren();if(!rooms.length){root.innerHTML='<div class="empty">Aucune pièce configurée.</div>';return}for(const r of rooms){let card=document.createElement('article');card.className='room';let current=advice['sensor.conseils_air_'+r.key],existing=baseRooms.some(x=>x.key===r.key);card.innerHTML=`<div><strong>${esc(r.name)}</strong><small>${current?'Conseil actuel : '+esc(current.state):existing?'Conseil indisponible':'Conseil disponible après la première mesure'}</small>${current?.detail?'<small>'+esc(current.detail)+'</small>':''}${current?.perimetre?'<small>'+esc(current.perimetre)+'</small>':''}<small>${esc(labelFor(r.temperature))} · ${esc(labelFor(r.humidity))}${r.co2?' · '+esc(labelFor(r.co2)):''}${r.contacts?.length?' · '+r.contacts.map(labelFor).map(esc).join(', '):''}</small></div><div class="actions"><button data-edit="${esc(r.key)}">Modifier</button></div>`;root.append(card)}root.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openForm(rooms.find(x=>x.key===b.dataset.edit)))}
function optionsFor(kind,selected,optional=false){let list=entities.filter(e=>e.kind===kind);let absent=selected&&!list.some(e=>e.entity_id===selected);return (optional?'<option value="">Aucun</option>':'<option value="">Choisir…</option>')+(absent?`<option value="${esc(selected)}" selected>${esc(selected)} (indisponible)</option>`:'')+list.map(e=>`<option value="${esc(e.entity_id)}" ${e.entity_id===selected?'selected':''}>${esc(e.name)}${e.unit?' ('+esc(e.unit)+')':''}</option>`).join('')}
function openForm(room){editing=room?.key||null;$('form-title').textContent=room?'Modifier '+room.name:'Nouvelle pièce';$('name').value=room?.name||'';for(const field of ['temperature','humidity'])$(field).innerHTML=optionsFor(field,room?.[field]);$('co2').innerHTML=optionsFor('co2',room?.co2||'',true);$('contacts').replaceChildren();let contacts=entities.filter(e=>e.kind==='contact'),present=new Set(contacts.map(e=>e.entity_id));for(const id of room?.contacts||[])if(!present.has(id))contacts.push({entity_id:id,name:id+' (indisponible)'});for(const e of contacts){let label=document.createElement('label');label.className='check';label.innerHTML=`<input type="checkbox" value="${esc(e.entity_id)}" ${(room?.contacts||[]).includes(e.entity_id)?'checked':''}><span>${esc(e.name)}</span>`;$('contacts').append(label)}$('form-error').textContent='';$('contact-search').value='';$('rooms').classList.add('hidden');$('add-actions').classList.add('hidden');$('editor').classList.remove('hidden');$('name').focus();$('editor').scrollIntoView({behavior:'smooth',block:'start'})}
function closeForm(){$('editor').classList.add('hidden');$('rooms').classList.remove('hidden');$('add-actions').classList.remove('hidden');editing=null}
async function api(path,options){let base=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';let r=await fetch(base+path,options);let data=await r.json();if(!r.ok)throw Error(data.error||'Erreur de communication');return data}
async function init(){try{let [c,e]=await Promise.all([api('api/config'),api('api/entities')]);rooms=c.rooms||[];baseRooms=JSON.parse(JSON.stringify(rooms));advice=c.advice||{};entities=e.entities||[];render();status('Choisissez les capteurs puis enregistrez vos changements.')}catch(e){status(e.message,true)}}
$('contact-search').oninput=()=>{let q=$('contact-search').value.toLocaleLowerCase('fr');$('contacts').querySelectorAll('label').forEach(l=>l.classList.toggle('hidden',!l.textContent.toLocaleLowerCase('fr').includes(q)))};
$('add').onclick=()=>openForm();$('cancel').onclick=closeForm;
$('form').onsubmit=async e=>{e.preventDefault();let name=$('name').value.trim(),key=editing||roomKey(name),room={key,name,temperature:$('temperature').value,humidity:$('humidity').value,co2:$('co2').value,contacts:[...$('contacts').querySelectorAll('input:checked')].map(x=>x.value)};if(rooms.some(x=>x.key===key&&x.key!==editing)){ $('form-error').textContent='Une pièce utilise déjà cet identifiant. Modifiez le nom.';return}let nextRooms;if(editing){let old=rooms.find(x=>x.key===editing);room.decision_entity=old?.decision_entity;nextRooms=rooms.map(x=>x.key===editing?room:x)}else nextRooms=[...rooms,room];let submit=$('form').querySelector('[type=submit]');submit.disabled=true;status('Enregistrement et redémarrage…');try{await api('api/rooms',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({rooms:nextRooms,base_rooms:baseRooms})});rooms=nextRooms;baseRooms=JSON.parse(JSON.stringify(rooms));closeForm();status('Configuration enregistrée. Conseils Air redémarre.');setTimeout(()=>location.reload(),4000)}catch(error){$('form-error').textContent=error.message;status(error.message,true)}finally{submit.disabled=false;render()}};
init();
setInterval(async()=>{try{let c=await api('api/config');advice=c.advice||{};render()}catch(e){advice={};render();status('Conseils indisponibles. Rechargez la page.',true)}},30000);
</script></body></html>'''
