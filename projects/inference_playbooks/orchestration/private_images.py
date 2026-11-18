"""Hub-vault image injection; never persist the resolved image in artifacts."""

import copy
import json
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from projects.core.dsl.utils.k8s import oc_get_json as _oc_get_json
from projects.core.library import env, vault


def oc_get_json(*args, **kwargs):
    """Resource reads can include private images even during conflict/cleanup checks."""
    try:
        return _oc_get_json(*args, **kwargs, handled_secretly=True)
    except Exception:
        raise RuntimeError("Recipe resource read failed") from None


NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
IMAGE = re.compile(
    r"^[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/[a-z0-9]+(?:[._/-]+[a-z0-9]+)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[a-f0-9]{64})?$"
)
SERVERS = {"vllm", "vllm-leader", "vllm-worker", "main"}


def image_binding(serving, overrides):
    source = (
        overrides
        if any(k in overrides for k in ("image", "image_env", "image_visibility"))
        else serving
    )
    if source.get("image_visibility", "public") not in {"public", "private"}:
        raise ValueError("invalid image visibility")
    if source.get("image_visibility", "public") != "private":
        if "image_env" in source:
            raise ValueError("image_env requires private image visibility")
        return None
    name = source.get("image_env")
    if not isinstance(name, str) or not NAME.fullmatch(name) or "image" in source:
        raise ValueError("private images require image_env and prohibit a literal image")
    return {"image_visibility": "private", "image_env": name}


def placeholder(binding):
    return "private.invalid/runtime:" + binding["image_env"]


def replace_images(document, expected, replacement):
    result = copy.deepcopy(document)
    count = 0

    def visit(value):
        nonlocal count
        if isinstance(value, dict):
            for container in value.get("containers", []):
                if container.get("name") in SERVERS:
                    if container.get("image") != expected:
                        raise ValueError("private serving image differs from its declared binding")
                    container["image"] = replacement
                    count += 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    if not count:
        raise ValueError("private images require supported serving containers")
    return result


class PrivateImage:
    """Values stay in memory. Public captures contain placeholders/withholding markers."""

    def __init__(self, binding, value):
        if (
            not isinstance(value, str)
            or not IMAGE.fullmatch(value)
            or not any(c in value.split("/")[0] for c in ".:")
            or value.startswith("private.invalid/")
        ):
            raise ValueError("private image vault value is missing or invalid")
        self.binding = dict(binding)
        self.value = value
        repository, _, digest = value.partition("@")
        tag = repository.rsplit("/", 1)[-1].partition(":")[2]
        if tag:
            repository = repository.rsplit(":", 1)[0]
        self.tokens = {
            v
            for v in (
                value,
                repository,
                repository.split("/")[0],
                repository.split("/", 1)[1],
                tag,
                digest,
            )
            if v
        }
        if digest:
            self.tokens.add(digest.removeprefix("sha256:"))
        self.digests = set()

    def __repr__(self):
        return "PrivateImage(<withheld>)"

    @classmethod
    def load(cls, binding):
        path = vault.get_vault_content_path("psap-forge-private-images", "images")
        if path is None or path.resolve().is_relative_to(env.ARTIFACT_DIR.resolve()):
            raise ValueError("private image vault must be mounted outside artifacts")
        try:
            values = json.loads(path.read_text())
            value = values[binding["image_env"]]
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("private image binding is unavailable in the mounted vault") from None
        return cls(binding, value)

    def inject(self, document):
        return replace_images(document, placeholder(self.binding), self.value)

    @contextmanager
    def artifact_scope(self):
        """Publish only a checked staging tree; collection still sanitizes at source."""
        destination = env.ARTIFACT_DIR
        with tempfile.TemporaryDirectory(prefix="forge-private-evidence-") as temporary:
            staging = Path(temporary)
            try:
                with env.TempArtifactDir(staging):
                    yield
            finally:
                # No publication on a leak, including failed/interrupted workload
                # captures. Never report a matched value or a possibly sensitive path.
                tokens = [token.encode() for token in self.tokens]
                for path in staging.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("Private evidence staging contains a symlink")
                    raw = path.read_bytes() if path.is_file() else b""
                    name = str(path.relative_to(staging)).encode()
                    if any(token in raw or token in name for token in tokens):
                        raise ValueError(
                            "Private image identifier remains in evidence; publication blocked"
                        )
                shutil.copytree(staging, destination, dirs_exist_ok=True)

    def text(self, text):
        for token in sorted(self.tokens, key=len, reverse=True):
            text = text.replace(token, "[private-image]")
        return text

    def assert_public(self, value):
        if any(token in json.dumps(value) for token in self.tokens):
            raise ValueError("private image identifier remains in public artifact")

    def capture(self, document):
        result = copy.deepcopy(document)

        def discover(value):
            if isinstance(value, dict):
                for status in value.get("containerStatuses", []):
                    if status.get("name") not in SERVERS:
                        continue
                    image_id = status.get("imageID", "")
                    match = re.fullmatch(r"(?:\S+@)?(sha256:[a-f0-9]{64})", image_id)
                    if match:
                        self.digests.add(match[1])
                        self.tokens.update((image_id, match[1], match[1].removeprefix("sha256:")))
                    elif status.get("ready"):
                        raise ValueError("ready private serving image digest is missing")
                    status["imageID"] = "private"
                    if "image" in status:
                        status["image"] = placeholder(self.binding)
                for container in value.get("containers", []):
                    if container.get("name") in SERVERS:
                        if container.get("image") != self.value:
                            raise ValueError(
                                "observed private serving image differs from submission"
                            )
                        container["image"] = placeholder(self.binding)
                for child in value.values():
                    discover(child)
            elif isinstance(value, list):
                for child in value:
                    discover(child)

        discover(result)
        if len(self.digests) > 1:
            raise ValueError("heterogeneous private serving digests are unsupported")

        def scrub(value):
            if isinstance(value, dict):
                return {self.text(k): scrub(v) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            return self.text(value) if isinstance(value, str) else value

        result = scrub(result)
        self.assert_public(result)
        return result
