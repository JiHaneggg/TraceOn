"""Capture official HTML snapshots and build a versioned, citation-ready corpus.

No LLM rewriting. Locations refer to normalized visible HTML text, not PDF pages.
Snapshots/indexes are local artifacts. A failed refresh leaves the old index intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "data/rag/index.json"
SOURCES = Path(__file__).with_name("corpus_sources.json")
ALLOWED_HOSTS = {"kofiu.go.kr", "www.kofiu.go.kr", "law.go.kr", "www.law.go.kr",
                 "www.fincen.gov", "fintrac-canafe.canada.ca"}


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_json(path, payload):
    """Readers see one complete JSON file, including on Windows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def archive_index(payload, directory):
    """Preserve the complete citation index under its verified content hash."""
    checksum = payload.get("corpusSha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum) or checksum != digest(canonical({k: v for k, v in payload.items() if k != "corpusSha256"})):
        raise ValueError("Cannot archive an invalid corpus")
    path = Path(directory) / (checksum + ".json")
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise ValueError("Existing corpus archive is inconsistent")
    else:
        atomic_json(path, payload)
    return path


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def visible_text(html):
    parser = VisibleText()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def chunks_for(source, raw, captured_at, max_chars=800, overlap=100):
    if not 100 <= max_chars <= 2000 or not 0 <= overlap < max_chars:
        raise ValueError("Invalid chunk limits")
    text = visible_text(raw.decode("utf-8-sig"))
    if any(marker in text for marker in ("시스템 점검", "Access Denied", "403 Forbidden")):
        raise ValueError("Source is a maintenance/access error page, not a document")
    for required in source.get("requiredText", []):
        if required not in text:
            raise ValueError("Source identity/version marker missing: " + required)
    result = []
    for section in source["sections"]:
        occurrence = section.get("occurrence", "unique")
        if occurrence not in {"unique", "first", "last"}:
            raise ValueError("Unknown section occurrence selector")
        start = text.rfind(section["heading"]) if occurrence == "last" else text.find(section["heading"])
        end = text.find(section["end"], start + len(section["heading"])) if start >= 0 else -1
        if start < 0 or end <= start or (occurrence == "unique" and text.count(section["heading"]) != 1):
            raise ValueError(f"Missing/ambiguous source section: {source['documentId']} / {section['heading']}")
        for offset in range(start, end, max_chars - overlap):
            stop = min(offset + max_chars, end)
            content = text[offset:stop]
            chunk = {key: source[key] for key in ["documentId", "title", "url", "publisher",
                                                  "jurisdiction", "sourceKind", "effectiveDate"]}
            for key in ("publishedDate", "modifiedDate", "version", "applicability", "validUntil"):
                if key in source:
                    chunk[key] = source[key]
            chunk.update({"sourceSha256": digest(raw), "capturedAt": captured_at,
                          "section": section["heading"], "text": content,
                          "textSha256": digest(content),
                          "locator": {"type": "normalized_html_text", "start": offset, "end": stop},
                          "documentTextSha256": digest(text)})
            chunk["chunkId"] = source["documentId"] + ":" + digest(canonical(chunk))[:20]
            result.append(chunk)
            if stop == end:
                break
    return result


def build(raw_dir, output=DEFAULT_INDEX, refresh=False, sources=None):
    sources = sources or json.loads(SOURCES.read_text(encoding="utf-8"))
    raw_dir, output = Path(raw_dir), Path(output)
    if (output.parent / "registry.json").exists():
        raise ValueError("Managed corpus: use corpus_manager.py register/check/publish instead of rebuilding")
    raw_dir.mkdir(parents=True, exist_ok=True)
    chunks, documents = [], []
    for source in sources:
        url = urlparse(source["url"])
        if url.scheme != "https" or url.hostname not in ALLOWED_HOSTS:
            raise ValueError("Only curated HTTPS source hosts are allowed")
        path = raw_dir / (source["documentId"] + ".html")
        if refresh or not path.exists():
            with urlopen(Request(source["url"], headers={"User-Agent": "TraceOn-RAG-MVP/1.0"}), timeout=25) as response:
                if urlparse(response.url).hostname not in ALLOWED_HOSTS:
                    raise ValueError("Unexpected source redirect")
                if "/sorry/" in urlparse(response.url).path:
                    raise ValueError("Official source is under maintenance; excluded from the corpus")
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("Source too large")
            # Validate before replacing a previous good snapshot.
            chunks_for(source, raw, "validation")
            path.write_bytes(raw)
        raw = path.read_bytes()
        archive = raw_dir / "snapshots" / source["documentId"] / (digest(raw) + ".html")
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            archive.write_bytes(raw)
        captured = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        rows = chunks_for(source, raw, captured)
        chunks.extend(rows)
        documents.append({"documentId": source["documentId"], "url": source["url"],
                          "jurisdiction": source["jurisdiction"], "sourceKind": source["sourceKind"],
                          "effectiveDate": source["effectiveDate"],
                          "sourceSha256": digest(raw), "capturedAt": captured,
                          "snapshotFile": str(archive.relative_to(raw_dir)).replace("\\", "/"),
                          "chunkCount": len(rows)})
    payload = {"schemaVersion": "1.0", "evidencePresentationVersion": "1.0", "documents": documents, "chunks": chunks,
               "method": "verbatim_normalized_visible_html_sections"}
    payload["corpusSha256"] = digest(canonical(payload))
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        archive_index(json.loads(output.read_text(encoding="utf-8")), output.parent / "versions")
    archive_index(payload, output.parent / "versions")
    atomic_json(output, payload)
    return {"documents": len(documents), "chunks": len(chunks), "corpusSha256": payload["corpusSha256"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/rag/raw")
    parser.add_argument("--output", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.raw_dir, args.output, args.refresh), ensure_ascii=False))
