"""The one exception type the PDF layer raises for user-facing failures."""
from __future__ import annotations


class PdfError(Exception):
    """A failure with an HTTP status and a message that is safe to show to the user.

    ``needs_password`` marks an encrypted input whose password is missing or wrong (the UI then asks
    once and retries); ``file_id`` names the input it concerns when known. The class pickles cleanly
    so it survives the trip back from the isolated child process.
    """

    def __init__(self, status: int, detail: str, *, needs_password: bool = False,
                 file_id: str | None = None, wrong_password: bool = False) -> None:
        super().__init__(detail)
        self.status = int(status)
        self.detail = str(detail)
        self.needs_password = bool(needs_password)
        self.file_id = file_id
        self.wrong_password = bool(wrong_password)

    def __reduce__(self):  # keyword-only args are not restored by the default reduce
        return (_rebuild, (self.status, self.detail, self.needs_password, self.file_id, self.wrong_password))

    @property
    def extra(self) -> dict:
        """Extra keys of the JSON error body (the platform's ``error_info`` merges them)."""
        body = self.to_dict()
        body.pop("detail", None)
        return body

    def to_dict(self) -> dict:
        body: dict = {"detail": self.detail}
        if self.needs_password:
            body["needs_password"] = True
        if self.file_id:
            body["file_id"] = self.file_id
        if self.wrong_password:
            body["wrong_password"] = True
        return body

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PdfError({self.status}, {self.detail!r})"


def _rebuild(status: int, detail: str, needs_password: bool, file_id: str | None,
             wrong_password: bool = False) -> PdfError:
    return PdfError(status, detail, needs_password=needs_password, file_id=file_id, wrong_password=wrong_password)


def password_error(name: str, *, wrong: bool, file_id: str | None = None) -> PdfError:
    if wrong:
        msg = f"The password for “{name}” is incorrect"
    else:
        msg = f"“{name}” is password-protected; enter its password to continue"
    return PdfError(422, msg, needs_password=True, file_id=file_id, wrong_password=wrong)
