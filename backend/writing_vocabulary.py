"""Deterministic curriculum boundary and vocabulary audit, independent of the LLM."""
from collections import Counter
import json
from pathlib import Path
import re

DATA = Path(__file__).parent / "data/writing"
WORD = re.compile(r"[a-z]+(?:'[a-z]+)?", re.I)
IRREGULAR = {
    "children": "child", "men": "man", "women": "woman", "people": "person",
    "feet": "foot", "teeth": "tooth", "mice": "mouse", "went": "go", "gone": "go",
    "ate": "eat", "eaten": "eat", "saw": "see", "seen": "see", "took": "take",
    "taken": "take", "came": "come", "ran": "run", "bought": "buy", "brought": "bring",
    "made": "make", "got": "get", "gave": "give", "given": "give", "wrote": "write",
    "written": "write", "read": "read", "sang": "sing", "sung": "sing", "swam": "swim",
    "swum": "swim", "better": "good", "best": "good", "worse": "bad", "worst": "bad",
    "said": "say", "told": "tell", "spoke": "speak", "spoken": "speak",
    "thought": "think", "knew": "know", "known": "know", "felt": "feel",
    "left": "leave", "kept": "keep", "found": "find", "stood": "stand", "sat": "sit",
}


def tokens(text):
    text = text.lower().replace("’", "'")
    for short, expanded in {"can't": "can not", "won't": "will not", "shan't": "shall not"}.items():
        text = text.replace(short, expanded)
    text = re.sub(r"n't\b", " not", text)
    for suffix, expanded in {"re": "are", "ve": "have", "ll": "will", "m": "am", "d": "would"}.items():
        text = re.sub(r"'" + suffix + r"\b", " " + expanded, text)
    # 's can be a possessive, 'is', or 'has'; require the stem itself to be known.
    text = re.sub(r"'s\b", "", text)
    return WORD.findall(text)


def known(word, vocabulary):
    if word in vocabulary or IRREGULAR.get(word) in vocabulary:
        return True
    # Generate inflections only from known bases; no derivational suffix stripping.
    for base in vocabulary:
        forms = {base + "s", base + "es", base + "ed", base + "ing"}
        if base.endswith("e"):
            forms.update({base + "d", base[:-1] + "ing"})
        if len(base) > 2 and base.endswith("y") and base[-2] not in "aeiou":
            forms.update({base[:-1] + "ies", base[:-1] + "ied"})
        if len(base) > 2 and base[-1] not in "aeiouwxy" and base[-2] in "aeiou" and base[-3] not in "aeiou":
            forms.update({base + base[-1] + "ed", base + base[-1] + "ing"})
        if word in forms:
            return True
    return False


def lexical_tokens(headword):
    # Printed variants are learned vocabulary too, but editorial abbreviations and
    # dictionary placeholders are not. Keep raw printed headwords in provenance.
    headword = re.sub(r'\b(?:AmE|BrE|sb|sth)\b\.?', ' ', headword, flags=re.I)
    headword = re.sub(r'\b(?:pl|sing)\.', ' ', headword, flags=re.I)
    return tokens(headword)


def position(chapter):
    match = re.match(r"(?:Unit|Lesson)\s+(\d+)\b", chapter["unit"], re.I)
    if not match or not chapter["edition"].startswith(("上册", "下册")):
        raise ValueError("该章节缺少可排序的册次或单元编号")
    return int(re.search(r"\d+", chapter["grade"])[0]), int(chapter["edition"].startswith("下册")), int(match[1])


class VocabularyScope:
    def __init__(self, library, chapter_id, verified_path=None):
        self.chapter = library.chapters.get(chapter_id)
        if self.chapter is None:
            raise ValueError("请选择有效教材单元")
        current = self.chapter
        boundary = position(current)
        self.textbook_ready = False
        if current.get('curriculum') and not verified_path:
            self._from_curriculum(library, current, boundary)
            return
        family = current["edition"].split("_", 1)[-1]
        included = []
        for row in library.chapters.values():
            if row["publisher"] != current["publisher"] or row["edition"].split("_", 1)[-1] != family:
                continue
            try:
                if (3, 0, 0) <= position(row) <= boundary:
                    included.append(row)
            except ValueError:
                continue
        included.sort(key=position)
        foundation = json.loads((DATA / "foundation.json").read_text())["words"]
        entries = {}
        if verified_path:
            pack = json.loads(Path(verified_path).read_text())
            if pack.get("version") != 1 or not isinstance(pack.get("chapters"), dict):
                raise ValueError("完整词表文件格式错误")
            entries = pack["chapters"]
            foundation = pack.get("foundation", [])
        if not isinstance(foundation, list) or not all(isinstance(w, str) for w in foundation):
            raise ValueError("基础词表格式错误")
        self.words = set(tokens(" ".join(foundation)))
        self.sources = []
        self.current_words = []
        for row in included:
            entry = entries.get(row["id"])
            if entry is not None and (not isinstance(entry, dict) or not isinstance(entry.get("words"), list)
                                      or not all(isinstance(w, str) and w.strip() for w in entry["words"])
                                      or not isinstance(entry.get("source"), str) or not entry["source"].strip()):
                raise ValueError("核对词表必须包含 words 和 source")
            words = entry["words"] if entry else [w["word"] for w in row["vocabulary"]]
            self.words.update(tokens(" ".join(words)))
            if row["id"] == chapter_id:
                self.current_words = words
            self.sources.append({"chapterId": row["id"], "grade": row["grade"], "edition": row["edition"],
                                 "unit": row["unit"], "verified": entry is not None,
                                 "source": entry["source"] if entry else row["textKind"]})
        self.textbook_ready = bool(verified_path) and all(s['verified'] for s in self.sources)
        # Missing earlier semesters must never be silently declared complete.
        expected = {(g, t) for g in range(min(4, boundary[0]), boundary[0] + 1) for t in (0, 1) if (g, t) <= boundary[:2]}
        present = {position(r)[:2] for r in included}
        self.missing_terms = [f"{g}年级{'下册' if t else '上册'}" for g, t in sorted(expected - present)]
        self.missing_units = []
        for grade, term in sorted(present):
            numbers = {position(r)[2] for r in included if position(r)[:2] == (grade, term)}
            last = boundary[2] if (grade, term) == boundary[:2] else max(numbers)
            self.missing_units.extend(f"{grade}年级{'下册' if term else '上册'} Unit/Lesson {n}"
                                      for n in range(1, last + 1) if n not in numbers)

    def _from_curriculum(self, library, current, boundary):
        series = current['curriculum']['series']
        included = []
        for row in library.chapters.values():
            data = row.get('curriculum')
            if not data or row['publisher'] != current['publisher']:
                continue
            # Primary-school vocabulary and the selected junior series stay
            # separate from junior editions belonging to a different syllabus.
            compatible = data['series'] == series or data['grade'] <= 6 < boundary[0]
            if compatible and position(row) <= boundary:
                included.append(row)
        included.sort(key=position)
        self.words, self.sources = set(), []
        self.current_words = current['curriculum']['words']
        for row in included:
            data = row['curriculum']
            for word in [*data['words'], *data.get('observedWords',[])]:
                self.words.update(lexical_tokens(word))
            self.sources.append({'chapterId':row['id'],'grade':row['grade'],'edition':row['edition'],
                'unit':row['unit'],'verified':data['verified'],'source':data['source'],
                'sourcePages':data['sourcePages'],'method':data['method'],'revision':data['revision'],
                'observedWordCount':len(data.get('observedWords',[]))})
        present = {position(r)[:2] for r in included}
        expected = {(g,t) for g in range(3,boundary[0]+1) for t in (0,1) if (g,t) <= boundary[:2]}
        self.missing_terms = [f"{g}年级{'下册' if t else '上册'}（当前教材系列缺少词表）" for g,t in sorted(expected-present)]
        self.missing_units = []
        for book_id in {r['curriculum']['bookId'] for r in included}:
            rows = [r for r in included if r['curriculum']['bookId']==book_id]
            data = rows[0]['curriculum']
            last = boundary[2] if book_id == current['curriculum']['bookId'] else data['lastUnit']
            numbers = {r['curriculum']['unitNumber'] for r in rows}
            self.missing_units.extend(f"{data['grade']}年级{data['term']} Unit {n}"
                for n in range(data['firstUnit'],last+1) if n not in numbers)
        self.textbook_ready = bool(self.sources) and all(s['verified'] for s in self.sources)

    def summary(self):
        verified = bool(self.sources) and all(s["verified"] for s in self.sources) and not self.missing_terms and not self.missing_units
        return {"chapterId": self.chapter["id"], "unit": self.chapter["unit"],
                "vocabularySize": len(self.words), "currentWords": self.current_words,
                "sources": self.sources, "missingTerms": self.missing_terms, "missingUnits": self.missing_units, "verified": verified,
                "textbookReady": self.textbook_ready,
                "notice": (("仅使用教材词表及已学单元课文中有原图依据的词，未加入主题补充词或默认基础词。" if self.chapter.get('curriculum') else "依据已导入核对词表检查。")
                           + (" 部分前序册次缺失，词汇范围已保守收窄。" if self.missing_terms or self.missing_units else "")
                           + "请确认这些册次、版次与学生实际所学一致。") if self.textbook_ready else
                "此版本没有可用的完整教材词表，写作生成暂不可用；请导入匹配词表或选择有原图词表的科普版。"}

    def knows(self, word):
        return known(word, self.words)

    def audit(self, title, essay, *, strict=False, word_count=80):
        body = tokens(essay)
        all_words = tokens(title) + body
        outside = Counter(w for w in all_words if not self.knows(w))
        count = sum(outside.values())
        ratio = count / len(all_words) if all_words else 0
        length_ok = round(word_count * .8) <= len(body) <= round(word_count * 1.2)
        english_only = bool(body) and not re.search(r"[^\x00-\x7f]", title + essay)
        vocabulary_ok = not outside if strict else len(outside) <= 4 and ratio <= .05
        return {"passed": bool(vocabulary_ok and length_ok and english_only), "wordCount": len(body),
                "lengthOK": length_ok, "englishOnly": english_only, "outsideCount": count,
                "outsideRatio": round(ratio, 5), "outsideWords": [{"word": w, "count": n} for w, n in sorted(outside.items())],
                "policy": {"maxUniqueOutside": 0 if strict else 4, "maxOutsideRatio": 0 if strict else .05,
                           "targetWords": word_count, "lengthTolerance": .2, "includesTitle": True}}
