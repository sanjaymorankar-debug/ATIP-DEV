"""
Strategy parameters (SE-02).

Every tunable number a strategy uses is declared once in its definition as a
ParamSpec and referenced from rules as {"param": "name"} -- never written into
the rule itself. A parameter set is validated against the specs (type, bounds,
allowed values) before anything runs, and the resolved set is what a decision
or backtest records.

    {"name": "rsi_entry", "type": "float", "default": 30, "min": 5, "max": 50,
     "required": false, "description": "RSI below which a dip qualifies"}

required: a required parameter has no usable default -- every run must supply
it (resolve() refuses to run without it). Optional parameters always have a
default.

Types: int, float, bool, str, choice (allowed required), int_list.
Parameters belong to a strategy VERSION: changing a default or a bound is a
definition change, and so a new version.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

TYPES = ("int", "float", "bool", "str", "choice", "int_list")


class ParamError(ValueError):
    pass


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str
    default: object = None
    min: float | None = None
    max: float | None = None
    allowed: tuple | None = None
    description: str = ""
    required: bool = False

    def __post_init__(self):
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ParamError(f"bad parameter name {self.name!r}")
        if self.type not in TYPES:
            raise ParamError(f"{self.name}: type must be one of {TYPES}")
        if self.type == "choice" and not self.allowed:
            raise ParamError(f"{self.name}: a choice parameter needs 'allowed'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ParamError(f"{self.name}: min {self.min} > max {self.max}")
        if self.default is not None:
            self.coerce(self.default)          # the default must itself be valid
        elif not self.required:
            raise ParamError(f"{self.name}: an optional parameter needs a default (or mark it required)")

    def coerce(self, v):
        """The value as this parameter's type, or ParamError."""
        n = self.name
        if v is None:
            raise ParamError(f"{n}: a value is required")
        try:
            if self.type == "int":
                if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
                    raise ValueError
                v = int(v)
            elif self.type == "float":
                if isinstance(v, bool):
                    raise ValueError
                v = float(v)
            elif self.type == "bool":
                if not isinstance(v, bool):
                    raise ValueError
            elif self.type in ("str", "choice"):
                v = str(v)
            elif self.type == "int_list":
                v = [int(x) for x in v]
        except (TypeError, ValueError):
            raise ParamError(f"{n}: {v!r} is not a valid {self.type}")
        if self.type in ("int", "float"):
            if self.min is not None and v < self.min:
                raise ParamError(f"{n}: {v} is below the minimum {self.min}")
            if self.max is not None and v > self.max:
                raise ParamError(f"{n}: {v} is above the maximum {self.max}")
        if self.allowed is not None and v not in self.allowed:
            raise ParamError(f"{n}: {v!r} is not one of {list(self.allowed)}")
        return v

    def as_dict(self) -> dict:
        d = asdict(self)
        d["allowed"] = list(self.allowed) if self.allowed is not None else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ParamSpec":
        unknown = set(d) - {"name", "type", "default", "min", "max", "allowed", "description", "required"}
        if unknown:
            raise ParamError(f"parameter {d.get('name')!r}: unknown keys {sorted(unknown)}")
        return cls(name=d.get("name"), type=d.get("type"), default=d.get("default"), min=d.get("min"),
                   max=d.get("max"), allowed=tuple(d["allowed"]) if d.get("allowed") is not None else None,
                   description=d.get("description", ""), required=bool(d.get("required", False)))


def resolve(specs: list, overrides: dict | None = None) -> dict:
    """Defaults overlaid with overrides, every value validated; unknown names
    raise, and so does a required parameter that was not supplied."""
    by_name = {s.name: s for s in specs}
    overrides = overrides or {}
    unknown = set(overrides) - set(by_name)
    if unknown:
        raise ParamError(f"unknown parameters {sorted(unknown)}; declared: {sorted(by_name)}")
    out = {}
    for s in specs:
        if s.name not in overrides and s.default is None:
            raise ParamError(f"{s.name} is required and was not supplied")
        v = overrides.get(s.name, s.default)
        out[s.name] = s.coerce(v)
    return out


def value(x, params: dict):
    """A literal, or {"param": name} looked up in the resolved params."""
    if isinstance(x, dict) and set(x) == {"param"}:
        if x["param"] not in params:
            raise ParamError(f"rule refers to undeclared parameter {x['param']!r}")
        return params[x["param"]]
    return x


def referenced(obj) -> set:
    """Every parameter name referenced anywhere inside a definition fragment."""
    out = set()
    if isinstance(obj, dict):
        if set(obj) == {"param"}:
            out.add(obj["param"])
        else:
            for v in obj.values():
                out |= referenced(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= referenced(v)
    return out
