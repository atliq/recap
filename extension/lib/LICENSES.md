# Third-party libraries bundled with the RECAP extension

These libraries are vendored locally (never loaded from a CDN) so the extension
works fully offline and complies with MV3's content-security policy. Each is used
unmodified; their original licenses apply.

## Readability.js
- **Project:** Mozilla Readability (`@mozilla/readability`)
- **Source:** https://github.com/mozilla/readability
- **License:** Apache License 2.0
- **Use in RECAP:** primary main-content extraction in `content.js`.

## 3d-force-graph.js
- **Project:** 3d-force-graph by Vasco Asturiano
- **Source:** https://github.com/vasturiano/3d-force-graph
- **License:** MIT
- **Use in RECAP:** knowledge-graph visualization in `graph.html` (bundles
  three.js - MIT - and d3-force-3d - MIT).

The full Apache License 2.0 text is bundled here as [APACHE-2.0.txt](APACHE-2.0.txt)
(required by its §4a for redistribution).

## MIT license notice (3d-force-graph, three.js, d3-force-3d)

> MIT License
>
> Copyright (c) 2017 Vasco Asturiano
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
> THE SOFTWARE.
