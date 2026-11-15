"""Local administrator workflow: register -> inspect changes -> publish.

Registration never changes retrieval. Publication archives both corpus versions
before atomically switching the active index. Sources are data, never instructions.
Only curated HTTPS HTML sources are supported; no public write API is exposed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import difflib
import json
from pathlib import Path
import re
import sys
import textwrap
from urllib.parse import urlparse
from urllib.request import build_opener, HTTPRedirectHandler, Request

from corpus import (ALLOWED_HOSTS, DEFAULT_INDEX, SOURCES, archive_index,
                    atomic_json, canonical, chunks_for, digest, visible_text)
from retrieval import Index
from evidence_fit import validate_policy

MAX_RAW = 2_000_000
SOURCE_FIELDS = {"documentId", "title", "url", "publisher", "jurisdiction", "sourceKind",
                 "effectiveDate", "publishedDate", "modifiedDate", "version", "applicability",
                 "validUntil", "requiredText", "sections"}


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_url(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Invalid source URL")
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.fragment):
        raise ValueError("Only curated HTTPS source URLs are supported")


def validate_source(source):
    if not isinstance(source, dict) or set(source) - SOURCE_FIELDS:
        raise ValueError("Unknown source metadata field")
    required = {"documentId", "title", "url", "publisher", "jurisdiction", "sourceKind", "effectiveDate", "sections"}
    if required - set(source):
        raise ValueError("Missing source metadata: " + ", ".join(sorted(required - set(source))))
    if not isinstance(source["documentId"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", source["documentId"]):
        raise ValueError("documentId must be a lowercase slug")
    for key in ("title", "publisher"):
        if not isinstance(source[key], str) or not 1 <= len(source[key].strip()) <= 500:
            raise ValueError("Invalid " + key)
    validate_url(source["url"])
    if source["jurisdiction"] not in {"KR", "US", "CA"} or source["sourceKind"] not in {"law", "official_guidance", "foreign_typology", "bank_internal_policy"}:
        raise ValueError("Unsupported jurisdiction/sourceKind")
    for key in ("effectiveDate", "publishedDate", "modifiedDate", "validUntil"):
        value = source.get(key)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(key + " must be YYYY-MM-DD or null")
            date.fromisoformat(value)
    if source.get("validUntil") and (not source.get("effectiveDate") or source["validUntil"] <= source["effectiveDate"]):
        raise ValueError("validUntil is an exclusive bound after effectiveDate")
    for key in ("version", "applicability"):
        if key in source and (not isinstance(source[key], str) or not 1 <= len(source[key]) <= 2000):
            raise ValueError("Invalid " + key)
    markers = source.get("requiredText", [])
    if not isinstance(markers, list) or len(markers) > 20 or any(not isinstance(x, str) or not 1 <= len(x) <= 500 for x in markers):
        raise ValueError("Invalid requiredText")
    sections = source["sections"]
    if not isinstance(sections, list) or not 1 <= len(sections) <= 100:
        raise ValueError("Select 1..100 source sections")
    headings = []
    for section in sections:
        if not isinstance(section, dict) or set(section) - {"heading", "end", "occurrence"}:
            raise ValueError("Invalid section selector")
        for key in ("heading", "end"):
            if not isinstance(section.get(key), str) or not 1 <= len(section[key]) <= 500:
                raise ValueError("Missing/invalid section " + key)
        if section.get("occurrence", "unique") not in {"unique", "first", "last"}:
            raise ValueError("Invalid section occurrence")
        headings.append(section["heading"])
    if len(set(headings)) != len(headings):
        raise ValueError("Duplicate selected section")
    return deepcopy(source)


class CuratedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)  # Check before following a redirect, not afterwards.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_html(url):
    validate_url(url)
    request = Request(url, headers={"User-Agent": "TraceOn-RAG-MVP/1.0"})
    with build_opener(CuratedRedirect()).open(request, timeout=25) as response:
        validate_url(response.url)
        if "/sorry/" in urlparse(response.url).path or response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("Source is not an available HTML document")
        raw = response.read(MAX_RAW + 1)
    if len(raw) > MAX_RAW:
        raise ValueError("Source exceeds 2 MB")
    return raw


class CorpusManager:
    def __init__(self, index_path=DEFAULT_INDEX):
        self.index_path = Path(index_path).resolve()
        self.root = self.index_path.parent
        self.registry_path = self.root / "registry.json"
        self.raw_root = self.root / "raw"
        self.archive_dir = self.root / "versions"

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ".corpus-manager.lock"
        try:
            stream = path.open("x", encoding="utf-8")
        except FileExistsError:
            raise ValueError("Another document management operation holds the lock; inspect it before retrying") from None
        try:
            with stream:
                stream.write(now())
            yield
        finally:
            path.unlink()

    def load(self):
        if not self.registry_path.exists():
            raise ValueError("Run corpus_manager.py init first")
        if self.registry_path.stat().st_size > 8_000_000:
            raise ValueError("Registry exceeds 8 MB")
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        checksum = registry.pop("registrySha256")
        if checksum != digest(canonical(registry)) or registry.get("schemaVersion") != "1.0":
            raise ValueError("Registry checksum/version mismatch")
        for document_id, document in registry["documents"].items():
            for row in document["revisions"]:
                source = validate_source(row["source"])
                if (source["documentId"] != document_id or not re.fullmatch(r"[0-9a-f]{64}", row["sourceSha256"])
                        or row["revisionId"] != self.revision_id(source, row["sourceSha256"])):
                    raise ValueError("Registry revision identity mismatch")
        return registry

    def save(self, registry):
        payload = deepcopy(registry)
        payload["registrySha256"] = digest(canonical(payload))
        if len(canonical(payload).encode("utf-8")) > 8_000_000:
            raise ValueError("Registry exceeds 8 MB")
        atomic_json(self.registry_path, payload)

    def active_payload(self):
        if not self.index_path.exists():
            return None
        Index(self.index_path)
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    @staticmethod
    def revision_id(source, source_hash):
        return digest(canonical({"source": source, "sourceSha256": source_hash,
                                 "chunking": {"maxChars": 800, "overlap": 100}}))

    def snapshot(self, document_id, source_hash):
        path = self.raw_root / "snapshots" / document_id / (source_hash + ".html")
        if not path.resolve().is_relative_to(self.raw_root.resolve()):
            raise ValueError("Snapshot directory is outside configured storage")
        return path

    def rows(self, row):
        source = validate_source(row["source"])
        path = self.snapshot(source["documentId"], row["sourceSha256"])
        if not path.exists() or path.stat().st_size > MAX_RAW:
            raise ValueError("Original snapshot is missing or exceeds 2 MB")
        raw = path.read_bytes()
        if digest(raw) != row["sourceSha256"]:
            raise ValueError("Original snapshot checksum mismatch")
        result = chunks_for(source, raw, row["capturedAt"])
        if [item["chunkId"] for item in result] != row["chunkIds"]:
            raise ValueError("Revision extraction differs from its registered chunks")
        return result

    @staticmethod
    def active_revision(document, payload):
        if payload is None:
            return None
        ids = [r["chunkId"] for r in payload["chunks"] if r["documentId"] == document["revisions"][0]["source"]["documentId"]]
        return next((r for r in document["revisions"] if r["chunkIds"] == ids), None)

    def make_revision(self, source, raw, captured_at):
        if not raw or len(raw) > MAX_RAW:
            raise ValueError("HTML must contain 1..2000000 bytes")
        if not re.search(r"<(?:!doctype\s+html|html|body|h[1-6]|p|div|section|article)\b", raw.decode("utf-8-sig"), re.I):
            raise ValueError("Only UTF-8 HTML snapshots are supported")
        rows = chunks_for(source, raw, captured_at)
        if not rows:
            raise ValueError("No selected document text")
        return {"revisionId": self.revision_id(source, digest(raw)), "source": source,
                "sourceSha256": digest(raw), "documentTextSha256": digest(visible_text(raw.decode("utf-8-sig"))),
                "capturedAt": captured_at, "registeredAt": now(),
                "chunkIds": [row["chunkId"] for row in rows]}

    def init(self, sources=None):
        with self.locked():
            if self.registry_path.exists():
                return self.status()
            payload = self.active_payload()
            registry = {"schemaVersion": "1.0", "createdAt": now(), "documents": {}}
            definitions = {s["documentId"]: validate_source(s) for s in (sources if sources is not None else json.loads(SOURCES.read_text(encoding="utf-8")))}
            if payload:
                for document in payload["documents"]:
                    source = definitions.get(document["documentId"])
                    if source is None:
                        raise ValueError("Source metadata unavailable for " + document["documentId"])
                    path = self.snapshot(source["documentId"], document["sourceSha256"])
                    if path.stat().st_size > MAX_RAW:
                        raise ValueError("Snapshot exceeds 2 MB")
                    row = self.make_revision(source, path.read_bytes(), document["capturedAt"])
                    expected = [r for r in payload["chunks"] if r["documentId"] == source["documentId"]]
                    if self.rows(row) != expected:
                        raise ValueError("Current source metadata does not reproduce the active document")
                    registry["documents"][source["documentId"]] = {"revisions": [row]}
                archive_index(payload, self.archive_dir)
            self.save(registry)
            return self.status()

    def register(self, source, raw, *, captured_at=None):
        source = validate_source(source)
        with self.locked():
            registry, payload = self.load(), self.active_payload()
            document = registry["documents"].get(source["documentId"], {"revisions": []})
            key = self.revision_id(source, digest(raw))
            existing = next((r for r in document["revisions"] if r["revisionId"] == key), None)
            if existing:
                self.rows(existing)  # A known revision still needs its intact snapshot.
                row = existing
            else:
                row = self.make_revision(source, raw, captured_at or now())
                path = self.snapshot(source["documentId"], row["sourceSha256"])
                if path.exists() and path.read_bytes() != raw:
                    raise ValueError("Existing source archive is inconsistent")
                path.parent.mkdir(parents=True, exist_ok=True)
                # Content-addressed original is written before the registry references it.
                if not path.exists():
                    import os
                    with path.open("xb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                document["revisions"].append(row)
                registry["documents"][source["documentId"]] = document
                self.save(registry)
            active = self.active_revision(document, payload)
            return {"status": "no_change" if active and active["revisionId"] == key else "candidate",
                    "newRevision": existing is None, "documentId": source["documentId"], "revisionId": key,
                    "activeCorpusSha256": payload["corpusSha256"] if payload else None,
                    "change": self.change(active, row), "searchUpdated": False}

    @staticmethod
    def change(old, new):
        return {"sourceBytesChanged": old is None or old["sourceSha256"] != new["sourceSha256"],
                "normalizedDocumentChanged": old is None or old["documentTextSha256"] != new["documentTextSha256"],
                "metadataOrSelectorsChanged": old is None or old["source"] != new["source"]}

    def check(self, document_id, raw=None, source=None):
        registry = self.load()
        document = registry["documents"].get(document_id)
        if not document:
            raise ValueError("Unknown documentId; use register for a new document")
        if source is None:
            source = document["revisions"][-1]["source"]
        source = validate_source(source)
        if source["documentId"] != document_id:
            raise ValueError("Source documentId differs from the checked document")
        return self.register(source, fetch_html(source["url"]) if raw is None else raw)

    def diff(self, document_id, revision_id):
        registry, payload = self.load(), self.active_payload()
        document = registry["documents"].get(document_id)
        row = next((r for r in document["revisions"] if r["revisionId"] == revision_id), None) if document else None
        if row is None:
            raise ValueError("Unknown document revision")
        active = self.active_revision(document, payload)

        def sections(revision):
            if revision is None:
                return {}, False
            rows = self.rows(revision)
            raw = self.snapshot(document_id, revision["sourceSha256"]).read_bytes()
            text = visible_text(raw.decode("utf-8-sig"))
            groups = {}
            for chunk in rows:
                groups.setdefault(chunk["section"], []).append(chunk)
            result, truncated = {}, False
            for heading, members in groups.items():
                start = min(c["locator"]["start"] for c in members)
                end = max(c["locator"]["end"] for c in members)
                # Bound preview work; exact source/chunk hashes still identify the full revision.
                truncated |= end - start > 20_000
                result[heading] = text[start:min(end, start + 20_000)]
            return result, truncated

        before, left_truncated = sections(active)
        after, right_truncated = sections(row)
        changes, budget, truncated = [], 12_000, left_truncated or right_truncated
        for heading in dict.fromkeys([*before, *after]):
            old, new = before.get(heading, ""), after.get(heading, "")
            if old == new:
                continue
            if budget <= 0:
                truncated = True
                break
            lines = difflib.unified_diff(textwrap.wrap(old, width=100), textwrap.wrap(new, width=100),
                                         fromfile="active", tofile="candidate", lineterm="", n=2)
            excerpt = "\n".join(lines)
            truncated |= len(excerpt) > budget
            changes.append({"section": heading, "diff": excerpt[:budget]})
            budget -= min(len(excerpt), budget)
        previous_source = active["source"] if active else {}
        metadata = {key: {"before": previous_source.get(key), "after": row["source"].get(key)}
                    for key in sorted(set(previous_source) | set(row["source"]))
                    if previous_source.get(key) != row["source"].get(key)}
        return {"documentId": document_id, "revisionId": revision_id,
                "activeRevisionId": active["revisionId"] if active else None,
                "activeCorpusSha256": payload["corpusSha256"] if payload else None,
                "change": self.change(active, row), "metadataChanges": metadata,
                "selectedSectionDiffs": changes, "previewTruncated": truncated,
                "notice": "Diff covers selected normalized text only. Full HTML changes are identified separately by source hash; dates/authority require source review."}

    def publish(self, document_id, revision_id, expected_corpus_hash):
        with self.locked():
            registry, payload = self.load(), self.active_payload()
            current_hash = payload["corpusSha256"] if payload else "none"
            if expected_corpus_hash != current_hash:
                raise ValueError("Active corpus changed; inspect status before publishing again")
            document = registry["documents"].get(document_id)
            row = next((r for r in document["revisions"] if r["revisionId"] == revision_id), None) if document else None
            if row is None:
                raise ValueError("Unknown document revision")
            rows = self.rows(row)
            active = self.active_revision(document, payload)
            if active and active["revisionId"] == revision_id:
                return {"status": "no_change", "corpusSha256": current_hash, "searchUpdated": False}
            source = row["source"]
            new_document = {k: v for k, v in source.items() if k not in {"sections", "requiredText"}}
            new_document.update({"revisionId": revision_id, "sourceSha256": row["sourceSha256"],
                                 "capturedAt": row["capturedAt"], "chunkCount": len(rows),
                                 "snapshotFile": "snapshots/" + document_id + "/" + row["sourceSha256"] + ".html"})
            result = deepcopy(payload) if payload else {"schemaVersion": "1.0", "documents": [], "chunks": [],
                                                        "method": "verbatim_normalized_visible_html_sections"}
            # Preserve other documents and their exact chunk IDs/capture times.
            result["documents"] = [d for d in result["documents"] if d["documentId"] != document_id] + [new_document]
            result["chunks"] = [r for r in result["chunks"] if r["documentId"] != document_id] + rows
            result.pop("corpusSha256", None)
            result["evidencePresentationVersion"] = "1.0"
            result["corpusSha256"] = digest(canonical(result))
            if len((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")) > 4_000_000:
                raise ValueError("Corpus exceeds the 4 MB lookup limit")
            if payload:
                archive_index(payload, self.archive_dir)
            archive_index(result, self.archive_dir)
            atomic_json(self.index_path, result)  # Only authoritative publication pointer.
            return {"status": "published", "documentId": document_id, "revisionId": revision_id,
                    "previousCorpusSha256": None if payload is None else current_hash,
                    "corpusSha256": result["corpusSha256"], "searchUpdated": True,
                    "notice": "Running RAG requests reload the active index; past citations stay pinned to their original corpus hash."}

    def apply_evidence_policy(self, policy, expected_corpus_hash):
        """Publish immutable assessment rules together with the citation index."""
        with self.locked():
            self.load()
            payload = self.active_payload()
            if payload is None or payload["corpusSha256"] != expected_corpus_hash:
                raise ValueError("Active corpus changed; inspect status before applying the policy")
            index = Index(self.index_path)
            validate_policy(policy, index, strict_bindings=True)
            if payload.get("evidenceFitPolicy") == policy:
                return {"status": "no_change", "corpusSha256": expected_corpus_hash, "policySha256": policy["policySha256"]}
            result = deepcopy(payload)
            result.pop("corpusSha256")
            result["evidencePresentationVersion"] = "1.0"
            result["evidenceFitPolicy"] = deepcopy(policy)
            result["corpusSha256"] = digest(canonical(result))
            if len((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")) > 4_000_000:
                raise ValueError("Corpus exceeds the 4 MB lookup limit")
            archive_index(payload, self.archive_dir)
            archive_index(result, self.archive_dir)
            atomic_json(self.index_path, result)
            return {"status": "published", "previousCorpusSha256": expected_corpus_hash,
                    "corpusSha256": result["corpusSha256"], "policySha256": policy["policySha256"]}

    def status(self, document_id=None):
        registry, payload = self.load(), self.active_payload()
        if document_id is not None and document_id not in registry["documents"]:
            raise ValueError("Unknown documentId")
        documents = []
        for key, document in registry["documents"].items():
            if document_id is not None and key != document_id:
                continue
            active = self.active_revision(document, payload)
            revisions = []
            for row in document["revisions"]:
                revisions.append({**{k: row[k] for k in ("revisionId", "sourceSha256", "capturedAt", "registeredAt")},
                                  "metadata": {k: v for k, v in row["source"].items() if k not in {"sections", "requiredText"}},
                                  "active": bool(active and active["revisionId"] == row["revisionId"]),
                                  "changeFromActive": self.change(active, row)})
            documents.append({"documentId": key, "activeRevisionId": active["revisionId"] if active else None,
                              "revisions": revisions})
        return {"schemaVersion": "1.0", "activeCorpusSha256": payload["corpusSha256"] if payload else None,
                "documentCount": len(documents), "documents": documents,
                "notice": "Registration is not publication. Dates are source metadata, not a verified legal applicability decision."}

    def set_query_scope_policy(self, version, expected_corpus_hash):
        if version not in {"1.0", "legacy"}:
            raise ValueError("Unsupported query scope policy version")
        with self.locked():
            self.load()
            payload = self.active_payload()
            if payload is None or payload["corpusSha256"] != expected_corpus_hash:
                raise ValueError("Active corpus changed; inspect status before applying the scope policy")
            expected = None if version == "legacy" else version
            if payload.get("queryScopePolicyVersion") == expected:
                return {"status":"no_change","corpusSha256":expected_corpus_hash,"queryScopePolicyVersion":expected}
            result = deepcopy(payload)
            result.pop("corpusSha256")
            if expected is None:
                result.pop("queryScopePolicyVersion",None)
            else:
                result["queryScopePolicyVersion"] = expected
            result["corpusSha256"] = digest(canonical(result))
            archive_index(payload,self.archive_dir)
            archive_index(result,self.archive_dir)
            atomic_json(self.index_path,result)
            return {"status":"published","previousCorpusSha256":expected_corpus_hash,
                    "corpusSha256":result["corpusSha256"],"queryScopePolicyVersion":expected}


def read_html(path):
    path = Path(path)
    if path.stat().st_size > MAX_RAW:
        raise ValueError("Source exceeds 2 MB")
    return path.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    history = commands.add_parser("history")
    history.add_argument("--document-id", required=True)
    compare = commands.add_parser("diff")
    compare.add_argument("--document-id", required=True)
    compare.add_argument("--revision-id", required=True)
    for name in ("register", "check"):
        sub = commands.add_parser(name)
        sub.add_argument("--source", type=Path, required=name == "register")
        if name == "check":
            sub.add_argument("--document-id", required=True)
        group = sub.add_mutually_exclusive_group(required=True)
        group.add_argument("--snapshot", type=Path)
        group.add_argument("--fetch", action="store_true")
    publish = commands.add_parser("publish")
    publish.add_argument("--document-id", required=True)
    publish.add_argument("--revision-id", required=True)
    publish.add_argument("--expected-corpus-sha256", required=True, help="Current status hash, or 'none' for the first document")
    policy = commands.add_parser("apply-evidence-policy")
    policy.add_argument("--policy", type=Path, required=True)
    policy.add_argument("--expected-corpus-sha256", required=True)
    scope = commands.add_parser("set-query-scope")
    scope.add_argument("--version", choices=["1.0","legacy"], required=True)
    scope.add_argument("--expected-corpus-sha256", required=True)
    args = parser.parse_args()
    try:
        manager = CorpusManager(args.index)
        if args.command == "init":
            result = manager.init()
        elif args.command in {"status", "history"}:
            result = manager.status(getattr(args, "document_id", None))
        elif args.command == "publish":
            result = manager.publish(args.document_id, args.revision_id, args.expected_corpus_sha256)
        elif args.command == "diff":
            result = manager.diff(args.document_id, args.revision_id)
        elif args.command == "apply-evidence-policy":
            result = manager.apply_evidence_policy(json.loads(args.policy.read_text(encoding="utf-8-sig")), args.expected_corpus_sha256)
        elif args.command == "set-query-scope":
            result = manager.set_query_scope_policy(args.version,args.expected_corpus_sha256)
        else:
            source = json.loads(args.source.read_text(encoding="utf-8-sig")) if args.source else None
            raw = read_html(args.snapshot) if args.snapshot else None
            if args.command == "register":
                source = validate_source(source)
                result = manager.register(source, raw if raw is not None else fetch_html(source["url"]))
            else:
                result = manager.check(args.document_id, raw, source)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"status": "error", "message": str(error), "searchUpdated": False}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
