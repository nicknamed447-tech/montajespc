#!/usr/bin/env python3
"""Refresh announced desktop CPU/GPU models from official newsroom sources."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app.js"
OUTPUT = ROOT / "catalog-updates.json"
SOURCES = {
    "amd": "https://ir.amd.com/news-events/press-releases/rss",
    "nvidia": "https://nvidianews.nvidia.com/releases.xml",
    "intel": "https://www.intel.com/content/www/us/en/newsroom/home.html",
}
HEADERS = {"User-Agent": "MontajesPC-CatalogUpdater/1.0 (daily public product announcement check)"}


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self.current = [attrs.get("href", ""), ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data + " "

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append((self.current[0], " ".join(self.current[1].split())))
            self.current = None


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())


MODEL_PATTERNS = [
    ("AMD", "cpu", re.compile(r"\bRyzen\s+[3579]\s+\d{4}\s*[A-Z0-9]*(?:\s+(?:Dual Edition|Plus))?", re.I)),
    ("AMD", "gpu", re.compile(r"\bRadeon\s+RX\s+\d{4}\s*(?:XT|XTX|GRE)?", re.I)),
    ("Nvidia", "gpu", re.compile(r"\bGeForce\s+(?:RTX|GTX)\s+\d{4}(?:\s+(?:Ti|SUPER|D|GRE|v\d))?", re.I)),
    ("Intel", "cpu", re.compile(r"\bCore\s+Ultra\s+[579]\s+\d{3,4}[A-Z]{0,3}(?:\s+Plus)?", re.I)),
    ("Intel", "cpu", re.compile(r"\bCore\s+i[3579]-\d{4,5}[A-Z]{0,3}", re.I)),
    ("Intel", "gpu", re.compile(r"\bArc\s+[AB]\d{3,4}(?:\s+Limited Edition)?", re.I)),
]


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read()


def clean_html(value: str) -> str:
    parser = TextOnly()
    parser.feed(html.unescape(value or ""))
    return " ".join(parser.parts)


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def existing_products():
    code = APP_JS.read_text(encoding="utf-8")
    names = {normalize(name) for name in re.findall(r"name:'([^']+)'", code)}
    for _, _, pattern in MODEL_PATTERNS:
        names.update(normalize(match.group(0)) for match in pattern.finditer(code))
    return names


def rss_entries(source: str, url: str):
    root = ET.fromstring(fetch(url))
    for node in root.findall(".//item"):
        title = node.findtext("title", default="")
        link = node.findtext("link", default="")
        summary = node.findtext("description", default="")
        date = node.findtext("pubDate", default="")
        yield title, link, clean_html(summary), date, source
    for node in root.findall(".//{*}entry"):
        title = node.findtext("{*}title", default="")
        link_node = node.find("{*}link")
        link = link_node.attrib.get("href", "") if link_node is not None else ""
        summary = node.findtext("{*}summary", default="") or node.findtext("{*}content", default="")
        date = node.findtext("{*}published", default="")
        yield title, link, clean_html(summary), date, source


def intel_entries():
    data = fetch(SOURCES["intel"]).decode("utf-8", errors="replace")
    parser = PageLinks()
    parser.feed(data)
    seen = set()
    for href, title in parser.links:
        title_low = title.lower()
        if not any(word in title_low for word in ("core", "processor", "gpu", "graphics", "arc")):
            continue
        link = urllib.parse.urljoin(SOURCES["intel"], href)
        if link in seen or "intel.com" not in urllib.parse.urlparse(link).netloc:
            continue
        seen.add(link)
        yield title, link, "", datetime.now(timezone.utc).isoformat(), "Intel"


def parse_specs(text: str, kind: str):
    specs = {}
    patterns = {
        "Núcleos": r"(?:up to\s+)?(\d+)\s*(?:cpu\s*)?cores?",
        "Hilos": r"(\d+)\s*threads?",
        "Frecuencia base": r"base(?:\s+clock)?(?:\s+frequency)?[^\d]{0,30}(\d+(?:\.\d+)?)\s*GHz",
        "Frecuencia máxima": r"(?:boost|max(?:imum)?(?:\s+turbo)?(?:\s+clock)?(?:\s+frequency)?)[^\d]{0,30}(\d+(?:\.\d+)?)\s*GHz",
        "Caché": r"(\d+(?:\.\d+)?)\s*MB\s+(?:of\s+)?(?:total\s+)?(?:L[23]\s+)?cache",
        "TDP": r"(?:TDP|thermal design power)[^\d]{0,20}(\d+)\s*W",
        "Socket": r"\b(AM[245]|LGA\s?\d{4,5})\b",
        "Memoria": r"\b(DDR[45](?:\s*/\s*DDR[45])?)\b",
        "PCIe": r"\b(PCIe\s*[345](?:\.0)?)\b",
    }
    for label, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            value = match.group(1)
            specs[label] = f"{value} GHz" if label in ("Frecuencia base", "Frecuencia máxima") else f"{value} W" if label == "TDP" else value + (" MB" if label == "Caché" else "")
    if kind == "gpu":
        for label, pattern in {
            "Memoria": r"(\d{1,2})\s*GB\s*(GDDR[56X7]+|HBM\w*)",
            "Interfaz de memoria": r"(\d{3})\s*-?bit\s+(?:memory\s+)?(?:bus|interface)",
            "Ancho de banda": r"(\d+(?:\.\d+)?)\s*GB/s\s+(?:memory\s+)?bandwidth",
            "Consumo máximo": r"(?:TBP|TGP|total board power|graphics card power)[^\d]{0,24}(\d{2,3})\s*W",
        }.items():
            match = re.search(pattern, text, re.I)
            if match:
                specs[label] = f"{match.group(1)} {match.group(2)}" if label == "Memoria" else f"{match.group(1)}-bit" if label == "Interfaz de memoria" else f"{match.group(1)} W"
    return specs


def parse_release_date(text: str):
    match = re.search(r"(?:available|launch(?:es|ing)?|release(?:s|d)?)\s+(?:starting\s+)?(?:on\s+)?([A-Z][a-z]+\s+\d{1,2},?\s+20\d{2})", text, re.I)
    if not match:
        return None
    for pattern in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(match.group(1).replace(",", ""), pattern.replace(",", "")).date().isoformat()
        except ValueError:
            pass
    return None


def generation_for(brand: str, kind: str, name: str):
    if brand == "AMD" and kind == "cpu":
        match = re.search(r"\bRyzen\s+[3579]\s+(\d)", name)
        return f"Ryzen {match.group(1)}000" if match else "Ryzen"
    if brand == "AMD" and kind == "gpu":
        match = re.search(r"\bRX\s+(\d)", name)
        return f"Radeon RX {match.group(1)}000" if match else "Radeon RX"
    if brand == "Nvidia":
        match = re.search(r"\b(?:RTX|GTX)\s+(\d{2})", name)
        return f"GeForce {'RTX' if 'RTX' in name else 'GTX'} {match.group(1)}" if match else "GeForce"
    if brand == "Intel" and kind == "gpu":
        return "Arc Battlemage B-series" if re.search(r"\bB\d", name) else "Arc Alchemist A-series"
    if brand == "Intel" and "Core Ultra" in name:
        return "Core Ultra 200S Plus" if "Plus" in name else "Core Ultra 200S"
    if brand == "Intel":
        match = re.search(r"Core\s+i[3579]-(\d{2})", name)
        return f"{match.group(1)}.ª generación" if match else "Intel Core"
    return "Nueva generación"


def published_date(value: str):
    if not value:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return datetime.now(timezone.utc).date().isoformat()


def collect_candidates():
    candidates = []
    successes = 0
    for source in ("AMD", "NVIDIA"):
        feed_key = "amd" if source == "AMD" else "nvidia"
        try:
            candidates.extend(rss_entries(source, SOURCES[feed_key]))
            successes += 1
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as error:
            print(f"No se pudo consultar el feed oficial de {source}: {error}")
    try:
        candidates.extend(intel_entries())
        successes += 1
    except (urllib.error.URLError, TimeoutError, UnicodeError) as error:
        print(f"No se pudo consultar el newsroom oficial de Intel: {error}")
    if successes == 0:
        raise RuntimeError("No se pudo consultar ninguna fuente oficial del catálogo.")
    return candidates


def main():
    known = existing_products()
    old = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {"products": []}
    products = {normalize(item["name"]): item for item in old.get("products", [])}
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=45)

    for title, link, summary, published, source in collect_candidates():
        if not title or not link:
            continue
        description = f"{title}. {summary}"
        if not any(token in description.lower() for token in ("desktop", "gaming", "geforce", "radeon", "ryzen", "core ultra", "core i", "arc")):
            continue
        if not re.search(r"\b(announc\w*|unveil\w*|launch\w*|introduc\w*|debut\w*|available now|available today|released)\b", description, re.I):
            continue
        try:
            article = clean_html(fetch(link).decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, UnicodeError):
            article = summary
        full_text = re.sub(r"[™®]", "", f"{title}. {summary}. {article}")
        date = published_date(published)
        if datetime.fromisoformat(date).date() < cutoff:
            continue
        for brand, kind, pattern in MODEL_PATTERNS:
            for match in pattern.finditer(full_text):
                name = " ".join(match.group(0).split())
                key = normalize(name)
                if brand == "AMD" and kind == "cpu" and re.search(r"(?:HX|HS|U|H)$", name, re.I):
                    continue
                if brand == "Intel" and kind == "cpu" and re.search(r"(?:H|U|V|HX|HL)$", name, re.I):
                    continue
                if brand == "Nvidia" and kind == "gpu" and re.search(r"laptop|notebook", description, re.I) and not re.search(r"desktop", description, re.I):
                    continue
                # Known catalog models are refreshed only when an official news item
                # supplies a status/spec update; ordinary old entries remain untouched.
                if key in known and key not in products:
                    continue
                release_date = parse_release_date(full_text)
                says_released = bool(re.search(r"\b(now available|available today|available now|launched today|on sale now)\b", full_text, re.I))
                status = "released" if says_released or (release_date and release_date <= datetime.now(timezone.utc).date().isoformat()) else "announced"
                specs = parse_specs(full_text, kind)
                if brand == "AMD":
                    architecture = re.search(r"\b(Zen\s+\d|RDNA\s+\d)\b", full_text, re.I)
                    if architecture:
                        specs["Arquitectura"] = architecture.group(1)
                elif brand == "Nvidia":
                    architecture = re.search(r"\b(Blackwell|Ada Lovelace|Ampere|Turing|Hopper)\b", full_text, re.I)
                    if architecture:
                        specs["Arquitectura"] = architecture.group(1)
                previous = products.get(key, {})
                if says_released and not release_date:
                    release_date = date
                products[key] = {
                    **previous,
                    "kind": kind,
                    "brand": brand,
                    "name": name,
                    "date": release_date or previous.get("date"),
                    "releaseDate": release_date or previous.get("releaseDate"),
                    "announcementDate": date,
                    "generation": previous.get("generation") or generation_for(brand, kind, name),
                    "status": "coming-soon" if status == "announced" else "released",
                    "specs": {**previous.get("specs", {}), **specs},
                    "sourceUrl": link,
                    "sourceName": source,
                }

    result = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "products": sorted(products.values(), key=lambda item: (item.get("brand", ""), item.get("name", ""))),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
