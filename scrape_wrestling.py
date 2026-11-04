"""Agente Wrestling (versione dati strutturati)

Legge i risultati dei tornei UWW elencati in eventi.txt (una pagina evento per riga)
e li scrive in un database Notion. Per ogni stile e categoria di peso intercetta i dati
che la pagina stessa scarica (incontri completi di atleti, NOC, punti, vincitore e
data di fine), invece di interpretare il testo disegnato sullo schermo.
Variabili: NOTION_TOKEN, NOTION_DATABASE_ID.
Se DRY_RUN=true fa solo una prova: stampa i dati trovati senza scrivere su Notion.
"""
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
_raw_id = os.getenv("NOTION_DATABASE_ID", "").strip().replace("-", "")
_found = re.search(r"[0-9a-fA-F]{32}", _raw_id)
DATABASE_ID = _found.group(0) if _found else _raw_id
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() == "true"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Nomi delle colonne nel database (maiuscole/minuscole non contano).
COLUMNS = {
    "p1": ["Player 1"],
    "noc1": ["NOC 1"],
    "p2": ["Player 2"],
    "noc2": ["NOC 2"],
    "gender": ["Gender"],
    "style": ["Stile"],
    "weight": ["Categoria"],
    "score": ["Score"],
    "winner": ["Vincitore"],
    "round": ["Turno"],
    "tournament": ["Torneo"],
    "date": ["Data"],
    "source": ["Fonte", "Fonte (link)", "Link"],
    "id": ["ID univoco", "ID"],
}
OPTIONAL = {"winner"}  # se la colonna manca, il programma lavora lo stesso

WEIGHT_RE = re.compile(r"^\s*\d+\s*kg\s*$", re.I)
STYLE_RE = re.compile(r"freestyle|greco|women", re.I)
STYLE_NAMES = {"fs": "Freestyle", "gr": "Greco-Roman"}


def die(msg):
    print(f"ERRORE: {msg}")
    sys.exit(1)


# ------------------------------------------------------ lettura dei dati UWW
def clean(value):
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


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


def fighter_info(entry):
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
    return {"name": name, "noc": noc}


def fight_to_match(f, tournament, url, slug):
    """Trasforma un incontro in una riga; None se non e' concluso o e' incompleto."""
    e1, e2 = fighter_info(f.get("entry1")), fighter_info(f.get("entry2"))
    if not e1 or not e2 or not e1["name"] or not e2["name"]:
        return None
    winner = fighter_info(f.get("winner"))
    if not winner or not winner["name"]:
        return None
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
        "p1": e1["name"], "noc1": e1["noc"], "p2": e2["name"], "noc2": e2["noc"],
        "gender": gender, "style": style, "weight": weight, "score": score,
        "winner": winner["name"], "round": rnd, "tournament": tournament,
        "date": date, "source": url,
    }


# ------------------------------------------------------- navigazione pagina
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


def collect_event(url):
    """Passa in rassegna stili e categorie di peso e restituisce gli incontri conclusi."""
    slug = urlparse(url).path.strip("/").split("/")[-1]
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
        browser.close()

    fights, seen = [], set()
    for text in captures:
        for f in parse_fights(text):
            key = f.get("id") or json.dumps(f, sort_keys=True)[:200]
            if key not in seen:
                seen.add(key)
                fights.append(f)

    stats, matches = {}, []
    for f in fights:
        m = fight_to_match(f, tournament, url, slug)
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
    return matches


# ------------------------------------------------------------------- Notion
def notion(method, path, **kwargs):
    r = None
    for attempt in range(4):
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
    props = notion("GET", f"/databases/{DATABASE_ID}")["properties"]
    by_name = {n.strip().lower(): (n, p["type"]) for n, p in props.items()}
    mapping, missing = {}, []
    for key, options in COLUMNS.items():
        hit = next((by_name[o.lower()] for o in options if o.lower() in by_name), None)
        if hit:
            mapping[key] = hit
        elif key in OPTIONAL:
            print(f"Nota: nel database non c'è la colonna '{options[0]}': quel dato non verrà scritto.")
        else:
            missing.append(options[0])
    titles = [(n, p["type"]) for n, p in props.items() if p["type"] == "title"]
    mapping["title"] = titles[0]
    if missing:
        die("nel database non trovo queste colonne: " + ", ".join(missing)
            + ". Colonne presenti: " + ", ".join(props.keys()))
    return mapping


def existing_ids(id_column):
    ids, cursor = set(), None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion("POST", f"/databases/{DATABASE_ID}/query", json=body)
        for page in data["results"]:
            prop = page["properties"].get(id_column, {})
            parts = prop.get("rich_text") or prop.get("title") or []
            text = "".join(p.get("plain_text", "") for p in parts)
            if text:
                ids.add(text)
        if not data.get("has_more"):
            return ids
        cursor = data["next_cursor"]


def notion_value(ptype, value):
    value = "" if value is None else str(value)
    if ptype == "title":
        return {"title": [{"text": {"content": value[:2000]}}]}
    if ptype == "rich_text":
        return {"rich_text": [{"text": {"content": value[:2000]}}]}
    if ptype == "select":
        return {"select": {"name": value.replace(",", " ")[:100]} if value else None}
    if ptype == "date":
        return {"date": {"start": value} if value else None}
    if ptype == "url":
        return {"url": value or None}
    raise RuntimeError(f"Tipo di colonna non gestito: {ptype}. Usa testo, selezione, data o URL.")


def create_row(mapping, m):
    values = dict(m)
    values["title"] = f"{m['p1']} vs {m['p2']}"
    props = {name: notion_value(ptype, values.get(key, "")) for key, (name, ptype) in mapping.items()}
    notion("POST", "/pages", json={"parent": {"database_id": DATABASE_ID}, "properties": props})


# --------------------------------------------------------------------- main
def read_urls():
    with open("eventi.txt", encoding="utf-8") as f:
        return [r.strip() for r in f if r.strip() and not r.strip().startswith("#")]


def main():
    if not DRY_RUN and (not NOTION_TOKEN or not DATABASE_ID):
        die("mancano NOTION_TOKEN o NOTION_DATABASE_ID (controlla i Secrets su GitHub).")
    mapping, done = None, set()
    if not DRY_RUN:
        mapping = read_schema()
        done = existing_ids(mapping["id"][0])
        print(f"Righe già presenti in Notion: {len(done)}")
    else:
        print("MODALITÀ PROVA: non scrivo nulla su Notion.")

    errors = added = 0
    for url in read_urls():
        print(f"\nLeggo {url}")
        try:
            matches = collect_event(url)
        except Exception as e:
            print(f"  Evento non leggibile: {e}")
            errors += 1
            continue
        unique = {m["id"]: m for m in matches}
        new = [m for m in unique.values() if m["id"] not in done]
        print(f"  Incontri conclusi: {len(unique)}, nuovi: {len(new)}")
        no_date = sum(1 for m in new if not m["date"])
        if no_date:
            print(f"  Attenzione: {no_date} incontri senza data (colonna Data lasciata vuota).")
        if DRY_RUN:
            shown = {}
            for m in new:
                key = (m["style"], m["weight"])
                if shown.get(key, 0) < 2:
                    shown[key] = shown.get(key, 0) + 1
                    print(f"  [{m['gender']}] {m['style']} {m['weight']} | {m['round']} | "
                          f"{m['p1']} ({m['noc1']}) vs {m['p2']} ({m['noc2']}) | {m['score']} | "
                          f"vince {m['winner']} | {m['date']}")
            continue
        for m in new:
            try:
                create_row(mapping, m)
                done.add(m["id"])
                added += 1
                time.sleep(0.4)
            except RuntimeError as e:
                print(f"  Errore su {m['p1']} vs {m['p2']}: {e}")
                errors += 1
    if DRY_RUN:
        print("\nProva terminata (per ogni categoria sono mostrati due incontri di esempio).")
    else:
        print(f"\nFatto. Righe aggiunte: {added}. Errori: {errors}.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
