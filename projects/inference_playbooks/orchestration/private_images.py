"""Hub-vault image injection; never persist the resolved image in artifacts."""

import copy
import json
import re
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
    r"^[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[a-f0-9]{64})?$"
)
SERVERS = {"vllm", "vllm-leader", "vllm-worker", "main"}


def public_resource(document):
    """Construct approved runtime facts; submitted bundles own desired configuration."""
    if "items" in document:
        return {"kind": "List", "items": [public_resource(item) for item in document["items"]]}
    result = {"kind": document["kind"]}
    if "apiVersion" in document:
        result["apiVersion"] = document["apiVersion"]
    if "name" in document.get("metadata", {}):
        result["metadata"] = {"name": document["metadata"]["name"]}
    if document["kind"] != "Pod":
        # Raw controller objects carry admission annotations, conditions and API
        # messages. The verified public submission already records their spec.
        return result
    spec = document["spec"]
    containers = [
        {
            key: copy.deepcopy(container[key])
            for key in (
                "name",
                "image",
                "command",
                "args",
                "env",
                "resources",
                "volumeMounts",
                "securityContext",
            )
            if key in container
        }
        for container in spec["containers"]
        if container.get("name") in SERVERS
    ]
    names = {container["name"] for container in containers}
    result["spec"] = {
        "containers": containers,
        **{
            key: copy.deepcopy(spec[key])
            for key in ("volumes", "securityContext", "serviceAccountName")
            if key in spec
        },
    }
    result["status"] = {
        "containerStatuses": [
            {
                key: copy.deepcopy(status[key])
                for key in ("name", "ready", "imageID")
                if key in status
            }
            for status in document.get("status", {}).get("containerStatuses", [])
            if status.get("name") in names
        ]
    }
    return result


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
                repository.split("/")[0].split(":")[0],
                repository.split("/", 1)[1],
                digest,
            )
            if v
        }
        if digest:
            self.tokens.add(digest.removeprefix("sha256:"))
        self.digests = set()
        self._staging = None
        self._exports = {}

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

    def publish(self, path):
        """Register bytes just constructed from public inputs or approved facts."""
        if self._staging is not None:
            if path.is_symlink():
                raise ValueError("Private evidence output cannot be a symlink")
            name = path.resolve().relative_to(self._staging.resolve()).as_posix()
            self._exports[name] = path.read_bytes()

    @contextmanager
    def artifact_scope(self):
        """Publish registered public records and metric projections, never raw staging."""
        destination = env.ARTIFACT_DIR
        with tempfile.TemporaryDirectory(prefix="forge-private-evidence-") as temporary:
            staging = Path(temporary)
            self._staging, self._exports = staging, {}
            try:
                with env.TempArtifactDir(staging):
                    yield
                from .public_invocation import project_invocation
                from .public_metrics import project_native

                patterns = {
                    "guidellm": "**/*__run_guidellm_benchmark/artifacts/results/benchmarks*.json",
                    "aiperf": "**/*__run_aiperf_benchmark/artifacts/aiperf/profile_export_aiperf.json",
                }
                for harness, pattern in patterns.items():
                    for path in sorted(staging.glob(pattern)):
                        if path.is_symlink() or not path.resolve().is_relative_to(
                            staging.resolve()
                        ):
                            raise ValueError("Private metric export cannot escape staging")
                        name = path.relative_to(staging).as_posix()
                        self._exports[name] = json.dumps(
                            project_native(path.read_bytes(), harness)
                        ).encode()
                    for path in sorted(
                        staging.glob(f"**/*__run_{harness}_benchmark/src/{harness}-job.yaml")
                    ):
                        if path.is_symlink() or not path.resolve().is_relative_to(
                            staging.resolve()
                        ):
                            raise ValueError("Private invocation cannot escape staging")
                        name = (
                            (path.parent.parent / "artifacts" / f"{harness}-invocation.json")
                            .relative_to(staging)
                            .as_posix()
                        )
                        self._exports[name] = json.dumps(
                            project_invocation(path.read_bytes(), harness)
                        ).encode()
            finally:
                self._staging = None
            # Failed or interrupted scopes never publish. Do not report a matched
            # value or a possibly sensitive path when checking successful output.
            tokens = [token.encode() for token in self.tokens]
            for name, raw in self._exports.items():
                if any(token in raw or token in name.encode() for token in tokens):
                    raise ValueError(
                        "Private image identifier remains in evidence; publication blocked"
                    )
            for name, raw in self._exports.items():
                path = destination / name
                if not path.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("Private publication destination escapes artifact root")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)

    def text(self, text):
        for token in sorted(self.tokens, key=len, reverse=True):
            text = text.replace(token, "[private-image]")
        return text

    def assert_public(self, value):
        if any(token in json.dumps(value) for token in self.tokens):
            raise ValueError("private image identifier remains in public artifact")

    def capture(self, document):
        result = public_resource(document)

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
                    status.pop("image", None)
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

        self.assert_public(result)
        return result
