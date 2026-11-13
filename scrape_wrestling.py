"""Agente Wrestling – risultati UWW (versione 2 del 13/11/2026, scheda 22, sessione 1)

Che cosa fa
-----------
Legge i risultati dei tornei UWW elencati in eventi.txt e li scrive nel database Notion
"🤼 Agente Wrestling". Per ogni stile e categoria di peso intercetta i dati che la pagina
UWW stessa scarica (incontri completi di atleti, NOC, punti, vincitore e data di fine),
invece di interpretare il testo disegnato sullo schermo.

Novità della versione 2 (scheda 22 §11)
--------------------------------------
- scrive il codice UWW dei due lottatori nelle colonne "ID UWW 1" e "ID UWW 2"
  (servono all'ELO: lo stesso lottatore viene riconosciuto anche se il nome è scritto
  in modo diverso da un torneo all'altro);
- scrive "Tipo Torneo", preso da eventi.txt, che ora ha il formato   URL ; tipo
- modalità "completa": per gli incontri già presenti in Notion aggiorna SOLO
  "ID UWW 1", "ID UWW 2" e "Tipo Torneo", e solo se sono vuote. Non archivia nulla
  e non tocca le altre colonne (es. la correzione a mano MIHAI-RUSU resta);
- "Solo prova" con diagnosi: campi dell'atleta nei dati UWW, stabilità del codice tra
  tornei diversi, righe di Notion ritrovate, righe da proporre per eventi.txt;
- limite di tempo: dopo TEMPO_MAX_MINUTI smette di scrivere in ordine; il lancio
  successivo riprende da dove si era fermato.

Variabili d'ambiente (arrivano dal workflow)
--------------------------------------------
NOTION_TOKEN, NOTION_DATABASE_ID   i due segreti del repository
DRY_RUN=true                       "Solo prova": legge UWW e Notion, stampa le diagnosi,
                                   non scrive nulla
MODALITA                           "aggiungi" (default): scrive solo gli incontri nuovi
                                   "completa": in più completa le righe già presenti
TEMPO_MAX_MINUTI                   dopo quanti minuti smettere di scrivere (default 165)
"""
import json
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

# Orologio di partenza: serve per fermarsi in ordine prima del limite di tempo di GitHub.
AVVIO = time.monotonic()

# ------------------------------------------------------------------ impostazioni
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
# l'id del database si può incollare anche come link intero: ne estraggo i 32 caratteri
_raw_id = os.getenv("NOTION_DATABASE_ID", "").strip().replace("-", "")
_found = re.search(r"[0-9a-fA-F]{32}", _raw_id)
DATABASE_ID = _found.group(0) if _found else _raw_id
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() == "true"
MODALITA = os.getenv("MODALITA", "").strip().lower() or "aggiungi"
FILE_EVENTI = os.getenv("FILE_EVENTI", "").strip() or "eventi.txt"
try:
    TEMPO_MAX_MINUTI = float(os.getenv("TEMPO_MAX_MINUTI", "").strip() or 165)
except ValueError:
    TEMPO_MAX_MINUTI = 165.0

PAUSA_NOTION = 0.35      # secondi tra una scrittura e l'altra (Notion accetta circa 3 richieste al secondo)
SECONDI_PER_RIGA = 0.75  # stima del tempo reale per riga (pausa + risposta di Notion), per il preventivo

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Nomi ESATTI delle colonne del db "🤼 Agente Wrestling" (scheda 22 §2.1 e §4.1).
# Il codice li cerca senza distinguere maiuscole e minuscole.
# La colonna titolo ("Incontro") viene riconosciuta da sola perché è l'unica di tipo titolo.
COLUMNS = {
    "p1": ["Player 1"],
    "noc1": ["NOC 1"],
    "id1": ["ID UWW 1"],          # NOVITÀ: codice UWW del lottatore in Player 1
    "p2": ["Player 2"],
    "noc2": ["NOC 2"],
    "id2": ["ID UWW 2"],          # NOVITÀ: codice UWW del lottatore in Player 2
    "gender": ["Gender"],
    "style": ["Stile"],
    "weight": ["Categoria"],
    "score": ["Score"],
    "winner": ["Vincitore"],
    "round": ["Turno"],
    "tournament": ["Torneo"],
    "kind": ["Tipo Torneo"],      # NOVITÀ: preso da eventi.txt
    "date": ["Data"],
    "source": ["Fonte", "Fonte (link)", "Link"],
    "id": ["ID Univoco", "ID"],
}
OPTIONAL = {"winner"}  # se manca, il programma lavora lo stesso; tutte le altre sono obbligatorie

# Le SOLE colonne che la modalità "completa" può scrivere in una riga già esistente,
# e solo se sono vuote (scheda 22 §11.3). Ogni altra colonna è bloccata da aggiorna_riga.
COMPLETABILI = ("id1", "id2", "kind")

# Le 6 voci ammesse di "Tipo Torneo" (scheda 22 §4.1 e §5.2), scritte esattamente così.
TIPI_TORNEO = ["Olimpiadi", "Mondiali", "Campionati continentali", "Giochi continentali",
               "Ranking Series", "Altri tornei"]

# Tipo deciso da Gualtiero per i 9 tornei presenti nel db al 13/11/2026 (scheda 22 §2.1).
# Serve SOLO alla "Solo prova", per proporre le righe mancanti di eventi.txt.
TIPO_NOTO = {
    "islamic solidarity games": "Giochi continentali",
    "zagreb open": "Ranking Series",
    "muhamet malo": "Ranking Series",
    "senior asian championships": "Campionati continentali",
    "senior european championships": "Campionati continentali",
    "ulaanbaatar open": "Ranking Series",
    "polyák imre varga jános & kozma istván memorial": "Ranking Series",
    "asian games": "Giochi continentali",
    "senior world championships": "Mondiali",
}

WEIGHT_RE = re.compile(r"^\s*\d+\s*kg\s*$", re.I)
STYLE_RE = re.compile(r"freestyle|greco|women", re.I)
STYLE_NAMES = {"fs": "Freestyle", "gr": "Greco-Roman"}

# Dove cercare il codice dell'ATLETA nei dati UWW, in quest'ordine (scheda 22 §11.2).
# Attenzione: l'oggetto "entry" è l'iscrizione al singolo torneo, il suo id cambia a ogni
# torneo; serve l'id della persona, dentro "entry.athlete". La "Solo prova" mostra tutti
# i campi, così si può correggere questo elenco se il nome del campo è diverso.
CHIAVI_ID_ATLETA = ("id", "uuid", "athlete_id", "person_id")


def die(msg):
    print(f"ERRORE: {msg}")
    sys.exit(1)


def minuti_trascorsi():
    return (time.monotonic() - AVVIO) / 60


def tempo_scaduto():
    """Vero quando è ora di smettere di scrivere, per non essere interrotti a metà da GitHub."""
    return minuti_trascorsi() >= TEMPO_MAX_MINUTI


# ------------------------------------------------------------------ diagnosi
# Contenitore unico delle informazioni che la "Solo prova" deve mostrare (scheda 22 §11.2).
DIAG = {
    "campi_entry": None,           # campi dell'oggetto "entry" (una volta sola)
    "campi_atleta": None,          # campi dell'oggetto "entry.athlete" con un valore d'esempio
    "fonte_id": Counter(),         # da quale campo è stato preso il codice dell'atleta
    "senza_id": 0,                 # lottatori senza codice
    "persone": defaultdict(lambda: defaultdict(set)),   # nome -> codice -> eventi in cui compare
    "nomi_per_id": defaultdict(set),                     # codice -> nomi diversi con cui compare
}


# ------------------------------------------------------ lettura dei dati UWW
def clean(value):
    """Toglie spazi doppi e a capo da un testo; restituisce "" se il valore non è un testo."""
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def norm(value):
    """Testo per i confronti: spazi doppi tolti, maiuscole e minuscole uguali."""
    return clean(value).casefold()


def real(value):
    """Valore utilizzabile (scarta i segnaposto '$undefined' ecc.)."""
    if isinstance(value, str) and value.startswith("$") and not value.startswith("$D"):
        return None
    return value


def fix_encoding(s):
    """Ripara le lettere accentate rovinate (es. 'NoÃ©mi' -> 'Noémi').

    La risposta dati non dichiara la codifica, quindi il browser la legge come testo
    Windows-1252 invece che UTF-8. Se il testo e' gia' corretto lo lascia com'e'.
    """
    raw = bytearray()
    for ch in s:
        o = ord(ch)
        if o < 128:
            raw.append(o)
            continue
        try:
            raw += ch.encode("cp1252")
        except UnicodeEncodeError:
            if o < 256:
                raw.append(o)
            else:
                return s
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return s


def parse_fights(text):
    """Estrae gli incontri dalla risposta dati della pagina."""
    fights = []
    for line in text.split("\n"):
        m = re.match(r"^[0-9a-zA-Z]+:(.*)$", line, re.S)
        if not m:
            continue
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, list):
            fights += [f for f in data if isinstance(f, dict) and "entry1" in f and "entry2" in f]
    return fights


def id_atleta(entry):
    """Codice UWW della PERSONA che lotta (non dell'iscrizione al torneo).

    Restituisce (codice, nome del campo da cui è stato preso); ("", "") se non c'è.
    """
    if not isinstance(entry, dict):
        return "", ""
    ath = entry.get("athlete") if isinstance(entry.get("athlete"), dict) else {}
    for k in CHIAVI_ID_ATLETA:
        v = real(ath.get(k))
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip(), f"athlete.{k}"
    # in alternativa, un campo dell'iscrizione che punta alla persona
    for k in ("athlete_id", "person_id"):
        v = real(entry.get(k))
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip(), f"entry.{k}"
    return "", ""


def fighter_info(entry):
    """Nome, NOC e codice UWW di un lottatore (entry1, entry2 o winner)."""
    if not isinstance(entry, dict):
        return None
    ath = entry.get("athlete") if isinstance(entry.get("athlete"), dict) else {}
    nm = ath.get("name")
    full = ""
    if isinstance(nm, dict):
        full = clean(nm.get("fullname")) or clean(
            " ".join(x for x in (nm.get("firstname"), nm.get("lastname")) if isinstance(x, str)))
    team = entry.get("team") if isinstance(entry.get("team"), dict) else {}
    team_country = team.get("country") if isinstance(team.get("country"), dict) else {}
    ath_country = ath.get("country") if isinstance(ath.get("country"), dict) else {}
    name = full or clean(ath.get("title")) or clean(team.get("name"))
    noc = real(team.get("code")) or real(team_country.get("ioc_code")) or real(ath_country.get("ioc_code")) or ""
    pid, fonte = id_atleta(entry)
    return {"name": name, "noc": noc, "pid": pid, "fonte_id": fonte}


def breve(v, n=60):
    """Valore d'esempio accorciato, per la diagnosi."""
    t = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return t if len(t) <= n else t[:n] + "…"


def osserva(f, slug, e1, e2):
    """Raccoglie per la diagnosi i campi dell'atleta e la stabilità del codice."""
    if DIAG["campi_entry"] is None and isinstance(f.get("entry1"), dict):
        entry = f["entry1"]
        DIAG["campi_entry"] = {k: breve(v) for k, v in entry.items() if k != "athlete"}
        ath = entry.get("athlete") if isinstance(entry.get("athlete"), dict) else {}
        DIAG["campi_atleta"] = {k: breve(v) for k, v in ath.items()}
    for e in (e1, e2):
        if not e["pid"]:
            DIAG["senza_id"] += 1
            continue
        DIAG["fonte_id"][e["fonte_id"]] += 1
        DIAG["persone"][norm(e["name"])][e["pid"]].add(slug)
        DIAG["nomi_per_id"][e["pid"]].add(e["name"])


def fight_to_match(f, tournament, url, slug, tipo):
    """Trasforma un incontro in una riga; None se non e' concluso o e' incompleto."""
    e1, e2 = fighter_info(f.get("entry1")), fighter_info(f.get("entry2"))
    if not e1 or not e2 or not e1["name"] or not e2["name"]:
        return None
    winner = fighter_info(f.get("winner"))
    if not winner or not winner["name"]:
        return None
    osserva(f, slug, e1, e2)
    sub = f.get("sub_event") if isinstance(f.get("sub_event"), dict) else {}
    wc = sub.get("weight_category") if isinstance(sub.get("weight_category"), dict) else {}
    sport = clean(wc.get("sport")).lower()
    gender_raw = clean(wc.get("gender")).lower()
    gender = {"male": "Men", "female": "Women"}.get(gender_raw, "")
    style = STYLE_NAMES.get(sport) or ("Women's Wrestling" if gender == "Women" else sport.upper())
    weight = f"{clean(str(wc.get('weight', '')))} kg" if wc.get("weight") else ""

    tp = f.get("technical_points") if isinstance(f.get("technical_points"), dict) else {}
    vtype = real(f.get("victory_type"))
    vtype = clean(vtype) if isinstance(vtype, str) else ""
    if tp.get("entry1") is not None and tp.get("entry2") is not None:
        score = f"{tp['entry1']}-{tp['entry2']}" + (f" ({vtype})" if vtype else "")
    else:
        score = f"({vtype})" if vtype else ""

    end = f.get("end")
    date = ""
    if isinstance(end, str):
        end = end[2:] if end.startswith("$D") else end
        if re.match(r"\d{4}-\d{2}-\d{2}", end):
            date = end[:10]

    rnd = clean(real(f.get("round")) or "")
    fid = clean(str(f.get("id") or "")) or f"{style}|{weight}|{rnd}|{e1['name']}|{e2['name']}"
    return {
        "id": f"{slug}|{fid}",
        "p1": e1["name"], "noc1": e1["noc"], "id1": e1["pid"],
        "p2": e2["name"], "noc2": e2["noc"], "id2": e2["pid"],
        "gender": gender, "style": style, "weight": weight, "score": score,
        "winner": winner["name"], "round": rnd, "tournament": tournament, "kind": tipo,
        "date": date, "source": url,
    }


# ------------------------------------------------------- navigazione pagina (invariata)
def pick_style(page, style):
    trigger = page.locator("[data-slot='select-trigger']").filter(has_text=STYLE_RE).first
    trigger.click(timeout=10000)
    page.wait_for_timeout(1000)
    page.get_by_role("option", name=style, exact=True).first.click(timeout=10000)
    page.wait_for_timeout(5000)


def list_styles(page):
    trigger = page.locator("[data-slot='select-trigger']").filter(has_text=STYLE_RE).first
    current = trigger.inner_text(timeout=10000).strip()
    trigger.click(timeout=10000)
    page.wait_for_timeout(1000)
    options = [o.inner_text().strip() for o in page.get_by_role("option").all()]
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    return [current] + [o for o in options if o and o != current], current


def slug_di(url):
    """'https://uww.org/events/asian-games-1?competition=…' -> 'asian-games-1'."""
    return urlparse(url).path.strip("/").split("/")[-1]


def collect_event(url, tipo):
    """Passa in rassegna stili e categorie di peso e restituisce gli incontri conclusi."""
    slug = slug_di(url)
    captures = []

    def on_response(resp):
        try:
            req = resp.request
            if req.method == "POST" and req.headers.get("next-action"):
                captures.append(fix_encoding(resp.text()))
        except Exception:
            pass

    # Nota: le attese usano page.wait_for_timeout (non time.sleep), perche' solo cosi'
    # Playwright elabora le risposte in arrivo mentre il programma aspetta.
    def wait_new(count, timeout=30):
        for _ in range(timeout):
            if len(captures) > count:
                return True
            page.wait_for_timeout(1000)
        return len(captures) > count

    def settle(max_wait=15):
        last, quiet = len(captures), 0
        for _ in range(max_wait):
            page.wait_for_timeout(1000)
            if len(captures) == last:
                quiet += 1
                if quiet >= 3:
                    return
            else:
                last, quiet = len(captures), 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 2000})
        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        tournament = clean(page.title().split("|")[0])
        try:
            styles, current = list_styles(page)
        except Exception as e:
            print(f"  Non riesco a leggere l'elenco degli stili ({e}): uso solo quello mostrato.")
            styles, current = [None], None
        for style in styles:
            if style and style != current:
                try:
                    pick_style(page, style)
                except Exception as e:
                    print(f"  Non riesco a passare allo stile {style}: {e}")
                    continue
            settle()
            buttons = page.locator("button[data-slot='button']").filter(has_text=WEIGHT_RE)
            count = buttons.count()
            print(f"  Stile {style or 'unico'}: {count} categorie di peso")
            # si cliccano prima le altre categorie e per ultima la prima, cosi' ogni clic
            # cambia davvero la categoria selezionata e provoca una richiesta dati
            order = list(range(1, count)) + [0] if count > 1 else [0]
            for i in order:
                n = len(captures)
                try:
                    btn = page.locator("button[data-slot='button']").filter(has_text=WEIGHT_RE).nth(i)
                    label = clean(btn.inner_text())
                    btn.click(timeout=10000)
                except Exception as e:
                    print(f"    Categoria n.{i + 1}: clic non riuscito ({e})")
                    continue
                if not wait_new(n):
                    print(f"    {label}: nessuna risposta dalla pagina entro 30 secondi")
            settle(20)
        try:
            browser.close()
        except Exception:
            pass

    fights, seen = [], set()
    for text in captures:
        for f in parse_fights(text):
            key = f.get("id") or json.dumps(f, sort_keys=True)[:200]
            if key not in seen:
                seen.add(key)
                fights.append(f)

    stats, matches = {}, []
    for f in fights:
        m = fight_to_match(f, tournament, url, slug, tipo)
        wc = (f.get("sub_event") or {}).get("weight_category") or {}
        label = (STYLE_NAMES.get(clean(wc.get("sport")).lower())
                 or ("Women's Wrestling" if clean(wc.get("gender")).lower() == "female" else clean(wc.get("sport")).upper()),
                 f"{wc.get('weight', '?')} kg")
        s = stats.setdefault(label, [0, 0])
        s[0] += 1
        if m:
            s[1] += 1
            matches.append(m)
    for (style, weight), (total, done) in stats.items():
        print(f"    {style} {weight}: {total} incontri nei dati, {done} conclusi")
    return matches, tournament


# ------------------------------------------------------------------- eventi.txt
def read_events():
    """Legge eventi.txt: una riga per evento nel formato   URL ; Tipo Torneo

    Si ferma con un messaggio chiaro se una riga non ha il tipo o il tipo è scritto male
    (scheda 22 §11.4: meglio fermarsi che assegnare un coefficiente sbagliato).
    """
    try:
        with open(FILE_EVENTI, encoding="utf-8") as f:
            righe = f.readlines()
    except FileNotFoundError:
        die(f"non trovo il file {FILE_EVENTI} nel repository.")
    eventi, problemi = [], []
    for n, riga in enumerate(righe, 1):
        riga = riga.strip()
        if not riga or riga.startswith("#"):
            continue
        if ";" not in riga:
            problemi.append(f"riga {n}: manca il tipo di torneo. Scrivi: URL ; uno di {TIPI_TORNEO}")
            continue
        url, tipo = (x.strip() for x in riga.split(";", 1))
        if not url.startswith("http"):
            problemi.append(f"riga {n}: l'indirizzo non comincia con http ({url[:60]})")
            continue
        if tipo not in TIPI_TORNEO:
            simile = next((t for t in TIPI_TORNEO if t.casefold() == tipo.casefold()), None)
            consiglio = f" Forse volevi «{simile}» (maiuscole esatte)." if simile else ""
            problemi.append(f"riga {n}: tipo «{tipo}» non valido.{consiglio} Tipi ammessi: {', '.join(TIPI_TORNEO)}")
            continue
        eventi.append((url, tipo))
    if problemi:
        die(f"il file {FILE_EVENTI} va corretto:\n  " + "\n  ".join(problemi))
    slugs = Counter(slug_di(u) for u, _ in eventi)
    doppi = [s for s, c in slugs.items() if c > 1]
    if doppi:
        die(f"in {FILE_EVENTI} lo stesso evento compare più volte: {', '.join(doppi)}")
    if not eventi:
        die(f"il file {FILE_EVENTI} non contiene nessun evento.")
    return eventi


# ------------------------------------------------------------------- Notion
def notion(method, path, **kwargs):
    """Chiamata all'API Notion; aspetta e riprova se Notion è occupato (429) o in errore (5xx)."""
    r = None
    for attempt in range(5):
        r = requests.request(method, "https://api.notion.com/v1" + path,
                             headers=NOTION_HEADERS, timeout=30, **kwargs)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 2)) + attempt)
            continue
        break
    if not r.ok:
        raise RuntimeError(f"Notion ha risposto {r.status_code}: {r.text[:400]}")
    return r.json()


def read_schema():
    """Legge le colonne del db e le associa ai dati. Si ferma se manca una colonna obbligatoria."""
    props = notion("GET", f"/databases/{DATABASE_ID}")["properties"]
    by_name = {n.strip().lower(): (n, p["type"]) for n, p in props.items() if p["type"] != "title"}
    mapping, missing = {}, []
    for key, options in COLUMNS.items():
        hit = next((by_name[o.lower()] for o in options if o.lower() in by_name), None)
        if hit:
            mapping[key] = hit
        elif key in OPTIONAL:
            print(f"Nota: nel database non c'è la colonna '{options[0]}': quel dato non verrà scritto.")
        else:
            missing.append(options[0])
    if missing:
        die("nel database non trovo queste colonne: " + ", ".join(missing)
            + ". Colonne presenti: " + ", ".join(props.keys())
            + ". (ID UWW 1 e ID UWW 2 vanno create di tipo Testo, Tipo Torneo di tipo Seleziona)")
    titles = [(n, p["type"]) for n, p in props.items() if p["type"] == "title"]
    mapping["title"] = titles[0]
    # controllo dei tipi delle colonne nuove (scheda 22 §4.1)
    attesi = {"id1": "rich_text", "id2": "rich_text", "kind": "select"}
    sbagliati = [f"{mapping[k][0]} è di tipo {mapping[k][1]}" for k, t in attesi.items() if mapping[k][1] != t]
    if sbagliati:
        die("tipo di colonna inatteso: " + "; ".join(sbagliati)
            + ". ID UWW 1 e ID UWW 2 devono essere Testo, Tipo Torneo Seleziona.")
    print("Colonne trovate in Notion: titolo '" + titles[0][0] + "' + "
          + ", ".join(f"{n} ({t})" for k, (n, t) in mapping.items() if k != "title"))
    return mapping


def valore_prop(prop):
    """Testo semplice di una proprietà Notion (titolo, testo, selezione o URL)."""
    if not prop:
        return ""
    t = prop.get("type")
    if t in ("title", "rich_text"):
        return "".join(p.get("plain_text", "") for p in prop.get(t) or []).strip()
    if t == "select":
        return ((prop.get("select") or {}).get("name") or "").strip()
    if t == "url":
        return (prop.get("url") or "").strip()
    return ""


def leggi_righe_esistenti(mapping):
    """Tutte le righe del db, raggruppate per ID Univoco.

    Restituisce  ID Univoco -> [ {page, p1, p2, id1, id2, kind, torneo, fonte}, ... ]
    (di norma una sola riga per ID Univoco).
    """
    col = {k: mapping[k][0] for k in ("id", "p1", "p2", "id1", "id2", "kind", "tournament", "source")}
    righe, senza_id, cursor = defaultdict(list), 0, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion("POST", f"/databases/{DATABASE_ID}/query", json=body)
        for page in data["results"]:
            p = page["properties"]
            v = {k: valore_prop(p.get(c)) for k, c in col.items()}
            if not v["id"]:
                senza_id += 1
                continue
            righe[v["id"]].append({"page": page["id"], "p1": v["p1"], "p2": v["p2"], "id1": v["id1"],
                                   "id2": v["id2"], "kind": v["kind"], "torneo": v["tournament"],
                                   "fonte": v["source"]})
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    if senza_id:
        print(f"Nota: {senza_id} righe in Notion non hanno 'ID Univoco': lo script le lascia stare.")
    return righe


def notion_value(ptype, value):
    """Converte un valore nel formato che Notion vuole per quel tipo di colonna."""
    value = "" if value is None else str(value)
    if ptype == "title":
        return {"title": [{"text": {"content": value[:2000]}}]}
    if ptype == "rich_text":
        return {"rich_text": [{"text": {"content": value[:2000]}}] if value else []}
    if ptype == "select":
        return {"select": {"name": value.replace(",", " ")[:100]} if value else None}
    if ptype == "date":
        return {"date": {"start": value} if value else None}
    if ptype == "url":
        return {"url": value or None}
    raise RuntimeError(f"Tipo di colonna non gestito: {ptype}. Usa testo, selezione, data o URL.")


def create_row(mapping, m):
    values = dict(m)
    values["title"] = f"{m['p1']} vs {m['p2']}"   # colonna "Incontro"
    props = {name: notion_value(ptype, values.get(key, "")) for key, (name, ptype) in mapping.items()}
    notion("POST", "/pages", json={"parent": {"database_id": DATABASE_ID}, "properties": props})


def da_completare(riga, m):
    """Quali colonne nuove si possono scrivere in una riga GIÀ ESISTENTE (modalità completa).

    Restituisce un dizionario {chiave: valore} (vuoto se non c'è nulla da fare),
    oppure None se i nomi della riga non corrispondono a quelli letti da UWW.
    Controllo di sicurezza: ID UWW 1 va al lottatore scritto in Player 1 della riga.
    Se UWW oggi elenca i due lottatori in ordine inverso, i codici vengono scambiati.
    """
    r1, r2, n1, n2 = norm(riga["p1"]), norm(riga["p2"]), norm(m["p1"]), norm(m["p2"])
    if (r1, r2) == (n1, n2):
        codici = (m["id1"], m["id2"])
    elif (r1, r2) == (n2, n1):
        codici = (m["id2"], m["id1"])
    else:
        return None
    nuovi = {}
    if not riga["id1"] and codici[0]:
        nuovi["id1"] = codici[0]
    if not riga["id2"] and codici[1]:
        nuovi["id2"] = codici[1]
    if not riga["kind"] and m["kind"]:
        nuovi["kind"] = m["kind"]
    return nuovi


def aggiorna_riga(mapping, page_id, nuovi):
    """Scrive in una riga ESISTENTE solo le colonne ammesse (ID UWW 1, ID UWW 2, Tipo Torneo).

    Qualsiasi altra colonna viene rifiutata: così una correzione fatta a mano (es. lo Score
    di MIHAI-RUSU) non può mai essere sovrascritta.
    """
    props = {}
    for chiave, valore in nuovi.items():
        if chiave not in COMPLETABILI:
            raise RuntimeError(f"tentativo di scrivere la colonna non ammessa '{chiave}': bloccato")
        nome, tipo = mapping[chiave]
        props[nome] = notion_value(tipo, valore)
    if props:
        notion("PATCH", f"/pages/{page_id}", json={"properties": props})


# ------------------------------------------------------------------ stampe
def stampa_diagnosi(esistenti, eventi_letti):
    print("\n" + "=" * 78)
    print("DIAGNOSI (scheda 22 §11.2)")
    print("=" * 78)

    print("\n[1] Campi dell'iscrizione al torneo (entry), esempio dal primo incontro letto:")
    for k, v in (DIAG["campi_entry"] or {}).items():
        print(f"    {k}: {v}")
    print("    Campi dell'atleta (entry.athlete):")
    for k, v in (DIAG["campi_atleta"] or {}).items():
        print(f"    {k}: {v}")

    print("\n[2] Codice dell'atleta")
    print("    Preso dal campo: " + (", ".join(f"{k} ({v} volte)" for k, v in DIAG["fonte_id"].most_common())
                                   or "NESSUNO: il codice non è stato trovato"))
    print(f"    Lottatori senza codice: {DIAG['senza_id']}")
    in_piu_eventi = {n: ids for n, ids in DIAG["persone"].items()
                     if len({s for eventi in ids.values() for s in eventi}) >= 2}
    stabili = {n: ids for n, ids in in_piu_eventi.items() if len(ids) == 1}
    instabili = {n: ids for n, ids in in_piu_eventi.items() if len(ids) > 1}
    print(f"    Lottatori (stesso nome) presenti in almeno 2 eventi: {len(in_piu_eventi)}")
    print(f"      con lo STESSO codice in tutti gli eventi: {len(stabili)}  ← deve essere quasi il 100%")
    print(f"      con codici DIVERSI: {len(instabili)}")
    for n, ids in list(instabili.items())[:10]:
        print(f"        · {n}: " + "; ".join(f"{c} in {', '.join(sorted(e))}" for c, e in ids.items()))
    for n, ids in list(stabili.items())[:5]:
        c, e = next(iter(ids.items()))
        print(f"        esempio stabile · {n}: {c} in {', '.join(sorted(e))}")
    grafie = {c: nomi for c, nomi in DIAG["nomi_per_id"].items() if len(nomi) > 1}
    print(f"    Codici usati con nomi scritti in modo diverso: {len(grafie)}")
    for c, nomi in list(grafie.items())[:10]:
        print(f"        · {c}: {' / '.join(sorted(nomi))}")

    if esistenti is None:
        return
    print("\n[3] Tornei presenti in Notion ma NON elencati in eventi.txt")
    letti = set(eventi_letti)
    mancanti = defaultdict(lambda: {"righe": 0, "da_completare": 0, "torneo": "", "fonte": ""})
    for uid, lista in esistenti.items():
        slug = uid.split("|")[0]
        if slug in letti:
            continue
        for r in lista:
            info = mancanti[slug]
            info["righe"] += 1
            info["da_completare"] += not (r["id1"] and r["id2"] and r["kind"])
            info["torneo"] = info["torneo"] or r["torneo"]
            info["fonte"] = info["fonte"] or r["fonte"]
    if not mancanti:
        print("    nessuno: eventi.txt copre tutti i tornei del db")
    else:
        for slug, info in sorted(mancanti.items(), key=lambda kv: kv[1]["torneo"]):
            print(f"    - {info['torneo'] or slug}: {info['righe']} righe, da completare {info['da_completare']}")
        print("\n    Righe pronte da incollare in eventi.txt (controlla il tipo; ??? = da scegliere):")
        for slug, info in sorted(mancanti.items(), key=lambda kv: kv[1]["torneo"]):
            tipo = TIPO_NOTO.get(norm(info["torneo"]), "???")
            print(f"    {info['fonte'] or 'https://uww.org/events/' + slug} ; {tipo}")


# --------------------------------------------------------------------- main
def main():
    if MODALITA not in ("aggiungi", "completa"):
        die(f"MODALITA deve essere 'aggiungi' o 'completa' (hai scritto '{MODALITA}').")
    if not DRY_RUN and (not NOTION_TOKEN or not DATABASE_ID):
        die("mancano NOTION_TOKEN o NOTION_DATABASE_ID (controlla i Secrets su GitHub).")
    eventi = read_events()
    print(f"{'SOLO PROVA: non scrivo nulla su Notion.' if DRY_RUN else 'SCRITTURA su Notion.'} "
          f"Modalità: {MODALITA}. Eventi in {FILE_EVENTI}: {len(eventi)}.")
    for url, tipo in eventi:
        print(f"  · {slug_di(url)} ({tipo})")

    # 1. stato del db (anche in prova, se il token c'è: è solo lettura)
    mapping, esistenti = None, None
    if NOTION_TOKEN and DATABASE_ID:
        try:
            mapping = read_schema()
            esistenti = leggi_righe_esistenti(mapping)
            incompleti = sum(1 for lista in esistenti.values() for r in lista
                             if not (r["id1"] and r["id2"] and r["kind"]))
            print(f"Righe già presenti in Notion: {sum(len(l) for l in esistenti.values())}, "
                  f"di cui senza ID UWW o Tipo Torneo: {incompleti}")
            if incompleti and MODALITA == "aggiungi":
                print("  (le righe vecchie restano come sono: per completarle usa la modalità 'completa')")
        except SystemExit:
            if not DRY_RUN:
                raise
            print("In prova continuo comunque, ma prima del lancio vero le colonne vanno sistemate.")
        except RuntimeError as e:
            if not DRY_RUN:
                raise
            print(f"Non riesco a leggere il db Notion ({e}). In prova continuo senza.")
    else:
        print("Token Notion assente: in prova non leggo il db.")

    # 2. lettura degli eventi e scrittura
    errori = aggiunte = completate = 0
    tot_nuovi = tot_da_completare = tot_nomi_diversi = tot_non_ritrovate = 0
    interrotto = False
    eventi_letti = []
    for url, tipo in eventi:
        if not DRY_RUN and tempo_scaduto():
            interrotto = True
            break
        slug = slug_di(url)
        print(f"\nLeggo {slug} ({tipo})")
        try:
            matches, torneo = collect_event(url, tipo)
        except Exception as e:
            print(f"  Evento non leggibile: {e}")
            errori += 1
            continue
        eventi_letti.append(slug)
        unique = {m["id"]: m for m in matches}
        presenti = esistenti or {}
        nuovi = [m for m in unique.values() if m["id"] not in presenti]

        # righe esistenti da completare (calcolate sempre, scritte solo in modalità completa)
        lavori, nomi_diversi = [], []
        for m in unique.values():
            for riga in presenti.get(m["id"], []):
                d = da_completare(riga, m)
                if d is None:
                    nomi_diversi.append(f"{riga['p1']} vs {riga['p2']} (Notion) ≠ {m['p1']} vs {m['p2']} (UWW)")
                elif d:
                    lavori.append((riga["page"], d))
        non_ritrovate = [uid for uid in presenti if uid.startswith(slug + "|") and uid not in unique]
        tot_nuovi += len(nuovi)
        tot_da_completare += len(lavori)
        tot_nomi_diversi += len(nomi_diversi)
        tot_non_ritrovate += len(non_ritrovate)
        senza_id = sum(1 for m in unique.values() for c in ("id1", "id2") if not m[c])
        print(f"  {torneo}: incontri conclusi {len(unique)} · nuovi {len(nuovi)} · "
              f"righe esistenti da completare {len(lavori)} · lottatori senza codice {senza_id}")
        if nomi_diversi:
            print(f"  ATTENZIONE: {len(nomi_diversi)} righe con nomi diversi tra Notion e UWW (non toccate):")
            for t in nomi_diversi[:5]:
                print(f"    · {t}")
        if non_ritrovate:
            print(f"  Nota: {len(non_ritrovate)} righe di questo evento in Notion non sono più nei dati UWW "
                  f"(restano senza codice), es. {non_ritrovate[0]}")
        no_date = sum(1 for m in nuovi if not m["date"])
        if no_date:
            print(f"  Attenzione: {no_date} incontri senza data (colonna Data lasciata vuota).")

        if DRY_RUN:
            shown = set()
            for m in unique.values():
                if m["style"] in shown:
                    continue
                shown.add(m["style"])
                print(f"  esempio [{m['gender']}] {m['style']} {m['weight']} | {m['round']} | "
                      f"{m['p1']} ({m['noc1']}, codice {m['id1'] or '—'}) vs "
                      f"{m['p2']} ({m['noc2']}, codice {m['id2'] or '—'}) | {m['score']} | vince {m['winner']}")
            continue

        # scrittura degli incontri nuovi (con codici e tipo torneo)
        for m in nuovi:
            if tempo_scaduto():
                interrotto = True
                break
            try:
                create_row(mapping, m)
                presenti.setdefault(m["id"], [])
                aggiunte += 1
                if aggiunte % 50 == 0:
                    print(f"  ... scritte {aggiunte} righe ({minuti_trascorsi():.0f} minuti)", flush=True)
                time.sleep(PAUSA_NOTION)
            except RuntimeError as e:
                print(f"  Errore su {m['p1']} vs {m['p2']}: {e}")
                errori += 1

        # completamento delle righe esistenti (solo modalità completa)
        if MODALITA == "completa" and not interrotto:
            for page_id, d in lavori:
                if tempo_scaduto():
                    interrotto = True
                    break
                try:
                    aggiorna_riga(mapping, page_id, d)
                    completate += 1
                    if completate % 100 == 0:
                        print(f"  ... completate {completate} righe ({minuti_trascorsi():.0f} minuti)", flush=True)
                    time.sleep(PAUSA_NOTION)
                except RuntimeError as e:
                    print(f"  Errore nel completare la riga {page_id}: {e}")
                    errori += 1
        if interrotto:
            break

    # 3. diagnosi e riepilogo
    stampa_diagnosi(esistenti, eventi_letti)
    print("\n" + "=" * 78)
    print("RIEPILOGO")
    print("=" * 78)
    print(f"Eventi letti: {len(eventi_letti)} su {len(eventi)} · incontri nuovi: {tot_nuovi} · "
          f"righe esistenti da completare: {tot_da_completare} · nomi diversi: {tot_nomi_diversi} · "
          f"righe non ritrovate nei dati UWW: {tot_non_ritrovate}")
    if DRY_RUN:
        operazioni = tot_nuovi + (tot_da_completare if MODALITA == "completa" else 0)
        print(f"Tempo stimato per la scrittura in Notion: circa {operazioni * SECONDI_PER_RIGA / 60:.0f} minuti "
              f"(più la lettura degli eventi; limite per lancio: {TEMPO_MAX_MINUTI:.0f} minuti).")
        print("\nProva terminata: non è stato scritto nulla su Notion.")
    else:
        print(f"Righe nuove scritte: {aggiunte} · righe completate: {completate} · errori: {errori} · "
              f"minuti: {minuti_trascorsi():.0f}")
    if interrotto:
        print(f"\nATTENZIONE: mi sono fermato dopo {TEMPO_MAX_MINUTI:.0f} minuti per non superare il limite di "
              f"GitHub. Rilancia con le stesse impostazioni: le righe già scritte o completate vengono saltate.")
        sys.exit(1)
    if errori:
        sys.exit(1)


if __name__ == "__main__":
    # Uscita forzata: se resta appeso un processo del browser, il lavoro viene
    # comunque chiuso subito invece di restare "In progress" su GitHub.
    exit_code = 0
    try:
        main()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
    except Exception:
        traceback.print_exc()
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
