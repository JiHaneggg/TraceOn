"""Version-pinned, read-only document and transaction evidence resolution.

No live source fetch, arbitrary file paths, database credentials or Cypher.
HTML is returned only as normalized text, with offsets for client highlighting.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
import re
import threading
from urllib.parse import quote

from corpus import digest, visible_text
from retrieval import Index, section_key
from source_presentation import source_display


class EvidenceError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def citation_path(chunk_id, corpus_hash):
    return "/v1/rag/citations/" + quote(chunk_id, safe="") + "?corpusSha256=" + corpus_hash


def fact_request(context, fact_id):
    return {"method": "POST", "path": "/v1/rag/facts/resolve", "factId": fact_id,
            "contextId": context["contextId"], "corpusSha256": context["corpusSha256"]}


class EvidenceRepository:
    def __init__(self, index, raw_root=None, archive_dir=None):
        self.active = index
        self.raw_root = Path(raw_root) if raw_root is not None else index.path.parent / "raw"
        self.archive_dir = Path(archive_dir) if archive_dir is not None else index.path.parent / "versions"
        self.cache, self.lock = OrderedDict(), threading.RLock()
        self.active_stamp = self._stamp()

    def _stamp(self):
        stat = self.active.path.stat()
        return stat.st_mtime_ns, stat.st_size

    def reload_active(self):
        """Each request keeps one Index instance; later requests see publication."""
        with self.lock:
            try:
                stamp = self._stamp()
                if stamp != self.active_stamp:
                    current = Index(self.active.path, selection_policy=self.active.selection_policy)
                    self.active = current
                    self.active_stamp = stamp
            except (OSError, ValueError, KeyError, TypeError):
                raise EvidenceError(409, "active_corpus_unavailable", "Published corpus is missing or failed integrity checks") from None
            return self.active

    def index_for(self, corpus_hash=None):
        active = self.reload_active()
        if corpus_hash is None:
            return active
        if not isinstance(corpus_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", corpus_hash):
            raise EvidenceError(400, "invalid_corpus_hash", "corpusSha256 must be a lowercase SHA256")
        if corpus_hash == active.corpus_hash:
            return active
        with self.lock:
            if corpus_hash in self.cache:
                self.cache.move_to_end(corpus_hash)
                return self.cache[corpus_hash]
            if self.archive_dir.exists():
                root = self.archive_dir.resolve()
                exact = self.archive_dir / (corpus_hash + ".json")
                # Content-addressed archives have no 64-version search ceiling.
                # Keep a bounded fallback for legacy v1-<hash>.json filenames.
                candidates = [exact] if exact.exists() else sorted(self.archive_dir.glob("*" + corpus_hash + "*.json"))[:64]
                for path in candidates:
                    if corpus_hash not in path.name:
                        continue
                    if not path.resolve().is_relative_to(root) or path.stat().st_size > 4_000_000:
                        raise EvidenceError(409, "archive_unavailable", "Archived corpus cannot be safely loaded")
                    try:
                        archived = Index(path)
                    except (ValueError, KeyError, OSError):
                        raise EvidenceError(409, "archive_integrity_mismatch", "Archived corpus failed integrity checks") from None
                    if archived.corpus_hash == corpus_hash:
                        self.cache[corpus_hash] = archived
                        while len(self.cache) > 8:
                            self.cache.popitem(last=False)
                        return archived
        raise EvidenceError(409, "corpus_unavailable", "Requested corpus version is unavailable; active data was not substituted")

    def citation(self, chunk_id, corpus_hash=None):
        if not isinstance(chunk_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", chunk_id):
            raise EvidenceError(400, "invalid_chunk_id", "Invalid chunkId")
        index = self.index_for(corpus_hash)
        row = index.by_id.get(chunk_id)
        if row is None:
            raise EvidenceError(404, "citation_not_found", "chunkId is not present in the requested corpus")
        members = sorted((item for item in index.chunks if section_key(item) == section_key(row)),
                         key=lambda item: item["locator"]["start"])
        start, end = min(item["locator"]["start"] for item in members), max(item["locator"]["end"] for item in members)
        result = {"schemaVersion": "1.0", "corpusSha256": index.corpus_hash, "citation": deepcopy(row),
                  "section": {"title": row["section"], "text": None,
                              "chunkIds": [item["chunkId"] for item in members]},
                  "sourceVerification": {"status": "index_only", "sourceSha256": row["sourceSha256"],
                                         "liveSourceFetched": False, "reason": "Original snapshot is not available"},
                  "links": {"source": row["url"], "self": citation_path(chunk_id, index.corpus_hash)}}
        if index.presentation_version:
            result["citation"]["sourceDisplay"] = source_display(row)
            result["evidencePresentationVersion"] = index.presentation_version
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", row["documentId"]) or not re.fullmatch(r"[0-9a-f]{64}", row["sourceSha256"]):
            raise EvidenceError(409, "invalid_snapshot_identity", "Invalid source snapshot identity")
        root = self.raw_root.resolve()
        path = root / "snapshots" / row["documentId"] / (row["sourceSha256"] + ".html")
        if not path.resolve().is_relative_to(root):
            raise EvidenceError(409, "snapshot_path_conflict", "Snapshot does not belong to the configured source directory")
        if not path.exists():
            return result
        if path.stat().st_size > 2_000_000:
            raise EvidenceError(409, "snapshot_too_large", "Source snapshot exceeds the read limit")
        raw = path.read_bytes()
        if digest(raw) != row["sourceSha256"]:
            raise EvidenceError(409, "snapshot_hash_mismatch", "Original snapshot hash does not match the recorded source")
        text = visible_text(raw.decode("utf-8-sig"))
        if digest(text) != row["documentTextSha256"] or end > len(text) or end - start > 20_000:
            raise EvidenceError(409, "snapshot_text_mismatch", "Normalized source text does not match the indexed document")
        for item in members:
            loc = item["locator"]
            if (text[loc["start"]:loc["end"]] != item["text"] or digest(item["text"]) != item["textSha256"]):
                raise EvidenceError(409, "citation_location_mismatch", "Citation text/offset does not match the original snapshot")
        loc = row["locator"]
        result["section"].update({"text": text[start:end], "documentStart": start, "documentEnd": end,
                                  "highlight": {"start": loc["start"] - start, "end": loc["end"] - start}})
        result["sourceVerification"].update({"status": "snapshot_checked", "reason": None})
        return result

    def index_for_generation(self, corpus_hash=None):
        selected = self.index_for(corpus_hash)
        active = self.index_for()
        if (active.query_scope_policy_version is not None
                and selected.query_scope_policy_version != active.query_scope_policy_version):
            raise EvidenceError(409,"legacy_corpus_read_only",
                                "Legacy corpus remains available for citation/fact lookup and stored report verification; new generation requires the active query-scope policy")
        return selected


def resolve_fact(context, fact_id, expected_context_id=None):
    if not isinstance(fact_id, str) or not 1 <= len(fact_id) <= 250:
        raise EvidenceError(400, "invalid_fact_id", "Invalid factId")
    if expected_context_id is not None and expected_context_id != context["contextId"]:
        raise EvidenceError(409, "context_mismatch", "Graph/query/rule or pipeline version differs from the stored context")
    fact = next((item for item in context["reportFacts"] if item["factId"] == fact_id), None)
    if fact is None:
        raise EvidenceError(404, "fact_not_found", "factId is not present in the verified context")
    ids = set(fact["evidenceTxIds"])
    return {"schemaVersion": "1.0", "contextId": context["contextId"], "graphSha256": context["graphSha256"],
            "corpusSha256": context["corpusSha256"], "fact": dict(fact),
            "transactions": [row for row in context["evidenceTransactions"] if row["txId"] in ids],
            "scope": context["scope"], "verification": {"recomputed": True,
                "basis": "backend_supplied_graph_and_deterministic_rules", "externalLedgerChecked": False}}
