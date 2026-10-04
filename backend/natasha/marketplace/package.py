"""Package handling and the pre-install security pipeline."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..core import ConfigurationError, VerificationFailed, sha256_file
from ..core.clock import iso
from ..core.paths import get_paths

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".skill", ".natasha")
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024


class PackageSource(str, Enum):
    LOCAL = "local"
    URL = "url"
    REGISTRY = "registry"

    @classmethod
    def parse(cls, value: object) -> "PackageSource":
        text = str(value or "").lower()
        for member in cls:
            if member.value == text:
                return member
        return cls.LOCAL


DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("subprocess_shell", re.compile(r"subprocess\.[a-z_]+\([^)]*shell\s*=\s*True")),
    ("eval_exec", re.compile(r"(?<![.\w])(eval|exec)\s*\(")),
    ("credential_paths", re.compile(r"(?i)(/\.ssh/|\.aws/credentials|id_rsa|NATASHA_MASTER_KEY)")),
    ("env_exfil", re.compile(r"(?i)(os\.environ|process\.env)[^\n]{0,60}(https?://|requests\.|httpx\.)")),
    ("obfuscated_payload", re.compile(r"(base64\.b64decode|codecs\.decode|fromCharCode)\s*\([^)]{60,}")),
    ("raw_socket", re.compile(r"(?i)(socket\.socket|pty\.spawn|/dev/tcp)")),
    ("install_hook", re.compile(r"(?i)(postinstall|preinstall|setup\.py[^\n]{0,40}(urlopen|requests))")),
    ("governance_tamper", re.compile(r"(?i)(constitution|policy\.py|events/log\.py)[^\n]{0,40}(write|open|rm|delete)")),
)


@dataclass
class MarketplacePackage:
    """A downloaded/verified package."""

    name: str
    version: str
    kind: str = "skill"           # skill | plugin | mcp | provider | agent | workflow | connector
    source: PackageSource = PackageSource.LOCAL
    origin: str = ""
    path: str = ""
    checksum: str = ""
    expected_checksum: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    publisher: str = ""
    signature: str = ""
    permissions: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=iso)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "kind": self.kind, "source": self.source.value,
            "origin": self.origin, "checksum": self.checksum, "publisher": self.publisher,
            "permissions": self.permissions, "dependencies": self.dependencies, "key": self.key,
        }


@dataclass
class SecurityReport:
    """Result of the pre-install pipeline."""

    package: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, str]] = field(default_factory=list)
    passed: bool = False
    requires_owner_approval: bool = False
    risk: str = "MEDIUM"
    generated_at: str = field(default_factory=iso)

    def add(self, step: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": step, "ok": ok, "detail": detail})
        if not ok:
            self.passed = False

    @property
    def failed_steps(self) -> list[str]:
        return [step["step"] for step in self.steps if not step["ok"]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package, "passed": self.passed, "risk": self.risk, "steps": self.steps,
            "findings": self.findings, "requires_owner_approval": self.requires_owner_approval,
            "failed_steps": self.failed_steps, "generated_at": self.generated_at,
        }


def download(source: str, *, destination: Path | None = None, timeout: float = 60.0) -> tuple[Path, PackageSource]:
    """Materialise a package from a local path or an HTTP URL (size-capped)."""
    target_dir = destination or Path(tempfile.mkdtemp(prefix="natasha-marketplace-", dir=str(get_paths().ensure().marketplace)))
    target_dir.mkdir(parents=True, exist_ok=True)

    if source.startswith(("http://", "https://")):
        import urllib.request

        name = Path(source.split("?")[0]).name or "package.download"
        target = target_dir / name
        request = urllib.request.Request(source, headers={"User-Agent": "Natasha/0.9"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit owner-provided URL
            declared = int(response.headers.get("content-length", 0) or 0)
            if declared > MAX_ARCHIVE_BYTES:
                raise ConfigurationError(f"package too large: {declared} bytes")
            payload = response.read(MAX_ARCHIVE_BYTES + 1)
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise ConfigurationError("package exceeded the size limit while downloading")
        target.write_bytes(payload)
        return target, PackageSource.URL

    path = Path(source).expanduser()
    if not path.exists():
        raise ConfigurationError(f"package not found: {source}")
    if path.is_dir():
        return path, PackageSource.LOCAL
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ConfigurationError(f"package too large: {path.stat().st_size} bytes")
    return path, PackageSource.LOCAL


def extract(archive: Path, *, destination: Path | None = None) -> Path:
    """Extract an archive safely (no path traversal, no symlink escape)."""
    if archive.is_dir():
        return archive
    target = destination or Path(tempfile.mkdtemp(prefix="natasha-extract-", dir=str(get_paths().ensure().marketplace)))
    target.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()

    if name.endswith((".zip", ".skill", ".natasha")) or zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                member_path = (target / member.filename).resolve()
                if not str(member_path).startswith(str(target.resolve())):
                    raise VerificationFailed(f"archive contains an unsafe path: {member.filename}")
                if member.file_size > MAX_ARCHIVE_BYTES:
                    raise VerificationFailed(f"archive member too large: {member.filename}")
            bundle.extractall(target)
        return target

    if name.endswith((".tar", ".tar.gz", ".tgz")):
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                member_path = (target / member.name).resolve()
                if not str(member_path).startswith(str(target.resolve())):
                    raise VerificationFailed(f"archive contains an unsafe path: {member.name}")
                if member.issym() or member.islnk():
                    raise VerificationFailed(f"archive contains a link: {member.name}")
            bundle.extractall(target, filter="data")
        return target

    raise ConfigurationError(f"unsupported package format: {archive.name}")


def compute_checksum(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(sha256_file(path).encode())
    return digest.hexdigest()


def verify_publisher(publisher: str, signature: str, checksum: str, *, trusted_publishers: list[str],
                     secret: bytes | None = None) -> tuple[bool, str]:
    """Verify a package signature. Unsigned packages from unknown publishers fail closed."""
    if not publisher:
        return False, "package has no publisher"
    if secret and signature:
        from ..core import hmac_verify

        payload = {"publisher": publisher, "checksum": checksum}
        if hmac_verify(secret, payload, signature):
            return True, f"signature verified for {publisher}"
        return False, "signature does not match the package contents"
    if publisher in trusted_publishers:
        return True, f"publisher {publisher} is locally trusted (unsigned package)"
    return False, (
        f"publisher {publisher!r} is neither trusted nor signed; "
        "add it to marketplace.trusted_publishers after verifying it yourself"
    )


def dependency_scan(package_dir: Path, declared: list[str]) -> tuple[bool, list[str], list[str]]:
    """Compare declared dependencies with what the code actually imports."""
    imports: set[str] = set()
    import_pattern = re.compile(r"^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)", re.M)
    for path in package_dir.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in import_pattern.finditer(text):
            imports.add(match.group(1).split(".")[0])
    stdlib = set(__import__("sys").stdlib_module_names)
    third_party = sorted(imports - stdlib - {"natasha", "__future__"})
    undeclared = sorted(set(third_party) - set(declared))
    return (not undeclared), third_party, undeclared


def static_scan(package_dir: Path) -> tuple[bool, list[dict[str, str]]]:
    """Scan source for dangerous patterns."""
    findings: list[dict[str, str]] = []
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".py", ".js", ".mjs", ".cjs", ".ts", ".sh"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pattern in DANGEROUS_PATTERNS:
            for match in pattern.finditer(text):
                findings.append(
                    {"file": str(path.relative_to(package_dir)), "pattern": name,
                     "excerpt": match.group(0)[:120]}
                )
                break
    return (not findings), findings


def inspect_package(
    source: str,
    *,
    kind: str = "skill",
    expected_checksum: str = "",
    trusted_publishers: list[str] | None = None,
    publisher_secret: bytes | None = None,
    run_sandbox_test: bool = True,
) -> tuple[MarketplacePackage, SecurityReport, Path]:
    """Run the pre-install pipeline and return the package, the report and the extracted directory."""
    from ..skills.manifest import load_manifest

    report = SecurityReport(package=source)
    path, origin = download(source)
    report.add("download", True, f"{path.name} from {origin.value}")

    extracted = extract(path)
    checksum = compute_checksum(extracted)
    report.add("checksum", True, checksum[:16])

    if expected_checksum:
        if checksum != expected_checksum:
            report.add("checksum_pin", False, f"expected {expected_checksum[:16]}, got {checksum[:16]}")
            raise VerificationFailed(f"checksum mismatch for {source}")
        report.add("checksum_pin", True, "matches the pinned checksum")

    manifest: dict[str, Any] = {}
    name = extracted.name
    version = "0.0.0"
    permissions: list[str] = []
    dependencies: list[str] = []
    publisher = ""
    signature = ""
    if kind == "skill":
        try:
            skill_manifest = load_manifest(extracted)
            manifest = skill_manifest.to_dict()
            name, version = skill_manifest.id, skill_manifest.version
            permissions, dependencies = skill_manifest.permissions, skill_manifest.dependencies
            publisher, signature = skill_manifest.publisher, skill_manifest.signature
            report.add("manifest", True, f"{name}@{version}")
        except Exception as exc:
            report.add("manifest", False, f"{type(exc).__name__}: {exc}")
    else:
        manifest_path = extracted / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text())
                name = manifest.get("name", name)
                version = manifest.get("version", version)
                permissions = list(manifest.get("permissions", []) or [])
                dependencies = list(manifest.get("dependencies", []) or [])
                publisher = manifest.get("publisher", "")
                signature = manifest.get("signature", "")
                report.add("manifest", True, f"{name}@{version}")
            except json.JSONDecodeError as exc:
                report.add("manifest", False, f"invalid manifest.json: {exc}")
        else:
            report.add("manifest", False, "no manifest.json in package")

    ok, detail = verify_publisher(publisher, signature, checksum,
                                  trusted_publishers=trusted_publishers or [], secret=publisher_secret)
    report.add("publisher", ok, detail)

    ok, third_party, undeclared = dependency_scan(extracted, dependencies)
    report.add("dependencies", ok, f"imports={len(third_party)} undeclared={undeclared}")

    ok, findings = static_scan(extracted)
    report.findings = findings
    report.add("static_scan", ok, f"{len(findings)} finding(s)")

    if run_sandbox_test:
        report.add("sandbox_test", True, "skipped: package is data-only" if kind != "skill" else "run by installer")

    risky_permissions = {"shell.exec", "code.exec", "credential.use", "browser.control", "fs.write"}
    report.requires_owner_approval = bool(set(permissions) & risky_permissions) or bool(findings)
    report.risk = "HIGH" if report.requires_owner_approval else ("LOW" if not findings else "MEDIUM")
    report.passed = all(step["ok"] for step in report.steps if step["step"] != "sandbox_test")

    package = MarketplacePackage(
        name=name, version=version, kind=kind, source=origin, origin=source, path=str(extracted),
        checksum=checksum, expected_checksum=expected_checksum, manifest=manifest, publisher=publisher,
        signature=signature, permissions=permissions, dependencies=dependencies,
    )
    return package, report, extracted
