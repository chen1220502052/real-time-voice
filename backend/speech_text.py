"""Incremental speakable chunks without splitting English words or decimals."""

import re


def take_speech_segments(buffer: str, *, first_chunk: bool = False) -> tuple[list[str], str]:
    segments = []
    while buffer:
        cut = None
        for index, char in enumerate(buffer):
            if char in "，。！？；\n":
                cut = index + 1
                break
            if char in ".!?;" and index + 1 < len(buffer) and buffer[index + 1].isspace():
                if char == ".":
                    word = re.search(r"([A-Za-z]+)$", buffer[:index])
                    if word and (len(word[1]) == 1 or word[1].lower() in {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "vs", "etc"}):
                        continue
                cut = index + 1
                break
            if char == "," and index >= (12 if first_chunk else 32) and index + 1 < len(buffer) and buffer[index + 1].isspace():
                cut = index + 1
                break
        if cut is None and first_chunk and len(buffer) >= 24:
            boundary = re.search(r"\s+", buffer[24:])
            if boundary:
                cut = 24 + boundary.end()
        if first_chunk and cut is not None and cut > 40:
            # English Bot deltas are already complete sentences. A long
            # confirmed sentence need not be fully synthesized before audio
            # starts. Split only at a word boundary, retaining EVERY character;
            # leave number expressions intact for intelligible arithmetic.
            numeric = r'\d|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)\b'
            if not re.search(numeric, buffer[:cut], re.I):
                dangling = {'a','an','the','to','of','for','with','about','in','on','and','or','but',
                            'if','because','that','which','is','are','was','were','can','will',
                            'have','has','my','your','their'}
                for boundary in re.finditer(r'\s+', buffer[:cut]):
                    if 24 <= boundary.start() <= 36:
                        word = buffer[:boundary.start()].split()[-1].lower()
                        if word not in dangling:
                            cut = boundary.end()
                            break
        if cut is None and len(buffer) >= 100:
            # Keep at least one unfinished word in the buffer for the next delta.
            spaces = [m.start() for m in re.finditer(r"\s+", buffer[:100]) if m.start() >= 32]
            if spaces:
                cut = spaces[-1] + 1
        if cut is None:
            break
        segment, buffer = buffer[:cut], buffer[cut:]
        if segment.strip():
            segments.append(segment)
            first_chunk = False
    return segments, buffer
