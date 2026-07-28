---
name: ste-writing
description: Write prose in ASD-STE100 Simplified Technical English - docstrings, code comments, console/warning/error strings, exception messages, commit messages, PR and issue text, README and design docs. Never code, identifiers or command syntax. Always on in this project, imported by CLAUDE.md.
---

# Prose style - ASD-STE100 Simplified Technical English

Adapted from [ste-writing](https://github.com/woosal1337/blog/blob/main/videos/ep01-the-cure-for-ai-slop/ste-writing-skill.md).

Applies to docstrings and code comments, `console()` / `warning()` / `error()` strings,
exception messages, commit messages, PR and issue text, README and design docs. It does
**not** apply to code, identifiers, or command syntax. It is not for marketing copy or
anything that needs a voice. STE strips voice on purpose.

Use **strict** mode for exception and log strings, comments and commit messages: apply every
rule and both length caps. Use **STE-flavored** mode for docs, PR text and chat prose: keep
the sentence, paragraph and active-voice discipline, but keep enough vocabulary to read
naturally.

## Rules

**Words**
- One name for one thing. Do not call a `BuildDependency` "the dep" in one line and "the target" in the next.
- Use the short common word: start (not initiate), use (not utilize), help (not facilitate),
  make sure (not ensure), before (not prior to), after (not subsequent to), about (not regarding),
  get (not obtain), show (not demonstrate), also (not additionally/furthermore).
- One meaning per word. "fall" means to move down, not to decrease.
- No marketing adjectives: seamless, robust, powerful, cutting-edge, effortless, world-class.
- American spelling.

**Verbs**
- Active voice. "the parser reads the mamafile", not "the mamafile is read by the parser".
- Use a verb for an action. "analyze the log", not "perform an analysis of the log".
- No stacked auxiliaries. Not "it is important to note that this may help to improve". Write "this improves X".
- No "-ing" main verb where a simple tense works.

**Sentences**
- One instruction per sentence. Max 20 words for an instruction, max 25 for description.
- No contractions. Use articles: a, an, the, this, these.

**Punctuation**
- No semicolons. Write two sentences. No em-dashes either, per the code style rules in CLAUDE.md.

**Structure**
- One topic per paragraph, max six sentences.
- For steps, use a numbered list, one action per item, imperative form.
- Put a condition before its command: "If the fetch returns 404, keep the cached status."

Write only the requested text. No preamble, no summary, no closing remarks.

## Self-lint before you send or commit text

1. Any sentence over 20 words? Split it.
2. Any semicolon? Replace it with a period.
3. Any contraction? Expand it.
4. Any passive voice with a known actor? Make it active.
5. Any "-ing" main verb, nominalization, or phrasal verb ("spin up")? Use a plain verb.
6. The same thing named two ways? Pick one name.

These rules fix the FORM of weak text. They cannot make a hollow paragraph true.

Free official standard (do not paste it in full, it is copyrighted): https://asd-ste100.org
