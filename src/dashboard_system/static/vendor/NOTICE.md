KaTeX 0.16.21 browser renderer, stylesheet and fonts extracted from the installed Visual Studio Code markdown-math extension.

Upstream: https://github.com/KaTeX/KaTeX (MIT; see LICENSE-katex.txt).

Only the embedded KaTeX module and its bundler initialization wrapper were retained from notebook-out/katex.js. The extension integration and Markdown plugin were removed, and `export default rn();` exposes the renderer as an ES module. The embedded renderer, stylesheet and fonts are unchanged. No network is used during rendering.
