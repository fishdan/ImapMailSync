"""Private account configuration loading and validation."""

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re


ACCOUNT_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


class ConfigError(Exception):
    """A sanitized account configuration error."""


@dataclass(frozen=True)
class Account:
    account_id: str
    provider: str
    username: str
    password: str = field(repr=False)
    host: str = ""
    port: int = 993

    @property
    def role(self) -> str:
        return "destination" if self.account_id == "alpha" else "source"

    @property
    def identity(self) -> str:
        values = (self.account_id, self.role, self.host.lower(), self.port, self.username, "INBOX")
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def load_account(root: Path, account_id: str) -> Account:
    if not ACCOUNT_ID.fullmatch(account_id):
        raise ConfigError("INVALID_ACCOUNT_ID")
    accounts_dir = root / "accounts"
    account_dir = accounts_dir / account_id
    secret_dir = account_dir / ".secrets"
    path = secret_dir / "account.json"
    if accounts_dir.is_symlink() or account_dir.is_symlink() or secret_dir.is_symlink() or path.is_symlink():
        raise ConfigError("UNSAFE_CONFIG_PATH")
    if not secret_dir.is_dir() or not path.is_file():
        raise ConfigError("MISSING_CONFIG")
    try:
        insecure = secret_dir.stat().st_mode & 0o077 or path.stat().st_mode & 0o077
    except OSError as exc:
        raise ConfigError("UNREADABLE_CONFIG") from exc
    if insecure:
        raise ConfigError("UNSAFE_CONFIG_PERMISSIONS")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("INVALID_CONFIG_JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("imap"), dict):
        raise ConfigError("INVALID_CONFIG_SHAPE")
    provider, username, password = (data.get(key) for key in ("provider", "username", "password"))
    imap = data["imap"]
    host, port = imap.get("host"), imap.get("port")
    if not all(isinstance(value, str) and value.strip() for value in (provider, username, password, host)):
        raise ConfigError("MISSING_CONFIG_VALUE")
    if username.startswith("your-address@") or password.startswith("YOUR_"):
        raise ConfigError("PLACEHOLDER_CREDENTIALS")
    if any(char.isspace() for char in host) or "/" in host:
        raise ConfigError("INVALID_IMAP_HOST")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError("INVALID_IMAP_PORT")
    if imap.get("security") != "SSL/TLS" or imap.get("authenticationRequired") is not True:
        raise ConfigError("INSECURE_IMAP_CONFIG")
    return Account(account_id, provider, username, password, host, port)


def discover_accounts(root: Path) -> tuple[dict[str, Account], dict[str, str]]:
    accounts: dict[str, Account] = {}
    errors: dict[str, str] = {}
    parent = root / "accounts"
    if parent.is_dir():
        for path in sorted(parent.iterdir()):
            if path.name == "example" or not path.is_dir():
                continue
            try:
                accounts[path.name] = load_account(root, path.name)
            except ConfigError as exc:
                errors[path.name] = str(exc)
    if "alpha" not in accounts and "alpha" not in errors:
        errors["alpha"] = "MISSING_CONFIG"
    return accounts, errors
