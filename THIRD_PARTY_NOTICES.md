# Third-party notices

Actio's own code is [MIT-licensed](LICENSE). Its dependencies are declared in the `pyproject.toml` files and `uv.lock` (Python), and in the `package.json` files and `pnpm-lock.yaml` (JavaScript). They are installed from the package registries: none of their code is copied into this repository.

This file records what Actio's two container images ship. The API image was checked on 2026-11-11 and the web image on 2026-11-14, from the installed packages' own metadata and from inside the built images.

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

**The API image** (Python, 38 packages besides Actio's own two): MIT, BSD-3-Clause, Apache-2.0, ISC and PSF-2.0 licenses, and one public-domain dedication.

| License | Packages |
|---|---|
| MIT | alembic, annotated-doc, annotated-types, anyio, argon2-cffi, argon2-cffi-bindings, fastapi, h11, httptools, mako, pydantic, pydantic-core, pydantic-settings, pyyaml, sqlalchemy, truststore, typing-inspection, valkey, watchfiles |
| MIT-0 | cffi |
| MIT and PSF-2.0 | greenlet |
| MIT or Apache-2.0 | uvloop |
| BSD-3-Clause | click, httpcore2, httpx2, idna, markupsafe, pycparser, python-dotenv, starlette, uvicorn, websockets |
| Apache-2.0 | opentelemetry-api |
| ISC | dnspython |
| PSF-2.0 | typing-extensions |
| The Unlicense (public domain) | email-validator |

**The web image** (JavaScript, production dependencies only) ships its server's packages, and the scripts it serves to browsers, which are built from the same dependencies. Those are about 330 packages, most of them brought in by what shows answers as markdown ([ADR 0016](docs/adr/0016-answers-shown-as-markdown.md)):

| License | Packages |
|---|---|
| MIT | 260, including Next.js, React, Shiki, KaTeX and Mermaid |
| ISC | 41, most of them D3's |
| Apache-2.0 | 19, including the AI SDK (`ai`, `@ai-sdk/react`) and Streamdown |
| BSD-3-Clause | 7 |
| BSD-2-Clause | `entities` |
| 0BSD | `tslib` |
| The Unlicense (public domain) | `robust-predicates` |

- `dompurify`, which Mermaid cleans its diagrams with, is offered under MPL-2.0 or Apache-2.0, and is used under Apache-2.0.
- `json-schema` is offered under AFL-2.1 or BSD-3-Clause, and is used under BSD-3-Clause.
- `caniuse-lite`, the browser-support data used at build time, is under CC-BY-4.0.

## Not covered here

- **Development tools,** such as the test and lint tools, which no image ships. Among them is Playwright (Apache-2.0), which downloads a Chromium browser to the machine that runs the end-to-end tests.
- **The fake model's image,** which only the end-to-end tests build and run (`packages/fakellm/Dockerfile`). Its packages are a subset of the API image's.
- **Models.** Model weights are not part of this repository or of its images. You download them yourself, with Ollama, under each model's own license.
