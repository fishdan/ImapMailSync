"""Source-address label derivation and safe IMAP quoted strings."""


class LabelError(Exception):
    """A sanitized invalid source address or label name."""


def derive_source_label(address: str) -> str:
    if not isinstance(address, str) or address.count("@") != 1:
        raise LabelError("INVALID_SOURCE_ADDRESS")
    local, domain = address.split("@")
    parts = domain.split(".")
    if (not local or len(parts) < 2 or any(not part for part in parts)
            or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in address)):
        raise LabelError("INVALID_SOURCE_ADDRESS")
    label = local + "@" + ".".join(parts[:-1])
    try:
        encode_imap_label(label)
    except LabelError as exc:
        raise LabelError("INVALID_SOURCE_ADDRESS") from exc
    return label


def encode_imap_label(label: str) -> bytes:
    """Return one ASCII IMAP quoted string; reject unrepresentable names."""
    if (not isinstance(label, str) or not label
            or any(ord(char) < 32 or ord(char) >= 127 for char in label)):
        raise LabelError("INVALID_LABEL_NAME")
    return b'"' + label.replace("\\", "\\\\").replace('"', '\\"').encode("ascii") + b'"'
