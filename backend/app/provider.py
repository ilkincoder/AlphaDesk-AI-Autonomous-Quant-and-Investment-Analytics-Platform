"""One rule the outbound provider clients share: a credential never leaves in a message.

Every provider this application talks to is free to quote a key, a secret or a token back in an
error body, and every one of them is talked to with `httpx`, whose own exceptions can carry the
request line. A message built from any of that would put the credential in a log line, in a
traceback, or in a response the frontend reads -- so each client scrubs the text before it
becomes an error.

The clients keep their own copy of the *reason* in their module docstrings, because each one
names its own provider. The rule itself is here, so that three clients cannot end up applying
three slightly different versions of it.
"""

__all__ = ["redact"]


def redact(text: str, *secrets: str | None) -> str:
    """Replace any of `secrets` appearing in `text` with a fixed marker.

    Blank and absent secrets are skipped rather than replaced: an empty string is a substring
    of everything, and replacing it would turn the whole message into markers.
    """
    for secret in secrets:
        if secret and secret.strip():
            text = text.replace(secret, "***")
    return text
