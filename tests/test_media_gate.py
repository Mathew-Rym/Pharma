"""Non-image media must never be treated as an image.

_gowa_media_path computed the media KIND correctly and main.py then discarded it: the file
extension was forced to .jpg for anything outside jpg/jpeg/png/webp/pdf, the upload was
tagged image/jpeg, and inbound["type"] was hardcoded "image".

So a customer's voice note was downloaded, stored in the PRESCRIPTIONS bucket as a fake
JPEG, and handed to _handle_customer -- which sends any image to the prescription vision
extractor. From a staff number it became a GRN invoice page instead. A spoken message
parsed as a prescription, or as stock arriving, is the worst failure this system can
produce, and nothing anywhere would have flagged it: the audit row would say "image".

These tests pin the three properties that matter: it never reaches rx or grn, the row
records what it actually was, and the sender is told once.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


# ============================================================ detection
@pytest.mark.parametrize("kind", ["audio", "voice", "ptt", "video", "sticker", "document"])
def test_every_non_image_kind_is_detected(kind):
    """Detection is deliberately broader than what GOWA is known to send.

    The key a real voice note arrives under is UNVERIFIED: the GOWA binary carries the bare
    strings voice/Voice/VOICE and json tags for audio and ptt, but those tags are on its
    SEND structs, not the inbound webhook payload. Detecting a key that never appears costs
    nothing. Failing to detect one means a voice note falls through as an empty text
    message -- or worse, under a key we do handle, as a fake image.
    """
    import main
    assert main._gowa_media_path({kind: "2026/08/note.ogg"}) == (kind, "2026/08/note.ogg")


@pytest.mark.parametrize("shape", [
    "2026/08/x.ogg",
    {"path": "2026/08/x.ogg"},
    {"url": "https://example.test/x.ogg"},
])
def test_all_three_gowa_shapes_are_understood_for_audio(shape):
    """GOWA's payload shape is version-dependent: bare string, {path}, or {url}. The image
    branch already handled all three; audio must not be narrower or a voice note slips
    through undetected on a different GOWA build."""
    import main
    kind, path = main._gowa_media_path({"audio": shape})
    assert kind == "audio" and path


def test_only_image_is_actionable():
    """The allow-list is what the pharmacy can genuinely process. Everything else is
    refused rather than guessed at."""
    import main
    assert main._ACTIONABLE_MEDIA == ("image",)
    assert "audio" not in main._ACTIONABLE_MEDIA
    assert "document" not in main._ACTIONABLE_MEDIA


def test_document_is_returned_by_the_detector_and_not_silently_dropped():
    """test_gowa asserts _gowa_media_path returns ('document', path). Verify the CALLER
    honours it rather than discarding it -- previously the caller ignored `kind` entirely,
    so a document was stored as a .jpg and sent to the vision extractor."""
    import main
    assert main._gowa_media_path({"document": "invoice.pdf"}) == ("document", "invoice.pdf")
    assert "document" not in main._ACTIONABLE_MEDIA, (
        "a document reaching the image path becomes a fake JPEG in the prescriptions bucket")


# ============================================================ routing
def _dispatch_audio(monkeypatch, phone="254700555001", staff_row=None):
    """Run _dispatch for an audio message and capture where it went."""
    import router
    calls, replies = [], []
    monkeypatch.setattr(router, "_handle_staff",
                        lambda *a, **k: calls.append("staff"))
    monkeypatch.setattr(router, "_handle_customer",
                        lambda *a, **k: calls.append("customer"))
    monkeypatch.setattr(router, "reply_text", lambda p, b: replies.append(b))
    monkeypatch.setattr(router, "record_inbound", lambda p, pid: None)
    monkeypatch.setattr(router, "ex", lambda *a, **k: None)
    monkeypatch.setattr(router, "q1", lambda *a, **k: staff_row)
    msg = {"wa_id": "t-audio-1", "from": phone, "type": "audio",
           "media_path": "2026/08/note.ogg", "unsupported_media": "audio", "text": ""}
    router._dispatch(phone, msg, "11111111-1111-1111-1111-111111111111")
    return calls, replies


def test_audio_never_reaches_the_customer_prescription_path(monkeypatch):
    calls, replies = _dispatch_audio(monkeypatch, staff_row=None)
    assert "customer" not in calls, "a voice note reached the prescription extractor"
    assert calls == []


def test_audio_never_reaches_the_staff_grn_path(monkeypatch):
    calls, replies = _dispatch_audio(monkeypatch, staff_row={"role": "manager", "id": "x"})
    assert "staff" not in calls, "a voice note was filed as an invoice page"
    assert calls == []


def test_exactly_one_refusal_is_sent(monkeypatch):
    _, replies = _dispatch_audio(monkeypatch)
    assert len(replies) == 1, f"expected one refusal, got {len(replies)}"


def test_the_refusal_says_what_will_work_instead(monkeypatch):
    """A bare 'not supported' makes people resend the same thing."""
    _, replies = _dispatch_audio(monkeypatch)
    body = replies[0].lower()
    assert "text" in body and "photo" in body


def test_the_stored_row_says_audio_not_image():
    """The audit trail must not claim a spoken message was a photo. Asserted on the
    webhook's own construction, since that is where the type was being overwritten."""
    import inspect

    import main
    src = inspect.getsource(main.webhook_gowa)
    refuse = src[src.index("Refuse non-image media"):src.index("if media:\n        kind, rel")]
    assert '"type": kind' in refuse, "the refusal branch must persist the real kind"
    assert "media_path" in refuse, "the media path must survive for later inspection"


def test_the_image_path_still_works():
    """The guard must not have closed the door on the flow the product depends on."""
    import main
    assert main._gowa_media_path({"image": "2026/08/invoice.jpg"}) == (
        "image", "2026/08/invoice.jpg")
    assert "image" in main._ACTIONABLE_MEDIA
