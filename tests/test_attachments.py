import base64
import unittest

from fastapi import HTTPException

from attachments import AttachmentInput, process_attachments, redact_sensitive


def attachment(name: str, mime: str, data: bytes) -> AttachmentInput:
    return AttachmentInput(name=name, mime=mime, data_base64=base64.b64encode(data).decode("ascii"))


class AttachmentTests(unittest.TestCase):
    def test_rejects_more_than_three_files(self):
        files = [attachment(f"{index}.jpg", "image/jpeg", b"\xff\xd8\xffx\xff\xd9") for index in range(4)]
        with self.assertRaises(HTTPException) as error:
            process_attachments(files)
        self.assertEqual(error.exception.status_code, 413)

    def test_accepts_jpeg_and_webp_by_content(self):
        files = [
            attachment("product.jpg", "image/jpeg", b"\xff\xd8\xffproduct\xff\xd9"),
            attachment("product.webp", "image/webp", b"RIFF\x04\x00\x00\x00WEBPdata"),
        ]
        processed = process_attachments(files)
        self.assertEqual([result.status for result in processed.results], ["ok", "ok"])
        self.assertEqual(len(processed.image_urls), 2)

    def test_legacy_office_format_has_consistent_kind(self):
        processed = process_attachments([attachment("order.xls", "application/octet-stream", b"legacy")])
        self.assertEqual(processed.results[0].kind, "xlsx")
        self.assertEqual(processed.results[0].status, "error")

    def test_sensitive_values_are_redacted(self):
        cleaned = redact_sensitive("карта 4400 0000 0000 0000 CVV 123 пароль=qwerty")
        self.assertNotIn("4400", cleaned)
        self.assertNotIn("123", cleaned)
        self.assertNotIn("qwerty", cleaned)


if __name__ == "__main__":
    unittest.main()
