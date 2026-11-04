"""Agente Wrestling

Legge i risultati dei tornei UWW elencati in eventi.txt (una pagina evento per riga)
e li scrive in un database Notion. Passa in rassegna tutti gli stili e tutte le
categorie di peso dell'evento. Variabili: NOTION_TOKEN, NOTION_DATABASE_ID.
Se DRY_RUN=true fa solo una prova: stampa i dati trovati senza scrivere su Notion.
"""
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
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

COLUMNS = {
    "p1": ["Player 1"],
    "noc1": ["NOC 1"],
    "p2": ["Player 2"],
    "noc2": ["NOC 2"],
    "gender": ["Gender"],
    "style": ["Stile"],
    "weight": ["Categoria"],
    "score": ["Score"],
    "round": ["Turno"],
    "tournament": ["Torneo"],
    "date": ["Data"],
    "source": ["Fonte", "Fonte (link)", "Link"],
    "id": ["ID univoco", "ID"],
}

ROUND_RE = re.compile(r"^(final \d-\d|repechage|\d+/\d+ final|qualif\.?|final)$", re.I)
BY_RE = re.compile(r"^by\s+(\S+)$", re.I)
SCORE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")
WEIGHT_RE = re.compile(r"^\s*\d+\s*kg\s*$", re.I)
STYLE_RE = re.compile(r"freestyle|greco|women", re.I)


def die(msg):
    print(f"ERRORE: {msg}")
    sys.exit(1)


# ------------------------------------------------------- lettura della pagina
def athlete_info(a):
    name = (a.get("title") or a.get_text(" ", strip=True)).strip()
    noc = ""
    img = a.find("img", alt=re.compile(r"^Flag of", re.I))
    if img:
        noc = img["alt"].split("of", 1)[1].strip().upper()
    else:
        img = a.find("img", src=re.compile(r"/flags/"))
        if img:
            m = re.search(r"/flags/[^/]+/([a-z]+)\.svg", img["src"])
            noc = m.group(1).upper() if m else ""
    return name, noc


def parse_bracket(html):
    """Incontri conclusi presenti nella pagina (una categoria di peso)."""
    soup = BeautifulSoup(html, "html.parser")
    elements = soup.find_all(True)
    position = {id(el): i for i, el in enumerate(elements)}
    rounds = []
    for i, el in enumerate(elements):
        if el.find(True) is None:
            t = el.get_text(" ", strip=True)
            if ROUND_RE.match(t):
                rounds.append((i, re.sub(r"\s+", " ", t)))

    found = {}
    for el in elements:
        if el.name != "span" or el.find(True) is not None:
            continue
        by = BY_RE.match(el.get_text(" ", strip=True))
        if not by:
            continue
        # risale fino al contenitore che racchiude esattamente due atleti
        node, links = el.parent, []
        while node is not None and node.name not in ("body", "html"):
            anchors, seen = [], []
            for a in node.select("a[href^='/athletes/']"):
                if a["href"] not in seen:
                    seen.append(a["href"])
                    anchors.append(a)
            if len(anchors) >= 2:
                links = anchors
                break
            node = node.parent
        if len(links) != 2:
            continue
        prev = el.find_previous_sibling("span")
        sc = SCORE_RE.search(prev.get_text(" ", strip=True)) if prev else None
        if not sc:
            sc = SCORE_RE.search(node.get_text(" ", strip=True))
        if not sc:
            continue
        idx = position[id(el)]
        rnd = next((r for i, r in reversed(rounds) if i < idx), None)
        if not rnd:
            continue
        (n1, noc1), (n2, noc2) = athlete_info(links[0]), athlete_info(links[1])
        m = {
            "p1": n1, "noc1": noc1, "p2": n2, "noc2": noc2, "round": rnd,
            "score": f"{sc.group(1)}-{sc.group(2)} ({by.group(1).upper()})",
        }
        found[(rnd, n1, n2)] = m
    return list(found.values())


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
    ordered = [current] + [o for o in options if o and o != current]
    return ordered, current


def signature(found):
    return frozenset((m["round"], m["p1"], m["p2"]) for m in found)


def read_category(page, previous_sig):
    """Aspetta che la categoria selezionata sia davvero caricata e stabile, poi la legge.

    Controlla ogni 2 secondi (fino a 16): il contenuto deve comparire, non deve essere
    ancora quello della categoria precedente e deve restare uguale in due letture di fila.
    Se dopo l'attesa non c'e' nulla, la categoria viene considerata senza incontri conclusi.
    """
    last = None
    for _ in range(8):
        page.wait_for_timeout(2000)
        found = parse_bracket(page.content())
        sig = signature(found)
        if found and sig == previous_sig:
            last = None          # e' ancora la categoria precedente: continuo ad aspettare
            continue
        if found and sig == last:
            return found, sig    # contenuto stabile
        last = sig if found else None
    return [], None


def collect_event(url):
    """Passa in rassegna stili e categorie di peso dell'evento e restituisce gli incontri."""
    slug = urlparse(url).path.strip("/").split("/")[-1]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    matches = []
    previous_sig = None
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 2000})
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        tournament = page.title().split("|")[0].strip()
        try:
            styles, current = list_styles(page)
        except Exception as e:
            print(f"  Non riesco a leggere l'elenco degli stili ({e}): uso solo quello mostrato.")
            styles, current = ["Stile unico"], "Stile unico"
        for style in styles:
            if style != current:
                try:
                    pick_style(page, style)
                except Exception as e:
                    print(f"  Non riesco a passare allo stile {style}: {e}")
                    continue
            gender = "Women" if "women" in style.lower() else "Men"
            buttons = page.locator("button[data-slot='button']").filter(has_text=WEIGHT_RE)
            count = buttons.count()
            print(f"  Stile {style}: {count} categorie di peso")
            for i in range(count):
                try:
                    btn = page.locator("button[data-slot='button']").filter(has_text=WEIGHT_RE).nth(i)
                    weight = re.sub(r"\s+", " ", btn.inner_text().strip())
                    btn.click(timeout=10000)
                    for _ in range(10):
                        if btn.get_attribute("data-variant") == "default":
                            break
                        page.wait_for_timeout(1000)
                    found, sig = read_category(page, previous_sig)
                    if found:
                        previous_sig = sig
                except Exception as e:
                    print(f"    Categoria n.{i + 1}: problema ({e})")
                    continue
                print(f"    {weight}: {len(found)} incontri conclusi")
                if DRY_RUN:
                    for m in found[:2]:
                        print(f"        es.: {m['round']} | {m['p1']} ({m['noc1']}) vs "
                              f"{m['p2']} ({m['noc2']}) | {m['score']}")
                for m in found:
                    m.update({
                        "gender": gender, "style": style, "weight": weight,
                        "tournament": tournament, "date": today, "source": url,
                        "id": f"{slug}|{style}|{weight}|{m['round']}|{m['p1']}|{m['p2']}",
                    })
                matches.extend(found)
        browser.close()
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
        if DRY_RUN:
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
