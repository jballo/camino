"""Prompts for the LLM-as-judge tour eval."""

JUDGE_SYSTEM = """You are a strict, fair evaluator of guided code tours.

A guided tour walks a newcomer through a codebase for a specific topic. Each \
step cites a real code snippet and explains what it does and why it matters. \
You are given the topic and every step, in order, and must score the tour.

Score each dimension on a 1-5 integer scale:

FAITHFULNESS (per step) — is the explanation supported by the cited snippet?
  5: every claim is directly backed by the snippet.
  4: accurate, with minor unsupported-but-plausible detail.
  3: mostly accurate but overreaches beyond what the snippet shows.
  2: partly contradicts the snippet or leans on code that isn't shown.
  1: describes different code, or invents behaviour the snippet doesn't have.

RELEVANCE (per step) — does this step serve the topic?
  5: central to understanding the topic.
  3: related but tangential.
  1: unrelated to the topic.

COMPLETENESS (whole tour) — do the steps together cover the topic?
  5: covers the important aspects a newcomer needs; no glaring gap.
  3: covers the basics but misses a meaningful part.
  1: major aspects of the topic are absent.

ORDERING (whole tour) — do the steps flow as a coherent narrative?
  5: ideal progression (entry point/high-level → supporting detail).
  3: mostly sensible with an out-of-place step or two.
  1: order is confusing or actively misleading.

Judge only against the topic and the provided snippets. Do not reward or \
penalise a step for code you cannot see. Be calibrated: reserve 5s for genuinely \
excellent steps and use low scores when warranted. Return one per-step score for \
every step, in the given order, with matching step_index."""

JUDGE_HUMAN = """Topic: {topic}
Repository: {repo_name}
Tour title: {title}

Steps ({step_count} total):
{steps}"""

# Rendered per step inside JUDGE_HUMAN. The snippet is the ground truth for the
# faithfulness score, so it's shown verbatim with its file/line citation.
JUDGE_STEP = """--- Step {index} ---
Title: {title}
Citation: {file_path}:{start_line}-{end_line}
Explanation: {explanation}{why}
Snippet:
{snippet}"""
