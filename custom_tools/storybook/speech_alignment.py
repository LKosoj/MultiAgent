"""Match dialogue lines to the recognized words of the completed audio."""
from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher

MIN_LINE_SIMILARITY = 0.6
STEM_LENGTH = 5  # words with the same first letters are the same (endings differ in cases)
BAND_CELLS = 3_000_000  # a larger table is computed only near the diagonal
SHOWN_CHARS = 120  # length of the texts in the "line not found" message

_RU_UNITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
             "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
             "семнадцать", "восемнадцать", "девятнадцать"]
_RU_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят",
            "девяносто"]
_RU_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот",
                "девятьсот"]
_EN_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
             "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _ru_hundreds(number: int) -> list[str]:
    hundreds, rest = divmod(number, 100)
    words = [_RU_HUNDREDS[hundreds]] if hundreds else []
    if 0 < rest < 20:
        words.append(_RU_UNITS[rest])
    elif rest >= 20:
        words += [_RU_TENS[rest // 10]] + ([_RU_UNITS[rest % 10]] if rest % 10 else [])
    return words


def _en_hundreds(number: int) -> list[str]:
    hundreds, rest = divmod(number, 100)
    words = [_EN_UNITS[hundreds], "hundred"] if hundreds else []
    if 0 < rest < 20:
        words.append(_EN_UNITS[rest])
    elif rest >= 20:
        words += [_EN_TENS[rest // 10]] + ([_EN_UNITS[rest % 10]] if rest % 10 else [])
    return words


# Rules per project language. A language without an entry keeps numbers as digits and has no abbreviation
# tables; to support one more language add an entry here.
_RULES = {
    "ru": {
        "units": _RU_UNITS, "hundreds": _ru_hundreds, "scales": ["", "тысяча", "миллион", "миллиард"],
        "point": "запятая", "fraction_by_digits": False, "ordinal": r"[а-я]{1,3}", "thousands": r"[ \u00a0]", "decimal": ",",
        # Latin letters are read as Cyrillic: a term may be written in one script and recognized in the other
        "fold": {"ё": "е"} | dict(zip("abcdefghiklmnopqrstuvwyz", "абкдефгхиклмнопкрстувйз")) | {"j": "дж", "x": "кс"},
        "letters": dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (
            "эй би си ди и эф джи эйч ай джей кей эл эм эн оу пи кью ар эс ти ю ви дабл-ю икс уай зед").split())),
        "symbols": [(r"%", " процент "), (r"\$", " доллар "), (r"€", " евро "), (r"№", " номер "), (r"&", " и "),
                    (r"\+", " плюс "), (r"\bт\.\s*д\.", " так далее "), (r"\bт\.\s*е\.", " то есть "),
                    (r"\bт\.\s*п\.", " тому подобное ")],
        "words": {"две": "два", "одна": "один", "одно": "один", "млн": "миллион", "млрд": "миллиард",
                  "тыс": "тысяча", "руб": "рубль", "кг": "килограмм", "км": "километр",
                  "хы": "ха", "хе": "ха", "апчхи": "чхи"},
    },
    "en": {
        "units": _EN_UNITS, "hundreds": _en_hundreds, "scales": ["", "thousand", "million", "billion"],
        "point": "point", "fraction_by_digits": True, "ordinal": r"st|nd|rd|th", "thousands": ",", "decimal": r"\.", "fold": {},
        "letters": {letter: letter.lower() for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"} | {
            "H": "aitch", "W": "double u", "Y": "why"},
        "symbols": [(r"%", " percent "), (r"\$", " dollar "), (r"&", " and "), (r"\be\.\s*g\.", " for example "),
                    (r"\bi\.\s*e\.", " that is "), (r"\betc\.", " et cetera ")],
        "words": {"mr": "mister", "dr": "doctor", "vs": "versus"},
    },
}


def _number_pattern(rules: dict | None) -> str:
    """A number with groups: whole (thousand groups of three digits are joined) and fraction.

    A language without rules reads "," or "." followed by exactly three digits and a non-digit as thousands,
    any other as the decimal point. A fraction of three digits is therefore joined into one number, the same
    way on both sides of the comparison."""
    thousands = rules["thousands"] if rules else r"[.,\s]"
    decimal = rules["decimal"] if rules else "[.,]"
    return (rf"(?P<whole>\d{{1,3}}(?:{thousands}\d{{3}}(?!\d))+|\d+)(?:{decimal}(?P<fraction>\d+))?")


_ACRONYM = re.compile(r"\b[A-Z]{2,5}\b")
# Scripts written without spaces between words: every character is a token
_SPACELESS = ((0x0E00, 0x0EFF), (0x1000, 0x109F), (0x1780, 0x17FF), (0x3040, 0x30FF), (0x3400, 0x4DBF),
              (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2FA1F))


def _spell(digits: str, rules: dict) -> str:
    """A number as words; a long one or one with leading zeros is read digit by digit."""
    units = rules["units"]
    if len(digits) > 12 or (len(digits) > 1 and digits[0] == "0"):
        return " ".join(units[int(digit)] for digit in digits)
    number = int(digits)
    if number == 0:
        return units[0]
    words = []
    for scale in range(3, -1, -1):
        part = number // 1000 ** scale % 1000
        if part:
            words += rules["hundreds"](part) + ([rules["scales"][scale]] if scale else [])
    return " ".join(words)


def _expand_number(match: re.Match, rules: dict | None) -> str:
    whole, fraction = re.sub(r"\D", "", match["whole"]), match["fraction"]
    if not rules:  # no tables: digits stay, "3,5" and "3.5" come out the same
        return f" {whole} {fraction or ''} "
    spelled = _spell(whole, rules)
    if fraction:
        fraction_words = (" ".join(rules["units"][int(digit)] for digit in fraction)
                          if rules["fraction_by_digits"] else _spell(fraction, rules))
        spelled += f" {rules['point']} " + fraction_words
    return f" {spelled} "


def _split(text: str) -> list[str]:
    """Words made of letters, digits and combining marks; a letter of a spaceless script is a word of its own,
    its marks stay with it."""
    tokens, word, attached = [], "", False  # attached: the last character can take a mark
    for character in text:
        kind = unicodedata.category(character)[0]
        code = ord(character)
        if kind == "L" and any(low <= code <= high for low, high in _SPACELESS):
            if word:
                tokens.append(word)
            tokens.append(character)
            word, attached = "", True
        elif kind in "LN":
            word += character
            attached = True
        elif kind == "M" and attached:
            if word:
                word += character
            else:
                tokens[-1] += character
        else:
            if word:
                tokens.append(word)
            word, attached = "", False
    return tokens + [word] if word else tokens


def _tokens(text: str, language: str) -> list[str]:
    """Words of a text in one comparable form; with tables for the language numbers, symbols and
    abbreviations are spelled out."""
    rules = _RULES.get(language)
    text = unicodedata.normalize("NFKC", text)
    if rules:
        text = _ACRONYM.sub(lambda match: " " + " ".join(rules["letters"][letter] for letter in match.group(0)) + " ",
                            text)
    text = text.casefold()
    if rules:
        for old, new in rules["fold"].items():
            text = text.replace(old, new)
        for pattern, replacement in rules["symbols"]:
            text = re.sub(pattern, replacement, text)
        # The ending of an ordinal ("5-го", "5th") is dropped together with the number
        text = re.sub(f"{_number_pattern(rules)}(?:-?(?:{rules['ordinal']})(?![^\\W\\d_]))?",
                      lambda match: _expand_number(match, rules), text)
    else:
        text = re.sub(_number_pattern(None), lambda match: _expand_number(match, None), text)
    names = rules["words"] if rules else {}
    return [names.get(token, token) for token in _split(text)]


def _stem(token: str) -> str:
    return token[:STEM_LENGTH]


def _align(script: list[str], heard: list[str]) -> list[tuple[int, int]]:
    """Pairs (script token, heard token) of the best weighted common subsequence, in order."""
    count, total = len(script), len(heard)
    full = count * total <= BAND_CELLS
    width = total if full else max(200, max(count, total) // 10)
    negative = -1
    best = [[negative] * (total + 1) for _ in range(count + 1)]
    steps = [bytearray(total + 1) for _ in range(count + 1)]  # 1 match, 2 skip script, 3 skip heard
    for row in range(count + 1):
        center = row * total // max(count, 1)
        low, high = (0, total) if full else (max(0, center - width), min(total, center + width))
        for column in range(low, high + 1):
            if row == 0 or column == 0:
                best[row][column] = 0
                steps[row][column] = 2 if column == 0 and row else 3
                continue
            choices = []
            if script[row - 1] == heard[column - 1] and best[row - 1][column - 1] >= 0:
                choices.append((best[row - 1][column - 1] + len(script[row - 1]), 1))
            if best[row - 1][column] >= 0:
                choices.append((best[row - 1][column], 2))
            if best[row][column - 1] >= 0:
                choices.append((best[row][column - 1], 3))
            if not choices:
                continue
            # On a tie: match first, then skipping a heard token
            best[row][column], steps[row][column] = max(choices, key=lambda item: (item[0], item[1] == 1, item[1] == 3))
    pairs = []
    row, column = count, total
    while row and column:
        step = steps[row][column]
        if step == 1:
            pairs.append((row - 1, column - 1))
            row, column = row - 1, column - 1
        elif step == 2:
            row -= 1
        else:
            column -= 1
    pairs.reverse()
    return pairs


def _clip(text: str) -> str:
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS - 1] + "…"


class LineNotHeardError(ValueError):
    """No recognized word is left for a line: it should sound in start..end, between the words of its neighbours."""

    def __init__(self, message: str, start: float, end: float):
        super().__init__(message)
        self.start, self.end = start, end


def _not_found(number: int, line: dict, group_words: list[dict]) -> ValueError:
    heard = " ".join(word["text"] for word in group_words) or "ничего"
    expected = " ".join(line["text"].split())
    return ValueError(f"Реплика {number} не найдена в записи: ожидалось «{_clip(expected)}», услышано «{_clip(heard)}»")


def _check_words(words: list[dict], duration: float) -> None:
    if not isinstance(words, list) or not words:
        raise ValueError("Распознавание не вернуло слов")
    previous = 0.0
    for word in words:
        if not isinstance(word, dict):
            raise ValueError("Некорректные временные метки слов распознавания")
        start, end = word.get("start"), word.get("end")
        if (not isinstance(word.get("text"), str) or isinstance(start, bool)
                or isinstance(end, bool) or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or start < previous or end < start or end > duration):
            raise ValueError("Некорректные временные метки слов распознавания")
        previous = start


def measure_line_timing(lines: list[dict], words: list[dict], duration: float, language: str) -> list[dict]:
    """Start and end of every line by the recognized words {text, start, end}.

    Tokens of the script and of the heard words are matched in order over the whole text; the words between
    matched ones are shared by the neighbouring lines at the longest pauses. A line fails only when it is
    not found in the recording.
    """
    if not lines or any(not isinstance(line, dict) or not isinstance(line.get("text"), str) for line in lines):
        raise ValueError("Некорректные реплики сценария")
    _check_words(words, duration)

    script, owners = [], []
    line_tokens = []
    for index, line in enumerate(lines):
        tokens = _tokens(line["text"], language)
        if not tokens:
            raise ValueError(f"Реплика {index + 1} не содержит произносимого текста")
        line_tokens.append(tokens)
        script.extend(_stem(token) for token in tokens)
        owners.extend([index] * len(tokens))
    word_tokens = [_tokens(word["text"], language) for word in words]
    heard, heard_owners = [], []
    for index, tokens in enumerate(word_tokens):
        heard.extend(_stem(token) for token in tokens)
        heard_owners.extend([index] * len(tokens))

    # How many tokens of each word were matched to each line
    votes: dict[int, dict[int, int]] = {}
    for left, right in _align(script, heard):
        line, word = owners[left], heard_owners[right]
        votes.setdefault(word, {}).setdefault(line, 0)
        votes[word][line] += 1
    anchors: list[list[int]] = [[] for _ in lines]
    for word in sorted(votes):
        line = max(votes[word], key=lambda key: (votes[word][key], -key))  # a word goes to one line
        anchors[line].append(word)

    # first[i]..last[i]: words of line i; lines without anchors get words from the gaps
    count = len(words)
    first, last = [None] * len(lines), [None] * len(lines)
    for index, found in enumerate(anchors):
        if found:
            first[index], last[index] = found[0], found[-1]
    anchored = [-1] + [index for index, found in enumerate(anchors) if found] + [len(lines)]
    for before, after in zip(anchored, anchored[1:]):
        low = last[before] if before >= 0 else -1
        high = first[after] if after < len(lines) else count
        gap = list(range(before + 1, after))
        if not gap and (before < 0 or after >= len(lines)):
            continue
        if len(gap) > high - low - 1:
            raise LineNotHeardError(str(_not_found(gap[0] + 1, lines[gap[0]], [])),
                                    words[low]["end"] if low >= 0 else 0.0, words[high]["start"] if high < count else duration)

        def pause(position: int) -> float:
            left = words[position]["end"] if position >= 0 else 0.0
            right = words[position + 1]["start"] if position + 1 < count else duration
            return max(0.0, right - left)

        cuts_count = len(gap) + 1
        if not gap:
            cuts = [max(range(low, high), key=lambda position: (pause(position), -position))]
        else:
            ranked = sorted(range(low, high), key=lambda position: (-pause(position), position))
            cuts = sorted(ranked[:cuts_count])
        if before >= 0:
            last[before] = cuts[0]
        for number, index in enumerate(gap):
            first[index], last[index] = cuts[number] + 1, cuts[number + 1]
        if after < len(lines):
            first[after] = cuts[-1] + 1

    result = []
    for index, line in enumerate(lines):
        group = words[first[index]:last[index] + 1]
        target = "".join(line_tokens[index])
        transcript = "".join(token for tokens in word_tokens[first[index]:last[index] + 1] for token in tokens)
        if not group or SequenceMatcher(None, target, transcript, autojunk=False).ratio() < MIN_LINE_SIMILARITY:
            raise _not_found(index + 1, line, group)
        result.append({**line, "start": group[0]["start"], "end": group[-1]["end"], "word_start": first[index], "word_end": last[index] + 1})
    return result


def chunk_ranges(duration, size=180.0):
    if duration <= 0:
        raise ValueError("Длительность записи должна быть положительной")
    ranges = []
    start = 0.0
    while start < duration:
        end = min(start + size, duration)
        ranges.append((start, end))
        if end == duration:
            break
        start = end - 5.0
    return ranges


def merge_chunks(chunks):
    """Keep words farther from chunk edges and merge the same seam observation."""
    result = []
    for i, (start, end, words) in enumerate(chunks):
        left = (start + chunks[i - 1][1]) / 2 if i else -1
        right = (end + chunks[i + 1][0]) / 2 if i + 1 < len(chunks) else float("inf")
        for word in words:
            midpoint = (word["start"] + word["end"]) / 2
            if not left < midpoint <= right:
                continue
            if result and i and re.sub(r"[^\w]", "", result[-1]["text"].casefold()) == re.sub(r"[^\w]", "", word["text"].casefold()) and word["start"] < result[-1]["end"] and result[-1]["start"] < word["end"]:
                previous = result[-1]
                old_edge = min((previous["start"] + previous["end"]) / 2 - chunks[i-1][0], chunks[i-1][1] - (previous["start"] + previous["end"]) / 2)
                new_edge = min(midpoint - start, end - midpoint)
                if new_edge > old_edge:
                    result[-1] = word
            else:
                result.append(word)
    return result
