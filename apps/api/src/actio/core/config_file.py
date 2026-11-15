"""Reading one of the TOML files in `config/`, and saying what is wrong with it.

A file there is edited by whoever runs the installation, so a mistake in it has to be
told in one line that names the file and the fault: `read` turns every way of failing
into the caller's own error, with such a line as its text.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, StringConstraints, ValidationError

type Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]
"""What something is called in a file, such as a model or a plan: short, lower-case,
safe in a URL."""


def read[File: BaseModel, Failure: Exception](
    path: Path, form: type[File], *, what: str, elsewhere: str, failure: type[Failure]
) -> File:
    """Read the file at `path` and check it against `form`.

    `what` names the file in a message, such as `models`. `elsewhere` says what to do
    when it isn't there. Whatever is wrong is raised as a `failure`.
    """
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise failure(f"the {what} file {str(path)!r} doesn't exist. {elsewhere}") from None
    except OSError as error:
        raise failure(f"can't read the {what} file {str(path)!r}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise failure(f"{path} isn't valid TOML: {error}") from error
    try:
        return form.model_validate(data)
    except ValidationError as error:
        raise failure(f"{path}: {'; '.join(map(_in_words, error.errors()))}") from None


def _in_words(fault: Mapping[str, Any]) -> str:
    """One fault as `models.big.context_window: what is wrong with it`. A fault of the
    file as a whole, such as a default that isn't a model, names its place itself."""
    where = ".".join(str(part) for part in fault["loc"])
    what = str(fault["msg"]).removeprefix("Value error, ")
    return f"{where}: {what}" if where else what
