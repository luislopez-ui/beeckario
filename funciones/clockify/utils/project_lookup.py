
import unicodedata
from difflib import get_close_matches

def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))

def resolve_project_fuzzy(user_input: str, projects: list[dict]):
    q = _normalize(user_input)

    # 1) Exact match
    exact = [p for p in projects if _normalize(p["name"]) == q]
    if len(exact) == 1:
        return exact[0], []

    # 2) Contains match
    contains = [p for p in projects if q in _normalize(p["name"])]
    if len(contains) == 1:
        return contains[0], []
    if len(contains) > 1:
        return None, contains

    # 3) Fuzzy match
    names = [_normalize(p["name"]) for p in projects]
    matches = get_close_matches(q, names, n=5, cutoff=0.65)

    fuzzy = [p for p in projects if _normalize(p["name"]) in matches]
    if len(fuzzy) == 1:
        return fuzzy[0], []
    if len(fuzzy) > 1:
        return None, fuzzy

    return None, []
