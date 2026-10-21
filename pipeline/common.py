"""Pieces shared by the config schemas: the strict base model, common number types, YAML loading."""

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

Rpe = Annotated[float, Field(ge=1, le=10)]
Fraction = Annotated[float, Field(gt=0, le=1)]


class StrictModel(BaseModel):
    """Unknown keys are errors: a typo in a config file must not pass silently."""

    model_config = ConfigDict(extra="forbid")


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys (PyYAML keeps the last one silently)."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_yaml(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=_UniqueKeyLoader)
