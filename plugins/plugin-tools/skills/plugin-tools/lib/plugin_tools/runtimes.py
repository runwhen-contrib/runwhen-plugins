"""runtime.yaml: describes one runtime image version (`runtimes/{name}/runtime.yaml`, tag
`runtimes/{name}/v{version}`).

Ported from the registry's `runtimes.py`. Digests are never rewritten by a mirror rule.
"""

import re
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)
from runwhen_capability.contracts import load_yaml

from plugin_tools.semver import parse_semver

MAX_RUNTIME_YAML_BYTES = 64 * 1024
DIGEST_KEYS = frozenset({"index", "linux/amd64", "linux/arm64", "linux/arm/v7"})

_NAME = r"^[a-z][a-z0-9-]{0,62}$"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64
# A repository reference: optional registry host[:port], then path components. No scheme, tag
# or digest: a `:` is only allowed in the host's port, and `@` never.
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_HOST = r"[a-zA-Z0-9]+(?:[-.][a-zA-Z0-9]+)*(?::[0-9]+)?"
IMAGE_REFERENCE = re.compile(rf"^(?:{_HOST}/)?{_COMPONENT}(?:/{_COMPONENT})*$")


class RuntimeInvalid(Exception):
    """A runtime.yaml is not acceptable; `detail` says why, without values."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class RuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[StrictStr, Field(pattern=_NAME)]
    version: StrictStr
    image: StrictStr
    digests: dict[StrictStr, StrictStr]
    tools: dict[StrictStr, StrictStr]
    description: StrictStr | None = None

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        parse_semver(v)
        return v

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        if not IMAGE_REFERENCE.fullmatch(v):
            raise ValueError("must be a repository reference without scheme, tag or digest")
        return v

    @model_validator(mode="after")
    def _digests(self) -> "RuntimeSpec":
        if not self.digests:
            raise ValueError("digests must not be empty")
        for key, value in self.digests.items():
            if key not in DIGEST_KEYS:
                raise ValueError(f"digests key must be one of {sorted(DIGEST_KEYS)}")
            if not _DIGEST.fullmatch(value):
                raise ValueError("digests values must be sha256:<64 lowercase hex>")
            if value == PLACEHOLDER_DIGEST:
                # A stand-in from an example file would be served as if it were pullable.
                raise ValueError(
                    f"{self.name}@{self.major}: digests.{key} is the all-zero placeholder;"
                    " pin a real digest"
                )
        return self

    @property
    def major(self) -> int:
        return parse_semver(self.version)[0]


def parse_runtime_yaml(data: bytes, expected_name: str, expected_version: str) -> RuntimeSpec:
    """Parse one runtime.yaml; raises RuntimeInvalid. The name and version must equal the tag's."""
    if len(data) > MAX_RUNTIME_YAML_BYTES:
        raise RuntimeInvalid(f"runtime.yaml is over {MAX_RUNTIME_YAML_BYTES} bytes")
    try:
        doc = load_yaml(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise RuntimeInvalid("runtime.yaml is not valid UTF-8") from None
    except RecursionError:
        raise RuntimeInvalid("runtime.yaml is nested too deeply") from None
    except Exception:  # noqa: BLE001 - the parser's own errors, whatever their type
        raise RuntimeInvalid("runtime.yaml is not valid YAML") from None
    if not isinstance(doc, dict):
        raise RuntimeInvalid("runtime.yaml must be a mapping")
    try:
        spec = RuntimeSpec.model_validate(doc)
    except ValidationError as exc:
        # Location and message only: pydantic would otherwise echo the offending values.
        problems = "; ".join(
            f"{'.'.join(str(x) for x in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
        raise RuntimeInvalid(f"runtime.yaml is invalid: {problems}"[:500]) from None
    if spec.name != expected_name:
        raise RuntimeInvalid(f"runtime.yaml name {spec.name!r} does not match the tag's name")
    if spec.version != expected_version:
        raise RuntimeInvalid(
            f"runtime.yaml version {spec.version!r} does not match the tag's version"
            f" {expected_version!r}"
        )
    return spec
