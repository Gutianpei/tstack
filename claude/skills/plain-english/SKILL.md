---
name: plain-english
description: Make a piece of writing easy to read without removing technical detail. The checkable procedure behind "simplify the English, never the content". Use on documentation, a memory journal entry, a project note, a pull request description, a chat message, or any reply the reader found hard to read.
disable-model-invocation: true
---

<!-- tstack skill -->
# plain-english

The always-on rule (`~/.claude/rules/plain-english.md`) sets the standard: **simplify the English, never
the content.** This skill is the procedure behind it. Use it on a draft that is already written.

Sources for the standard: ISO 24495-1 (reader-first structure), ASD-STE100 Simplified Technical English
(sentence structure) and the Microsoft Writing Style Guide (word choice). The rules borrow their
discipline without limiting technical vocabulary.

## The test

**Friction** is anywhere the reader must pause: to look up a word, decode a metaphor, re-read a sentence
to find its subject, or guess what an internal tool does. Friction is the defect. **Technical depth is
not a defect.** The reader is a technical professional who reads agent messages all day. They pause on
filler, invented labels and figures of speech, not on established terms of their field.

For every sentence ask: would the reader pause here?

## Four passes, in this order

Refining the words of a sentence that should be deleted wastes effort, so keep the order.

1. **Structure.** Conclusion first: the direct answer on line 1, support after. One idea per sentence,
   under 25 words. Break noun strings longer than three words ("input cache invalidation callback"
   becomes "a callback that invalidates the input cache"). Prefer the active voice and name who acted.
   *Done when* each sentence carries one thought and the first line answers the question.
2. **Words.** Replace words that cause friction using the register below. Use the reader's vocabulary
   or standard domain terms; never swap a familiar term for a fancier one. Write "for example", not
   "e.g."; "that is", not "i.e."; name the rest or drop "etc.".
   *Done when* every unusual word is standard jargon or a deliberate technical term.
3. **Metaphors and idioms.** Replace invented figures of speech and phrasal verbs that have a plain
   equivalent with the literal action. Keep an analogy only if the reader introduced it or it is the
   standard explanation. *Done when* the text can be read directly, with no interpreting.
4. **Names.** Keep a label only if it is an established domain term, a name from the code, or a name the
   reader already uses. Otherwise describe the item: "the 47 GB of duplicate files", not "the easy
   bucket". Never define a label once and use it as shorthand paragraphs later.
   *Done when* every label is shared vocabulary or a literal description.

## Two checks after the passes (both required)

**Content check.** Compare the revision with the draft. Every number, path, identifier, hedge and
caveat must still be there. If simplifying an idea threatens precision, keep the detail and explain the
idea in one added sentence. *Done when* everything removed was redundant words, not facts.

**Background check.** Background is content. Sort each proper noun and concept:

1. *Field jargon*: use it directly.
2. *Custom tooling* (a script, hook, skill, service, check or convention): the reader has not read its
   code. Add a clause saying what it does and where it lives, for example "`clean-scratch`, the cleanup
   helper at `~/.local/bin/clean-scratch`". A bare tool name forces a lookup the reader cannot do.
3. *Concepts from outside research or specialized libraries*: check the user's memory notes to see if
   they have met the concept. If not, explain it from first principles.

For any custom tool or specialized concept the text relies on, give the context before the mechanics:
where it came from, what problem it solves, and one concrete example with real values.
*Done when* every name is jargon, carries a clause, or has an example.

## Register: words to swap

Add a row whenever the reader asks what a term means or says a sentence was hard to parse.

| Not this | This |
|---|---|
| an invented label reused later ("bucket 1") | repeat the literal description |
| a coined metaphor ("blind delete", "symlink forest") | the literal action: "deleting before checking which paths the process reads" |
| fragments and arrows (`A -> B fails`, `x2`) | a complete sentence |
| stratified | split by |
| falsifiable | testable |
| corroborate | back up |
| contradiction | disagreement |
| ratchet | the behavior: "it catches nothing now, but it will catch the first one written" |
| inferential claim | a claim that goes beyond what we measured |
| orthogonal | unrelated, or independent |
| canonical | the primary version that other copies come from |
| deterministic | always gives the same answer |
| heuristic | rule of thumb |
| idempotent | safe to run more than once |
| provenance | where the data or code came from |
| in anger | in production, on real workloads |
| load-bearing | critical, or a dependency other parts need |

Keep your own field's jargon: list it here so it is never "simplified".

## What this does not mean

- **Not shorter.** Plain words and full explanations are fine. Cut filler, recaps and sidebars, keep
  explanations.
- **Not less precise.** Numbers and measurements stay exact.
- **Not fewer numbers.** Technical readers take in numbers quickly.
- **Not warmer.** No softening openers or pleasantries; give the answer.

## Where to use it

- Every chat reply (the always-on rule covers this).
- Project notes, reports and documentation: revise them directly.
- Memory node summaries (`AGENT.md`): they are re-read at every session start.
- Pull request descriptions and commit messages.
- Memory journals (`journal.md`), with one override: if clarity costs any detail, keep the detail and
  leave the sentence clumsy. A journal lets a later session rebuild what happened; a dropped detail
  cannot be recovered.

## Retro-applying it to old files

Order files by how often they are re-read: memory `AGENT.md` files first, then active project notes and
docs, then journals. In an append-only journal, preserve every fact and do the rewrite in its own commit
with a clean diff.
