"""Concurrency test for the on-demand entity reconcile.

Proves that a burst of unknown-entity emails arriving at once triggers exactly ONE Drive
folder-list (coalesced under the lock), that followers see the first thread's import, and that
known-entity emails never touch Drive.

Run: python -m app.scripts.test_reconcile_concurrency
"""
import threading
import time
import types

from app.services.decision_service import DecisionValidation
from app.services.processing_service import ProcessingService
from app.services.types import ClassificationResult


class FakeEntities:
    def __init__(self, names):
        self.names = set(names)
        self.lock = threading.Lock()
        self.import_calls = 0

    def list_active(self, db):
        return [types.SimpleNamespace(entity_name=n) for n in sorted(self.names)]

    def import_entities(self, db, folders):
        with self.lock:
            self.import_calls += 1
            before = len(self.names)
            for f in folders:
                self.names.add(f["name"])
            return {"created": len(self.names) - before, "updated": 0}


class FakeDrive:
    def __init__(self, folders=None):
        self.list_calls = 0
        self.lock = threading.Lock()
        self._folders = folders if folders is not None else [{"name": "NEW LLC", "id": "drv1"}]

    def list_level1_folders(self):
        with self.lock:
            self.list_calls += 1
        time.sleep(0.05)  # widen the race window so a herd would be obvious
        return list(self._folders)


class FakeFiling:
    def __init__(self, folders=None):
        self.drive = FakeDrive(folders)


class FakeValidator:
    def validate(self, classification, email, issues, entities, artifacts=None):
        known = {e.entity_name for e in entities}
        ok = classification.entity in known
        return DecisionValidation(
            decision=classification,
            final_action="file" if ok else "needs_review",
            reasons=[] if ok else ["unknown_entity"],
            audit={},
        )


class FakeClassifier:
    def __init__(self):
        self.calls = 0

    def classify(self, db, prepared, entities):
        self.calls += 1
        return ClassificationResult(
            entity="NEW LLC", level2="x", level3=None, file_summary="", confidence=95,
            unknown_entity=False, needs_review=False, reason="", decision_audit={},
        )


class FakeDB:
    def commit(self):
        pass


def _proc(entities, drive_filing, validator, classifier):
    p = ProcessingService.__new__(ProcessingService)  # bypass heavy __init__
    from app.core.config import get_settings
    p.settings = get_settings()
    p.entities = entities
    p.filing = drive_filing
    p.validator = validator
    p.classifier = classifier
    return p


def _decision(entity):
    return ClassificationResult(
        entity=entity, level2="x", level3=None, file_summary="", confidence=70,
        unknown_entity=False, needs_review=False, reason="", decision_audit={},
    )


def main() -> None:
    assert ProcessingService.__new__(ProcessingService) is not None
    # Reset shared class state so the cooldown from a prior run doesn't interfere.
    ProcessingService._last_reconcile_attempt = None

    # --- Case 1: burst of 8 unknown-entity emails -> ONE Drive list, all resolved. ---
    entities = FakeEntities(names=[])            # registry starts empty: NEW LLC is unknown
    filing = FakeFiling()
    classifier = FakeClassifier()
    proc = _proc(entities, filing, FakeValidator(), classifier)
    prepared = types.SimpleNamespace(issues=[])
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()  # release all threads simultaneously
        classification = _decision("NEW LLC")
        pristine = _decision("NEW LLC")
        validation = DecisionValidation(decision=classification, final_action="needs_review", reasons=["unknown_entity"], audit={})
        _, _, val = proc._reconcile_unknown_entities(
            FakeDB(), prepared, object(), [], classification, pristine, [], validation
        )
        results.append(val.should_file)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert filing.drive.list_calls == 1, f"expected 1 Drive list (coalesced), got {filing.drive.list_calls}"
    assert entities.import_calls == 1, f"expected 1 import, got {entities.import_calls}"
    assert all(results) and len(results) == 8, f"all 8 should resolve to file, got {results}"
    assert classifier.calls == 0, f"no reclassify needed (cheap re-validate sufficed), got {classifier.calls}"

    # --- Case 2: known-entity email -> NEVER touches Drive. ---
    entities2 = FakeEntities(names=["NEW LLC"])  # already known
    filing2 = FakeFiling()
    proc2 = _proc(entities2, filing2, FakeValidator(), FakeClassifier())
    classification = _decision("NEW LLC")
    # Even if validation says review for some other reason, no unknown entity => no Drive call.
    validation = DecisionValidation(decision=classification, final_action="needs_review", reasons=["low_confidence"], audit={})
    proc2._reconcile_unknown_entities(FakeDB(), prepared, object(), [], classification, _decision("NEW LLC"), entities2.list_active(None), validation)
    assert filing2.drive.list_calls == 0, f"known entity must not sync Drive, got {filing2.drive.list_calls}"

    # --- Case 3: unknown entity whose folder does NOT exist in Drive -> stays review. ---
    ProcessingService._last_reconcile_attempt = None  # avoid the cooldown from Case 1
    entities3 = FakeEntities(names=["A LLC"])           # A known, but "GHOST LLC" not anywhere
    filing3 = FakeFiling(folders=[{"name": "A LLC", "id": "a"}])  # Drive has no GHOST folder
    classifier3 = FakeClassifier()
    proc3 = _proc(entities3, filing3, FakeValidator(), classifier3)
    classification = _decision("GHOST LLC")
    validation = DecisionValidation(decision=classification, final_action="needs_review", reasons=["unknown_entity"], audit={})
    active3 = entities3.list_active(None)
    _, _, val3 = proc3._reconcile_unknown_entities(
        FakeDB(), prepared, object(), [], classification, _decision("GHOST LLC"), active3, validation
    )
    assert filing3.drive.list_calls == 1, f"should check Drive once, got {filing3.drive.list_calls}"
    assert val3.should_review, "folder not found in Drive -> must stay Needs Review"
    assert classifier3.calls == 0, "no reclassify when the entity was never found"

    # --- Case 4: multi-entity, one of the three unknown but its folder EXISTS -> resolves. ---
    ProcessingService._last_reconcile_attempt = None
    entities4 = FakeEntities(names=["A LLC", "B LLC"])  # C is missing locally
    filing4 = FakeFiling(folders=[{"name": "A LLC", "id": "a"}, {"name": "B LLC", "id": "b"}, {"name": "C LLC", "id": "c"}])
    proc4 = _proc(entities4, filing4, FakeValidator(), FakeClassifier())
    # Primary A (known); additional B (known) + C (unknown) -> only C triggers the Drive check.
    classification = ClassificationResult(
        entity="C LLC", level2="x", level3=None, file_summary="", confidence=95,
        unknown_entity=False, needs_review=False, reason="",
        decision_audit={"additional_entities": ["A LLC", "B LLC"]},
    )
    validation = DecisionValidation(decision=classification, final_action="needs_review", reasons=["multiple_entities"], audit={})
    _, _, val4 = proc4._reconcile_unknown_entities(
        FakeDB(), prepared, object(), [], classification, ClassificationResult(
            entity="C LLC", level2="x", level3=None, file_summary="", confidence=95,
            unknown_entity=False, needs_review=False, reason="", decision_audit={"additional_entities": ["A LLC", "B LLC"]},
        ), entities4.list_active(None), validation,
    )
    assert filing4.drive.list_calls == 1, f"should check Drive once, got {filing4.drive.list_calls}"
    assert val4.should_file, "C's folder exists in Drive -> should resolve and file"

    print("reconcile concurrency: all assertions passed")
    print({"drive_list_calls_burst": filing.drive.list_calls, "imports": entities.import_calls,
           "resolved": sum(results), "reclassifies": classifier.calls, "known_entity_drive_calls": filing2.drive.list_calls})


if __name__ == "__main__":
    main()
