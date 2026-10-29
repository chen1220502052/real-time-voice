"""Build small, traceable grade chat packs from the collected local resources.

Print JSON to stdout; --check checks committed packs without changing files.
No textbook/audio payloads or student data are included.
"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(grade):
    source = ROOT / "英语/人教版/新版单元听说训练.json"
    rows = json.loads(source.read_text())
    if grade == 3:
        source = ROOT / "英语/人教版/三年级单元训练.json"
        rows = json.loads(source.read_text())
    selected = [r for r in rows if r["grade"] == f"{grade}年级"]
    sources = [{"path": str(source.relative_to(ROOT)), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "kind": "original_topic_supplements_not_textbook_transcripts"}]
    topics = []
    for row in selected:
        topics.append({"edition": row["edition"], "unit": row["unit"],
                       "vocabulary": row["vocabulary"], "question": row["question"],
                       "example": row["model"], "kind": row["kind"]})
    words = []
    for path in sorted((ROOT / f"英语/人教版/{grade}年级/单词").rglob("词汇.json")):
        entries = json.loads(path.read_text())
        sources.append({"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "kind": "legacy_third_party_vocabulary_not_new_edition_wordlist"})
        for entry in entries:
            words.append({"word": entry["word"], "meaning": entry["meaning"], "edition": entry["edition"]})
    if not topics or not words:
        raise ValueError("Missing grade resources")
    return {"version": 1, "grade": grade, "sources": sources, "topics": topics, "legacy_vocabulary": words}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grade", type=int, choices=[3, 4, 5, 6, 7, 8, 9])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        for grade in [3, 4, 5, 6, 7, 8, 9]:
            path = ROOT / f"backend/data/english/grade_{grade}.json"
            assert json.loads(path.read_text()) == build(grade), f"Stale pack: {path}"
            print(f"Grade {grade}: pack and source hashes verified")
    elif args.grade:
        print(json.dumps(build(args.grade), ensure_ascii=False, indent=2))
    else:
        parser.error("Supply --grade or --check")


if __name__ == "__main__":
    main()
