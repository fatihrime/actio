"""What every model of the API is, one it takes or one it answers with: `Described`.

A member of such a model says what it is in the string right under it:

    credits: int | None
    \"\"\"What the turn cost you, in credits: see `GET /api/usage`.\"\"\"

That string is the member's description in the API's own description of itself
(`/api/openapi.json`, `/api/docs`) and, through it, in the types the web app is
generated from. Left to the framework's default it would be read by whoever reads this
code, and by nobody who uses the API. So write it for a reader of the API: what the
member means, not how it is made.

A model defined in `actio.api` is a `Described`, or a test fails. `Body`, which every
request's body is, is one.
"""

from pydantic import BaseModel, ConfigDict


class Described(BaseModel):
    """A model whose members' docstrings are their descriptions in the API's
    description. A model that sets a `model_config` of its own keeps this setting:
    the two are merged."""

    model_config = ConfigDict(use_attribute_docstrings=True)
