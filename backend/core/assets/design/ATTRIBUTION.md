# Design Studio vendored assets — attribution

The Markdown assets in this directory (`design-systems/` and `craft/`) are
vendored **verbatim** from the [Open Design](https://github.com/zasonic/open-design)
project and are used by the AltoSymbioAgents "Design Studio" feature
(`services/design_assets.py`, `services/design_studio.py`).

## Open Design — Apache License 2.0

Open Design is licensed under the Apache License, Version 2.0. A copy of the
full license text is preserved alongside this file as `LICENSE-open-design.txt`.

```
Copyright (c) Open Design contributors
Licensed under the Apache License, Version 2.0 (the "License");
you may not use these files except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0
```

The Apache-2.0 license is compatible with AltoSymbioAgents' MIT license for the
purpose of redistribution; this NOTICE preserves the required attribution.

### `craft/` — additionally adapted from refero_skill (MIT)

Per the upstream `craft/README.md` and `craft/anti-ai-slop.md` headers, the
craft rules are *"adapted from the MIT-licensed
[refero_skill](https://github.com/referodesign/refero_skill) project
(© Refero Design)"*. Those in-file credits are retained unmodified.

### `design-systems/` — brand-inspired descriptions

Each `design-systems/<id>/DESIGN.md` is an *original descriptive document* that
documents a design language "inspired by" a third-party brand (e.g. Linear,
Apple, Notion). They contain design-token descriptions and prose, **not** any
brand's proprietary assets, logos, fonts, or code. Brand names remain the
trademarks of their respective owners and appear here only as nominative
references. Review before any public redistribution.

## Updating these assets

These files are a point-in-time vendor copy. To refresh, re-copy from the
upstream `open-design/{design-systems,craft}/` directories verbatim — do not
hand-edit the vendored Markdown, so the copy stays diffable against source.
