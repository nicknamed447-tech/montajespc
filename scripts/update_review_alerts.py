#!/usr/bin/env python3
"""Find recent CPU/GPU review reports about serious faults and persist evidence."""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app.js"
OUTPUT = ROOT / "review-alerts.json"
HEADERS = {"User-Agent": "MontajesPC-ReviewMonitor/1.0 (daily public review and issue monitoring)"}

# Search by product family, then require an exact catalog model match in each result.
# The queries cover professional testing and user-reported incidents.
SEARCHES = [
    ("CPU AMD", 'AMD Ryzen (CPU OR processor) (review OR users OR owners) (failure OR faulty OR defect OR instability OR overheating OR burned OR crash OR recall OR degradation OR issue)'),
    ("CPU Intel", 'Intel (Core OR Core Ultra) (CPU OR processor) (review OR users OR owners) (failure OR faulty OR defect OR instability OR overheating OR burned OR crash OR recall OR degradation OR issue)'),
    ("GPU Nvidia", 'Nvidia (GeForce OR RTX OR GTX) (GPU OR graphics card) (review OR users OR owners) (failure OR faulty OR defect OR instability OR overheating OR burned OR crash OR recall OR issue)'),
    ("GPU AMD", 'AMD (Radeon OR RX) (GPU OR graphics card) (review OR users OR owners) (failure OR faulty OR defect OR instability OR overheating OR burned OR crash OR recall OR issue)'),
    ("GPU Intel", 'Intel Arc GPU (review OR users OR owners) (failure OR faulty OR defect OR instability OR overheating OR burned OR crash OR recall OR issue)'),
]
FAILURE_TERMS = re.compile(r"\b(fail(?:s|ed|ure|ures|ing)?|faulty|defect(?:ive)?|instabil\w*|overheat\w*|burn(?:ed|t|ing)?|meltdown|degrad\w*|recall\w*|crash\w*|black screen|shutdown\w*|broken|bricked|rma|fall[oa]s?|aver[ií]a\w*|defectuos\w*|inestab\w*|sobrecalent\w*|quemad\w*|degradaci[oó]n|retirad[oa]s?|bloquead\w*)\b", re.I)
NEGATED_TERMS = re.compile(r"\b(no|not|without|never|none|avoid|zero|sin|ning[uú]n|ninguna|no hay|no se han)\b.{0,28}\b(fail\w*|fault\w*|defect\w*|issue\w*|problem\w*|fall[oa]s?|aver[ií]as?)\b", re.I)


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str):
        if data.strip():
            self.parts.append(data.strip())


def clean(value: str) -> str:
    parser = TextOnly()
    parser.feed(html.unescape(value or ""))
    return " ".join(parser.parts)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def parse_date(value: str):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None


def products_from_app():
    source = APP_JS.read_text(encoding="utf-8")
    products = []
    pattern = re.compile(r"kind:'(cpu|gpu)',brand:'([^']+)',name:'([^']+)'", re.I)
    for kind, brand, name in pattern.findall(source):
        products.append({"kind": kind.lower(), "brand": brand, "name": name})
    # Longest names first to avoid matching RTX 4070 inside RTX 4070 Ti.
    return sorted({(p["kind"], p["brand"], p["name"]): p for p in products}.values(), key=lambda p: len(p["name"]), reverse=True)


def news_entries(query: str):
    params = urllib.parse.urlencode({"q": query + " when:30d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    url = "https://news.google.com/rss/search?" + params
    root = ET.fromstring(fetch(url))
    for node in root.findall(".//item"):
        yield {
            "title": clean(node.findtext("title", default="")),
            "url": node.findtext("link", default=""),
            "summary": clean(node.findtext("description", default="")),
            "published": parse_date(node.findtext("pubDate", default="")),
            "source": clean(node.findtext("source", default="")),
        }


def model_mentions(text: str, products):
    text_norm = re.sub(r"[^a-z0-9]+", " ", text.lower())
    matches = []
    for product in products:
        name_norm = re.sub(r"[^a-z0-9]+", " ", product["name"].lower()).strip()
        if not name_norm:
            continue
        for found in re.finditer(r"(?<![a-z0-9])" + re.escape(name_norm) + r"(?![a-z0-9])", text_norm):
            matches.append((found.start(), found.end(), product))
    # Prefer the full SKU when model names overlap (e.g. RTX 4070 and RTX 4070 Ti).
    return [product for start, end, product in matches if not any(
        other_start <= start and end <= other_end and other_end - other_start > end - start
        for other_start, other_end, _ in matches
    )]


def find_alert_entries(products):
    recent_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    seen_urls = set()
    alerts = []
    success_count = 0
    for label, query in SEARCHES:
        try:
            entries = list(news_entries(query))
            success_count += 1
            print(f"{label}: {len(entries)} resultados consultados")
        except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError) as error:
            print(f"No se pudo consultar {label}: {error}")
            continue
        for entry in entries:
            if not entry["url"] or entry["url"] in seen_urls or not entry["published"] or entry["published"] < recent_cutoff:
                continue
            seen_urls.add(entry["url"])
            evidence = f"{entry['title']} {entry['summary']}"
            if not FAILURE_TERMS.search(evidence) or NEGATED_TERMS.search(evidence):
                continue
            for product in model_mentions(evidence, products):
                # The exact model must occur in the headline/snippet and the report must
                # name an explicit fault indicator; broad family-only matches are ignored.
                alerts.append({
                    "kind": product["kind"],
                    "brand": product["brand"],
                    "model": product["name"],
                    "title": entry["title"] or "Reporte reciente sobre un posible fallo",
                    "url": entry["url"],
                    "date": entry["published"].date().isoformat(),
                    "source": entry["source"] or label,
                    "warning": "Este reporte menciona un posible fallo o incidencia. Comprueba el artículo y confirma si afecta a esta variante exacta.",
                })
    if success_count == 0:
        raise RuntimeError("No se pudo consultar ninguna fuente de noticias para las reviews.")
    return alerts


def main():
    products = products_from_app()
    old = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {"alerts": []}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
    kept = [item for item in old.get("alerts", []) if item.get("date", "") >= cutoff and item.get("url")]
    by_source = {(item.get("kind"), item.get("model", "").casefold(), item.get("url")): item for item in kept}
    fresh = find_alert_entries(products)
    by_source.update({(item["kind"], item["model"].casefold(), item["url"]): item for item in fresh})
    result = {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "windowDays": 30,
        "alerts": sorted(by_source.values(), key=lambda item: (item.get("date", ""), item.get("model", "")), reverse=True),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Advertencias activas guardadas: {len(result['alerts'])}")


if __name__ == "__main__":
    main()
