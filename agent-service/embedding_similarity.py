"""Routing similarity from FastRP embeddings (runtime side; requires numpy).

An account is compared with the training-period laundering accounts. Its own
entry is never its nearest neighbour; being a past reference is reported as a
separate flag. Alerts inside the training period, and accounts never seen in
training (PPT: 신규 계좌), get no similarity instead of a leaked or guessed one.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from similarity import summarize

METHOD = "fastrp_cosine_v1"


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class EmbeddingSimilarity:
    def __init__(self, directory):
        directory = Path(directory)
        manifest_text = (directory / "manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        for name, expected in manifest["files"].items():
            if _sha256(directory / name) != expected:
                raise ValueError(f"Embedding file {name} does not match its manifest")
        self.vectors = np.load(directory / "embeddings.npy")
        keys = json.loads((directory / "accounts.json").read_text(encoding="utf-8"))
        self.index = {key: row for row, key in enumerate(keys)}
        self.reference_keys = json.loads((directory / "references.json").read_text(encoding="utf-8"))["accountKeys"]
        self.reference_rows = np.array([self.index[key] for key in self.reference_keys], dtype=np.int64)
        self.reference_matrix = self.vectors[self.reference_rows]
        self.train_end = manifest["trainEnd"]
        self.base = {"method": METHOD, "embeddingVersion": manifest["version"], "trainEnd": self.train_end,
                     "embeddingManifestSha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()}

    def account_score(self, account_key):
        row = self.index.get(account_key)
        if row is None:
            return {"status": "skipped", "reason": "account_not_in_training_graph"}
        own = self.reference_rows == row
        scores = np.where(own, -np.inf, self.reference_matrix @ self.vectors[row])
        best = int(np.argmax(scores))
        if not np.isfinite(scores[best]):
            return {"status": "skipped", "reason": "no_other_reference_account"}
        return {"status": "computed", "score": round(float(scores[best]), 4),
                "nearestReferenceId": self.reference_keys[best], "accountInReferenceSet": bool(own.any())}

    def score(self, profiles, context):
        if context["scope"]["asOf"] < self.train_end:
            return {**self.base, "status": "unavailable", "reason": "alert_inside_embedding_training_period"}
        accounts = [{"alias": profile["alias"], **self.account_score(profile["accountKey"])} for profile in profiles]
        return summarize(accounts, self.base)
