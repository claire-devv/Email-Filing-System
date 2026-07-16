"""Activity-feed per-row scoping for multi-entity splits.

Verifies each FilingLog row resolves its OWN Drive folder link and lists only its OWN files,
for 2 and 3 entities, with no regression for single-entity dual-filing.

Run: python -m app.scripts.test_activity_row_split
"""
from app.api.routes.activity import _folder_drive_id, _row_artifacts
from app.db.models import FileArtifact, FilingLog

ROOT = "RRES - File Test_Claire"


def _att(entity, fid, level2="Client Reporting", year="2027"):
    return FileArtifact(
        kind="attachment",
        original_filename=f"{entity}.pdf",
        local_path="/x.pdf",
        drive_file_id=f"file-{fid}",
        drive_folder_id=fid,
        drive_link=f"https://drive/{fid}",
        status="filed",
        metadata_json={"folder_path": f"{ROOT} / {entity} / {level2} / {year}"},
    )


def _combined(primary):
    return FileArtifact(
        kind="combined_package", original_filename=None, local_path="/c.pdf",
        drive_file_id="file-comb", drive_folder_id="comb-folder", drive_link="https://drive/comb",
        status="filed", metadata_json={"folder_path": f"{ROOT} / {primary} / Communications"},
    )


def _body():
    return FileArtifact(kind="email_body", local_path="/b.pdf", status="internal", metadata_json={})


def _log(entity, level2="Client Reporting", year="2027"):
    return FilingLog(entity=entity, folder_path=f"{ROOT} / {entity} / {level2} / {year}", status="filed")


def _names(arts):
    return sorted(a.original_filename or a.kind for a in arts)


def main() -> None:
    # ---- 3-entity split ----
    E = ["J. Claffey - 1339-45 N Front", "J. Claffey - 210 W. Girard Owner LLC", "K. Smith - 500 Main LLC"]
    atts = [_att(E[0], "f1"), _att(E[1], "f2"), _att(E[2], "f3")]
    artifacts = [_body(), *atts, _combined(E[0])]
    logs = [_log(E[0]), _log(E[1]), _log(E[2])]

    folder_ids = [_folder_drive_id(lg, artifacts) for lg in logs]
    assert folder_ids == ["f1", "f2", "f3"], folder_ids
    assert len(set(folder_ids)) == 3, "3 entities must yield 3 distinct folder links"

    for i, lg in enumerate(logs):
        shown = _row_artifacts(lg, artifacts)
        names = _names(shown)
        # row shows only its own attachment + the combined PDF, never another entity's report
        assert f"{E[i]}.pdf" in names, (i, names)
        assert all(f"{E[j]}.pdf" not in names for j in range(3) if j != i), (i, names)
        assert "combined_package" in names, names
        assert "email_body.pdf" not in names and "email_body" not in names

    # ---- 2-entity split (folder links distinct) ----
    atts2 = [_att(E[0], "g1"), _att(E[1], "g2")]
    arts2 = [_body(), *atts2, _combined(E[0])]
    logs2 = [_log(E[0]), _log(E[1])]
    ids2 = [_folder_drive_id(lg, arts2) for lg in logs2]
    assert ids2 == ["g1", "g2"] and len(set(ids2)) == 2, ids2

    # ---- single-entity dual-filing: one row shows ALL its attachments (no regression) ----
    one = "J. Doe - 12 Oak LLC"
    a_ins = _att(one, "h1", level2="Insurance", year="")
    a_ins.metadata_json = {"folder_path": f"{ROOT} / {one} / Insurance"}
    a_ins.original_filename = "policy.pdf"
    a_bank = _att(one, "h2", level2="Bank Statements", year="")
    a_bank.metadata_json = {"folder_path": f"{ROOT} / {one} / Bank Statements / Chase 1234"}
    a_bank.original_filename = "statement.pdf"
    arts1 = [_body(), a_ins, a_bank, _combined(one)]
    log1 = _log(one, level2="Insurance", year="")
    log1.folder_path = f"{ROOT} / {one} / Insurance"
    shown1 = _names(_row_artifacts(log1, arts1))
    assert "policy.pdf" in shown1 and "statement.pdf" in shown1, shown1  # both attachments kept
    assert "combined_package" in shown1

    print("activity row split: all assertions passed")
    print({"3-entity folder ids": folder_ids, "2-entity folder ids": ids2, "single-entity files": shown1})


if __name__ == "__main__":
    main()
