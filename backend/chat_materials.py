"""Read-only, grade-isolated chat references; no extra model or network call."""
from functools import lru_cache
import json
from pathlib import Path
import re

DATA = Path(__file__).resolve().parent / "data/english"


@lru_cache(maxsize=7)
def _pack(grade):
    if type(grade) is not int or grade not in {3, 4, 5, 6, 7, 8, 9}:
        raise ValueError("Invalid grade")
    pack = json.loads((DATA / f"grade_{grade}.json").read_text())
    if pack["version"] != 1 or pack["grade"] != grade:
        raise ValueError("Invalid grade resource pack")
    return pack


def _tokens(text):
    return set(re.findall(r"[a-z]+", text.lower())) - {
        "i", "you", "me", "my", "the", "a", "an", "and", "or", "is", "are", "to", "do", "what", "like",
        "would", "your", "it", "in", "of", "with", "how", "can", "unit", "starter", "about", "have", "that",
    }


def material_context(grade, messages=(), *, compact=False):
    pack = _pack(grade)
    users = [m.content for m in messages if m.role == "user"]
    latest = users[-1] if users else ""
    previous = " ".join(users[-3:-1])
    latest_tokens = _tokens(latest)

    def score(topic, text):
        words = topic["vocabulary"]
        document = topic["unit"] + " " + topic["question"] + " " + topic["example"] + " " + " ".join(w["word"] for w in words)
        english = len(_tokens(text) & _tokens(document))
        chinese = sum(bool(w["meaning"] and w["meaning"] in text) for w in words)
        # Small topical aliases connect normal student chat to collected
        # unit labels, without expanding this into a retrieval service.
        aliases = {"basketball": "sport tennis fit game", "足球": "sport fit game", "画画": "art drawing club",
                   "篮球": "sport tennis game", "生日": "birthday", "学校": "school", "朋友": "friend",
                   "压力": "relax healthy communicate", "宠物": "animal", "考试": "learning school"}
        for token, expansion in aliases.items():
            if token in text.lower():
                english += len(_tokens(expansion) & _tokens(document))
        return english + 2 * chinese

    ranked = sorted(enumerate(pack["topics"]), key=lambda pair: (-score(pair[1], latest), pair[0]))
    if not any(score(t, latest) for _, t in ranked) and previous:
        ranked = sorted(enumerate(pack["topics"]), key=lambda pair: (-score(pair[1], previous), pair[0]))
    scoring_text = latest
    if not any(score(t, latest) for _, t in ranked) and previous:
        scoring_text = previous
    chosen = [topic for _, topic in ranked if score(topic, scoring_text) > 0][:2]
    matched = []
    for word in pack["legacy_vocabulary"]:
        if word["word"].lower() in latest_tokens or (word["meaning"] and word["meaning"] in latest):
            if word["word"] not in {w["word"] for w in matched}:
                matched.append({"word": word["word"], "meaning": word["meaning"]})
        if len(matched) == 6:
            break
    # Full source provenance remains on disk. Only two small relevant topics
    # and at most six lookup entries go to the existing chat provider.
    context = {"grade": grade, "packVersion": pack["version"], "topics": chosen,
               "legacySupplementalWords": matched}
    if compact:
        # Provenance and complete packs remain unchanged on disk. Project only
        # model-relevant fields; never truncate user history or definitions.
        context["topics"] = [{"unit": topic["unit"],
                              "words": [[w["word"], w["meaning"]] for w in topic["vocabulary"]],
                              "example": topic["example"]} for topic in chosen]
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))
