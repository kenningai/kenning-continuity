# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Load the ontology model into a fresh TypeDB database and probe it both ways.

    uv run scripts/model_probe.py [--url http://localhost:8000] [--model PATH]

The unit gate (tests/unit/test_ontology_model.py) parses the model; it cannot
tell whether the TypeQL still commits, or whether a cardinality refuses what it
was written to refuse. This does both. Every probe asserts a verdict — ACCEPT or
REFUSE — and a refusal must also carry the expected error code, so a probe that
fails for an unrelated reason (a typo, a missing seed) does not pass as a
refusal.

A failed seed makes every match-based probe a false pass: the match finds
nothing, inserts nothing, and reports no error. So the seed is asserted to have
committed before any verdict is read, and each probe runs in a fresh database
so no probe sees another's writes.

Writes a UTC-stamped report to docs/research/ (git-ignored) and prints it.
Exits non-zero if any probe disagrees with its expected verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONTOLOGY_DIR = ROOT / "docs" / "design" / "ontology"

SEED = """
insert
  $d isa domain, has name "Telephony";
  $p isa person, has name "Seed Director", has source-key "seed@example.com";
  $s isa session, has session-id "s1", has session-status "active";
  (owner: $d, held: $s) isa has-session;
  (director: $p, act: $s) isa directed, has attribution-method "platform-stated";
  $a isa insight, has name "Routing", has description "v1";
  (discoverer: $s, found: $a) isa discovered;
  $b isa insight, has name "Routing", has description "v2";
  (discoverer: $s, found: $b) isa discovered;
  $c isa insight, has name "Routing", has description "v2-fork";
  (discoverer: $s, found: $c) isa discovered;
  $e isa insight, has name "Other", has description "unrelated";
  (discoverer: $s, found: $e) isa discovered;
"""

MATCH = """
match
  $s isa session, has session-id "s1";
  $a isa insight, has description "v1";
  $b isa insight, has description "v2";
  $c isa insight, has description "v2-fork";
  $e isa insight, has description "unrelated";
"""


@dataclass(frozen=True)
class Probe:
    label: str
    query: str
    expect: str            # "ACCEPT" or "REFUSE"
    code: str | None = None  # required error code on REFUSE


PROBES: list[Probe] = [
    # ── carried from v0.1.1 ────────────────────────────────────────────────
    Probe("a person directs a session",
          MATCH + 'insert $p2 isa person, has name "P2", has source-key "p2@example.com";'
          ' $s2 isa session, has session-id "s2", has session-status "active";'
          ' (director: $p2, act: $s2) isa directed, has attribution-method "platform-stated";'
          ' $d2 isa domain, has name "D2"; (owner: $d2, held: $s2) isa has-session;',
          "ACCEPT"),
    Probe("a person-referent directs a session",
          MATCH + 'insert $r isa person-referent, has name "R";'
          ' (discoverer: $s, found: $r) isa discovered;'
          ' (director: $r, act: $s) isa directed, has attribution-method "platform-stated";',
          "REFUSE", "INF11"),
    Probe("a referent-claim with resolution maybe",
          MATCH + 'insert $k isa referent-claim (link-source: $a, link-target: $e),'
          ' has resolution "maybe"; (discoverer: $s, found: $k) isa discovered;',
          "REFUSE", "CNT8"),
    Probe("a referent-claim with resolution distinct, discovered",
          MATCH + 'insert $k isa referent-claim (link-source: $a, link-target: $e),'
          ' has resolution "distinct"; (discoverer: $s, found: $k) isa discovered;',
          "ACCEPT"),
    Probe("a knowledge entity no session discovered",
          'insert $x isa insight, has name "Orphan";',
          "REFUSE", "CNT5"),
    # ── v0.2.0: evolved-from ───────────────────────────────────────────────
    Probe("an evolution carrying revision-why",
          MATCH + 'insert (successor: $b, predecessor: $a) isa evolved-from,'
          ' has revision-why "status changed from open to resolved";',
          "ACCEPT"),
    Probe("an evolution without revision-why (the pre-v0.2.0 past)",
          MATCH + 'insert (successor: $b, predecessor: $a) isa evolved-from;',
          "ACCEPT"),
    Probe("a second successor of one predecessor (a fork)",
          MATCH + 'insert (successor: $b, predecessor: $a) isa evolved-from;'
          ' (successor: $c, predecessor: $a) isa evolved-from;',
          "REFUSE", "CNT"),
    Probe("one successor evolving from two predecessors",
          MATCH + 'insert (successor: $b, predecessor: $a) isa evolved-from;'
          ' (successor: $b, predecessor: $e) isa evolved-from;',
          "REFUSE", "CNT"),
    Probe("an evolution with two revision-whys",
          MATCH + 'insert (successor: $b, predecessor: $a) isa evolved-from,'
          ' has revision-why "one", has revision-why "two";',
          "REFUSE", "CNT"),
    Probe("revision-why on a knowledge link",
          MATCH + 'insert $k isa informing (link-source: $a, link-target: $e),'
          ' has revision-why "no"; (discoverer: $s, found: $k) isa discovered;',
          "REFUSE", "INF"),
    Probe("a chain step of two (a → b → c), each with a reason",
          MATCH + 'insert (successor: $b, predecessor: $a) isa evolved-from, has revision-why "r1";'
          ' (successor: $c, predecessor: $b) isa evolved-from, has revision-why "r2";',
          "ACCEPT"),
]


class TypeDB:
    def __init__(self, url: str, user: str, password: str):
        self.url = url.rstrip("/")
        body = json.dumps({"username": user, "password": password}).encode()
        self.token = self._post("/v1/signin", body, auth=False)["token"]

    def _post(self, path: str, body: bytes | None, auth: bool = True) -> dict:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url + path, data=body or b"", headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw else {}

    def _delete(self, path: str) -> None:
        req = urllib.request.Request(self.url + path, method="DELETE",
                                     headers={"Authorization": "Bearer " + self.token})
        urllib.request.urlopen(req).read()

    def create(self, db: str) -> None:
        self._post(f"/v1/databases/{db}", None)

    def drop(self, db: str) -> None:
        self._delete(f"/v1/databases/{db}")

    def query(self, db: str, tx: str, q: str) -> tuple[bool, str]:
        body = json.dumps({"databaseName": db, "transactionType": tx,
                           "query": q, "commit": True}).encode()
        try:
            self._post("/v1/query", body)
            return True, ""
        except urllib.error.HTTPError as e:
            return False, e.read().decode()


def _latest_model() -> Path:
    return sorted(ONTOLOGY_DIR.glob("v*.tql"),
                  key=lambda p: tuple(int(n) for n in p.stem.lstrip("v").split(".")))[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("TYPEDB_URL", "http://localhost:8000"))
    ap.add_argument("--user", default=os.environ.get("TYPEDB_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("TYPEDB_PASSWORD", "password"))
    ap.add_argument("--model", type=Path, default=None)
    args = ap.parse_args()

    model = args.model or _latest_model()
    schema = model.read_text()
    db_api = TypeDB(args.url, args.user, args.password)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [f"# Model probe — {model.name} — {stamp}", ""]
    failures = 0

    for probe in PROBES:
        db = f"kc_probe_{uuid.uuid4().hex[:10]}"
        db_api.create(db)
        try:
            ok, err = db_api.query(db, "schema", schema)
            if not ok:
                lines.append(f"SCHEMA DID NOT COMMIT: {err}")
                failures += 1
                break
            ok, err = db_api.query(db, "write", SEED)
            if not ok:
                lines.append(f"SEED DID NOT COMMIT — every verdict would be a false pass: {err}")
                failures += 1
                break
            ok, err = db_api.query(db, "write", probe.query)
            got = "ACCEPT" if ok else "REFUSE"
            code_ok = ok or probe.code is None or probe.code in err
            agree = got == probe.expect and (got == "ACCEPT" or code_ok)
            failures += 0 if agree else 1
            detail = "" if ok else " " + _code(err)
            mark = "ok  " if agree else "FAIL"
            lines.append(f"{mark} {got:<6}{detail:<10} expected {probe.expect:<6} — {probe.label}")
            if not ok:
                lines.append(f"       {_message(err)}")
        finally:
            db_api.drop(db)

    total = len(PROBES)
    lines += ["", f"{total - failures} of {total} probes agree with their expected verdict."]
    report = "\n".join(lines)
    print(report)
    out = ROOT / "docs" / "research" / f"model_probe_{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n")
    print(f"\nwritten: {out.relative_to(ROOT)}")
    return 1 if failures else 0


def _message(err: str) -> str:
    try:
        return json.loads(err).get("message", "").splitlines()[0][:240]
    except (json.JSONDecodeError, AttributeError, IndexError):
        return err[:240]


def _code(err: str) -> str:
    try:
        return json.loads(err).get("code", "?")
    except (json.JSONDecodeError, AttributeError):
        return "?"


if __name__ == "__main__":
    sys.exit(main())
