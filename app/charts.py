"""Pick a chart for a query result from its shape alone.

No LLM call: the chart is drawn from the same rows as the table, so it can never show a number the data does not hold.
Column roles are inferred from values: T (time), N (measure), C (category). Returns None when a table is the better view.
"""

import re

MAX_SERIES = 6  # one color per series, in fixed order; more series are unreadable, so the table is shown instead
MAX_CATEGORIES = 25
MIN_HISTOGRAM_ROWS = 10
MAX_MEASURES = 4
MAX_SCALE_RATIO = 100  # measures this far apart on one axis would flatten the smaller one

_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")
_PERIOD = re.compile(r"^\d{4}[- ]?(Q[1-4]|W\d{2})$", re.IGNORECASE)
_TIME_NAME = re.compile(r"year|month|quarter|week|day|hour", re.IGNORECASE)
_ID_NAME = re.compile(r"(^|_)(id|no|num|number|code)$|[a-z](id|no|code)$", re.IGNORECASE)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _role(name: str, values: list) -> str:
    """T, N, C, or '-' for columns that carry no chartable meaning (e.g. a rank)."""
    present = [v for v in values if v is not None]
    if not present:
        return "C"
    if all(isinstance(v, str) and (_DATE.match(v) or _PERIOD.match(v)) for v in present):
        return "T"
    if all(_is_number(v) for v in present):
        if "rank" in name.lower():
            return "-"
        if all(isinstance(v, int) for v in present):
            if _TIME_NAME.search(name):
                return "T"
            if _ID_NAME.search(name):
                return "C"  # an identifier, not a quantity
        return "N"
    return "C"


def _distinct(values: list) -> int:
    return len({v for v in values if v is not None})


def _comparable(measures: list[str], columns: list[str], rows: list[list]) -> list[str]:
    """Keep measures that can share one axis; when scales differ wildly only the first is charted."""
    if len(measures) < 2:
        return measures
    peaks = []
    for m in measures:
        i = columns.index(m)
        peaks.append(max((abs(r[i]) for r in rows if r[i] is not None), default=0))
    nonzero = [p for p in peaks if p > 0]
    if nonzero and max(nonzero) / min(nonzero) > MAX_SCALE_RATIO:
        return measures[:1]
    return measures


def _drop_redundant_label(cols: dict[str, list], cats: list[str]) -> list[str]:
    """A code and its name (stock_code + description) are one axis, not two: keep the readable name."""
    if len(cats) != 2:
        return cats
    a, b = cats
    pairs = {(x, y) for x, y in zip(cols[a], cols[b])}
    if len(pairs) != _distinct(cols[a]) or len(pairs) != _distinct(cols[b]):
        return cats  # not one-to-one, so the second column is a real grouping
    code = a if _ID_NAME.search(a) and not _ID_NAME.search(b) else b if _ID_NAME.search(b) and not _ID_NAME.search(a) else None
    if code is None:  # no naming hint: the shorter text is the code
        code = min(cats, key=lambda c: sum(len(str(v)) for v in cols[c] if v is not None))
    return [c for c in cats if c != code]


def recommend_chart(columns: list[str], rows: list[list], truncated: bool = False) -> dict | None:
    """{type, x, y, series, x_kind, alternatives} or None. `type` is one of line, bar, scatter, histogram."""
    if truncated or len(rows) < 2 or not columns:
        return None  # a single value is the answer text; a truncated result would draw a misleading partial chart

    cols = {c: [r[i] for r in rows] for i, c in enumerate(columns)}
    roles = {c: _role(c, v) for c, v in cols.items()}
    time_cols = [c for c in columns if roles[c] == "T"]
    measures = [c for c in columns if roles[c] == "N"]
    cats = _drop_redundant_label(cols, [c for c in columns if roles[c] == "C"])

    if len(columns) == 1:
        if measures and len(rows) >= MIN_HISTOGRAM_ROWS:
            return _spec("histogram", measures[0], [], None, "category", ["histogram"])
        return None

    if len(time_cols) > 1 or not (measures or time_cols):
        return None

    if time_cols:
        x = time_cols[0]
        if not measures:
            return None
        if not cats and len(measures) <= MAX_MEASURES - 1:
            return _spec("line", x, _comparable(measures, columns, rows), None, "time", ["line", "bar"])
        if len(cats) == 1 and len(measures) == 1 and _distinct(cols[cats[0]]) <= MAX_SERIES:
            return _spec("line", x, measures, cats[0], "time", ["line", "bar"])
        return None

    if cats:
        if len(cats) == 1 and 1 <= len(measures) <= MAX_MEASURES:
            if _distinct(cols[cats[0]]) > MAX_CATEGORIES or len(rows) > MAX_CATEGORIES:
                return None
            return _spec("bar", cats[0], _comparable(measures, columns, rows), None, "category", ["bar"])
        if len(cats) == 2 and len(measures) == 1:
            x, series = sorted(cats, key=lambda c: -_distinct(cols[c]))
            if _distinct(cols[series]) <= MAX_SERIES and _distinct(cols[x]) <= MAX_CATEGORIES:
                return _spec("bar", x, measures, series, "category", ["bar"])
        return None

    if len(measures) >= 2 and len(rows) >= 3:
        return _spec("scatter", measures[0], measures[1:2], None, "category", ["scatter"])
    return None


def _spec(kind, x, y, series, x_kind, alternatives) -> dict:
    return {"type": kind, "x": x, "y": y, "series": series, "x_kind": x_kind, "alternatives": alternatives}
