// The mobile app can't vertically shift inline text, so <sup>/<sub> tags show
// up flat ("cm3") or, in plain-text options, as raw tags. Real Unicode
// superscript/subscript characters render correctly everywhere instead.

const SUPERSCRIPT = {
  0: "⁰", 1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹",
  "+": "⁺", "-": "⁻", "−": "⁻", "–": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
  a: "ᵃ", b: "ᵇ", c: "ᶜ", d: "ᵈ", e: "ᵉ", f: "ᶠ", g: "ᵍ", h: "ʰ", i: "ⁱ", j: "ʲ",
  k: "ᵏ", l: "ˡ", m: "ᵐ", n: "ⁿ", o: "ᵒ", p: "ᵖ", r: "ʳ", s: "ˢ", t: "ᵗ", u: "ᵘ",
  v: "ᵛ", w: "ʷ", x: "ˣ", y: "ʸ", z: "ᶻ",
  A: "ᴬ", B: "ᴮ", D: "ᴰ", E: "ᴱ", G: "ᴳ", H: "ᴴ", I: "ᴵ", J: "ᴶ", K: "ᴷ", L: "ᴸ",
  M: "ᴹ", N: "ᴺ", O: "ᴼ", P: "ᴾ", R: "ᴿ", T: "ᵀ", U: "ᵁ", V: "ⱽ", W: "ᵂ",
};

const SUBSCRIPT = {
  0: "₀", 1: "₁", 2: "₂", 3: "₃", 4: "₄", 5: "₅", 6: "₆", 7: "₇", 8: "₈", 9: "₉",
  "+": "₊", "-": "₋", "−": "₋", "–": "₋", "=": "₌", "(": "₍", ")": "₎",
  a: "ₐ", e: "ₑ", h: "ₕ", i: "ᵢ", j: "ⱼ", k: "ₖ", l: "ₗ", m: "ₘ", n: "ₙ", o: "ₒ",
  p: "ₚ", r: "ᵣ", s: "ₛ", t: "ₜ", u: "ᵤ", v: "ᵥ", x: "ₓ",
};

const decodeSigns = (text) => text.replace(/&ndash;|&minus;/g, "-");

const convert = (text, map) =>
  Array.from(decodeSigns(text), (ch) => map[ch] ?? ch).join("");

const canConvert = (text, map) =>
  Array.from(decodeSigns(text)).every((ch) => map[ch] !== undefined);

// A lone superscript "o" is how exam papers write the degree sign.
export const toSuperscript = (text) =>
  text.trim() === "o" ? "°" : convert(text, SUPERSCRIPT);

export const toSubscript = (text) => convert(text, SUBSCRIPT);

// Replaces every <sup>/<sub> in an HTML string with Unicode characters:
// - fraction-style units/numbers (<sup>m</sup>/<sub>s</sub>) become plain "m/s"
// - content with a Unicode form is converted (cm<sup>3</sup> -> cm³)
// - anything left (e.g. R<sub>f</sub>, no subscript "f" exists) is unwrapped
export const htmlScriptsToUnicode = (html) => {
  if (typeof html !== "string" || !/<su[bp]>/i.test(html)) return html;
  const TAGS = "((?:<[^>]*>|\\s)*)";
  // Fraction-style tops and bottoms (<sup>m</sup>/<sub>s</sub>) become plain text.
  const top = new RegExp(`<sup>([^<]+)</sup>(?=${TAGS}/)`, "gi");
  const bottom = new RegExp(`(/${TAGS})<sub>([^<]+)</sub>`, "gi");
  let out = html;
  // Innermost tags first, repeated so nested <sub>kg<sup>o</sup>C</sub> resolves.
  let prev;
  do {
    prev = out;
    out = out.replace(top, "$1").replace(bottom, "$1$3");
    out = out.replace(/<(sup|sub)>([^<]*)<\/\1>/gi, (_, tag, content) => {
      const [, body, trailing] = content.match(/^(.*?)((?:\s|&nbsp;)*)$/s);
      const isSup = tag.toLowerCase() === "sup";
      if (isSup && body === "o") return `°${trailing}`;
      const map = isSup ? SUPERSCRIPT : SUBSCRIPT;
      return (canConvert(body, map) ? convert(body, map) : body) + trailing;
    });
  } while (out !== prev);
  // Any tags still wrapping other markup: drop the tags, keep the content.
  return out.replace(/<\/?su[bp]>/gi, "");
};

// The app's fill-in-the-blank renderer (InlineBlankQuestion) only understands
// these tags and prints any other tag as literal text.
const APP_TAGS = new Set(["em", "strong", "br", "p"]);
const TAG_ALIASES = { i: "em", b: "strong" };

// Question text: Unicode super/subscripts, <i>/<b> renamed to <em>/<strong>,
// and any other tag (e.g. <span style=...>) unwrapped, keeping its content.
export const toAppQuestionHtml = (html) =>
  htmlScriptsToUnicode(html).replace(/<(\/?)([a-z][a-z0-9]*)\b[^>]*>/gi, (_, closing, name) => {
    const lower = name.toLowerCase();
    const tag = TAG_ALIASES[lower] || lower;
    if (!APP_TAGS.has(tag)) return "";
    return tag === "br" ? "<br>" : `<${closing}${tag}>`;
  });

// Options and answers are shown as plain <Text> in the app, so any tag or
// entity would appear literally: strip tags and decode entities.
export const toAppPlainText = (text) => {
  if (typeof text !== "string") return text;
  const stripped = htmlScriptsToUnicode(text)
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/<[^>]+>/g, "");
  const decoder = document.createElement("textarea");
  decoder.innerHTML = stripped;
  return decoder.value.replace(/ /g, " ").replace(/\s{2,}/g, " ").trim();
};
