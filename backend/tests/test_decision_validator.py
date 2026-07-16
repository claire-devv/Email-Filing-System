from datetime import datetime, timezone

from app.db.models import Entity, ProcessedEmail
from app.services.classifier_service import ClassifierService
from app.services.decision_service import DecisionValidator
from app.services.types import ClassificationResult


def _decision(**kwargs) -> ClassificationResult:
    defaults = {
        "action": "file",
        "entity": "C. Lacombe - Cast CMR LLC",
        "level2": "Property Taxes",
        "level3": None,
        "file_summary": "San Diego County Property Tax Receipt for Cast CMR LLC",
        "document_date": "2026-05-06",
        "confidence": 95,
        "unknown_entity": False,
        "needs_review": False,
        "reason": "test",
    }
    defaults.update(kwargs)
    return ClassificationResult(**defaults)


def main() -> None:
    validator = DecisionValidator()
    email = ProcessedEmail(
        gmail_message_id="test",
        subject="Property tax receipt",
        sender="client@example.com",
        received_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
    )
    entities = [Entity(entity_name="C. Lacombe - Cast CMR LLC", folder_name="C. Lacombe - Cast CMR LLC")]

    cases = {
        "confidence_fraction": validator.validate(_decision(confidence=0.82, level3="Receipts"), email, [], entities),
        "low_confidence": validator.validate(_decision(confidence=45), email, [], entities),
        "claude_review_high_confidence": validator.validate(_decision(action="needs_review", confidence=95, needs_review=True), email, [], entities),
        "unsafe_reject": validator.validate(_decision(action="reject", confidence=95), email, [], entities),
        "clear_reject": validator.validate(
            _decision(
                action="reject",
                entity=None,
                level2=None,
                file_summary="Instagram notification unrelated social email",
                reason="unrelated social notification",
            ),
            email,
            [],
            entities,
        ),
        "partial_entity": validator.validate(_decision(entity="Cast CMR LLC"), email, [], entities),
        "required_year_subfolder": validator.validate(_decision(level2="Receipts / Supporting Documents", level3=None), email, [], entities),
        "missing_bank_subfolder": validator.validate(_decision(level2="Bank Statements", level3=None), email, [], entities),
        "bad_document_date": validator.validate(_decision(document_date="1970-01-01"), email, [], entities),
        "approve_low_confidence": validator.validate(_decision(confidence=45), email, [], entities, force_file=True),
        "approve_unknown_entity": validator.validate(
            _decision(confidence=45, entity="Totally Unknown LLC"), email, [], entities, force_file=True
        ),
        "correct_new_drive_folder_entity": validator.validate(
            _decision(entity="Foremost Professional Plaza", level2="Client Reporting", level3="2026"),
            email,
            [],
            entities,
            allow_new_entity=True,
            force_file=True,
        ),
        "correct_bad_drive_folder_entity": validator.validate(
            _decision(entity="Bad/Folder", level2="Client Reporting", level3="2026"),
            email,
            [],
            entities,
            allow_new_entity=True,
            force_file=True,
        ),
        # Ambiguous inline image is the ONLY issue + the email is a confident known entity ->
        # must NOT hold for review (the image is decoration). No artifacts => single-entity path.
        "ambiguous_image_confident": validator.validate(
            _decision(confidence=95), email, ["Outlook-image.png.png: ambiguous_image_part"], entities
        ),
        # Ambiguous image + LOW confidence -> still review (the doc itself is uncertain).
        "ambiguous_image_low_conf": validator.validate(
            _decision(confidence=45), email, ["Outlook-image.png.png: ambiguous_image_part"], entities
        ),
        # A GENUINE attachment conversion failure still forces review, regardless of confidence.
        "real_conversion_failure": validator.validate(
            _decision(confidence=95), email, ["invoice.pdf: conversion failed: boom"], entities
        ),
        # A human Approve overrides a conversion/quality warning (they've seen it and chose to file).
        "approve_conversion_warning": validator.validate(
            _decision(confidence=95), email, ["invoice.pdf: conversion failed: boom"], entities, force_file=True
        ),
        # ...but a human Approve still cannot file to a structurally-invalid entity.
        "approve_still_blocks_unknown": validator.validate(
            _decision(confidence=95, entity="Totally Unknown LLC"), email, ["x: ambiguous_image_part"], entities, force_file=True
        ),
    }
    parse_failure = ClassifierService()._parse_json_response("not json")
    parse_with_extra_json = ClassifierService()._parse_json_response(
        """
        {
          "action": "file",
          "entity": "Sample Entity LLC",
          "level2": "Insurance",
          "file_summary": "Sample",
          "confidence": 90
        }
        {"debug":"second object that should be ignored"}
        """
    )

    assert cases["confidence_fraction"].decision.confidence == 82
    assert cases["confidence_fraction"].decision.level3 is None
    assert cases["confidence_fraction"].final_action == "file"
    assert cases["low_confidence"].final_action == "needs_review"
    assert "low_confidence" in cases["low_confidence"].reasons
    assert cases["claude_review_high_confidence"].final_action == "needs_review"
    assert cases["unsafe_reject"].final_action == "needs_review"
    assert cases["clear_reject"].final_action == "reject"
    assert cases["partial_entity"].final_action == "needs_review"
    assert "unknown_entity" in cases["partial_entity"].reasons
    assert cases["required_year_subfolder"].final_action == "file"
    assert cases["required_year_subfolder"].decision.level3 == "2026"
    assert cases["missing_bank_subfolder"].final_action == "needs_review"
    assert "missing_required_level3" in cases["missing_bank_subfolder"].reasons
    assert cases["bad_document_date"].audit["filename_date"] == "2026.06.08"
    assert cases["bad_document_date"].decision.document_date == "2026-06-08"
    # Human Approve overrides the low-confidence gate...
    assert cases["approve_low_confidence"].final_action == "file"
    assert "low_confidence" not in cases["approve_low_confidence"].reasons
    # ...but structural problems (unknown entity) still block the approve.
    assert cases["approve_unknown_entity"].final_action == "needs_review"
    assert "unknown_entity" in cases["approve_unknown_entity"].reasons
    assert cases["correct_new_drive_folder_entity"].final_action == "file"
    assert cases["correct_new_drive_folder_entity"].audit["new_entity_requested"] is True
    assert cases["correct_bad_drive_folder_entity"].final_action == "needs_review"
    # Ambiguous inline image no longer blocks a confident, single known-entity filing...
    assert cases["ambiguous_image_confident"].final_action == "file", cases["ambiguous_image_confident"].reasons
    assert "partial_conversion_failure" not in cases["ambiguous_image_confident"].reasons
    # ...but a low-confidence doc with an ambiguous image still reviews (on its own merits)...
    assert cases["ambiguous_image_low_conf"].final_action == "needs_review"
    # ...and a genuine conversion failure still forces review even at high confidence (auto path).
    assert cases["real_conversion_failure"].final_action == "needs_review"
    assert "partial_conversion_failure" in cases["real_conversion_failure"].reasons
    # A human Approve overrides a conversion/quality warning...
    assert cases["approve_conversion_warning"].final_action == "file", cases["approve_conversion_warning"].reasons
    # ...but still cannot file to a structurally-invalid (unknown) entity.
    assert cases["approve_still_blocks_unknown"].final_action == "needs_review"
    assert "unknown_entity" in cases["approve_still_blocks_unknown"].reasons
    assert "unknown_entity" in cases["correct_bad_drive_folder_entity"].reasons
    assert parse_failure.action == "needs_review"
    assert parse_with_extra_json.action == "file"
    assert parse_with_extra_json.entity == "Sample Entity LLC"
    assert parse_failure.needs_review_reason == "classification_parse_failure"

    print({name: {"action": item.final_action, "reasons": item.reasons} for name, item in cases.items()})


if __name__ == "__main__":
    main()
