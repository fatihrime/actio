# Third-party notices

Actio's own code is [MIT-licensed](LICENSE). Its dependencies are declared in the `pyproject.toml` files and `uv.lock` (Python), and in the `package.json` files and `pnpm-lock.yaml` (JavaScript). They are installed from the package registries: none of their code is copied into this repository.

This file records what Actio's two container images ship. It was checked on 2026-11-10, from the installed packages' own metadata and from inside the built images.

## Components under the LGPL

Actio allows LGPL libraries that are used unmodified, as separate libraries ([ADR 0008](docs/adr/0008-postgres-through-sqlalchemy-and-psycopg.md)). There are two:

| Component | License | Shipped in | What it does | Source |
|---|---|---|---|---|
| psycopg and psycopg-binary 3.3 | LGPL-3.0-only | The API image | The Postgres driver | <https://github.com/psycopg/psycopg> |
| libvips 8.18, as `@img/sharp-libvips-*` | LGPL-3.0-or-later | The web image | Image processing for Next.js's image optimizer, through `sharp` | <https://github.com/libvips/libvips> |

- Both are used exactly as their projects publish them, and Actio links to neither statically.
- You can replace either with a build of your own, by installing it over the one in the image: psycopg into the API image's virtual environment (`/app/.venv`), libvips into the web image's `node_modules`.
- The license text is at <https://www.gnu.org/licenses/lgpl-3.0.html>. Each package also carries its own copy.

`psycopg-binary` bundles the libraries it needs to talk to Postgres, among them libpq (PostgreSQL License) and OpenSSL (Apache-2.0).

## Everything else the images ship

**The API image** (Python): MIT, BSD-3-Clause, Apache-2.0, ISC and PSF-2.0 licenses, and one public-domain dedication.

| License | Packages |
|---|---|
| MIT | alembic, annotated-doc, annotated-types, anyio, argon2-cffi, argon2-cffi-bindings, fastapi, h11, httptools, mako, pydantic, pydantic-core, pydantic-settings, pyyaml, sqlalchemy, typing-inspection, valkey, watchfiles |
| MIT-0 | cffi |
| MIT and PSF-2.0 | greenlet |
| MIT or Apache-2.0 | uvloop |
| BSD-3-Clause | click, idna, markupsafe, pycparser, python-dotenv, starlette, uvicorn, websockets |
| Apache-2.0 | opentelemetry-api, tzdata |
| ISC | dnspython |
| PSF-2.0 | typing-extensions |
| The Unlicense (public domain) | email-validator |

**The web image** (JavaScript, production dependencies only): 45 packages under MIT, including Next.js and React, and the rest under Apache-2.0, ISC, BSD-3-Clause and 0BSD. `caniuse-lite`, the browser-support data used at build time, is under CC-BY-4.0.

## Not covered here

- **Development tools,** such as the test and lint tools, which no image ships.
- **Models.** Model weights are not part of this repository or of its images. You download them yourself, with Ollama, under each model's own license.
