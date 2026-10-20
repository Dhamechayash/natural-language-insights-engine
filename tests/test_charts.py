import pytest

from app.charts import recommend_chart


def spec(columns, rows, **kw):
    return recommend_chart(columns, rows, **kw)


def test_time_series_is_a_line():
    s = spec(["month", "revenue"], [["2011-01", 10.0], ["2011-02", 12.5], ["2011-03", 9.0]])
    assert (s["type"], s["x"], s["y"], s["series"], s["x_kind"]) == ("line", "month", ["revenue"], None, "time")
    assert s["alternatives"] == ["line", "bar"]


def test_iso_dates_and_quarters_count_as_time():
    assert spec(["d", "n"], [["2011-03-01", 1], ["2011-03-02", 2]])["x_kind"] == "time"
    assert spec(["q", "n"], [["2011-Q1", 1], ["2011-Q2", 2]])["x_kind"] == "time"
    assert spec(["ts", "n"], [["2011-03-01T10:00:00", 1], ["2011-03-02T10:00:00", 2]])["x_kind"] == "time"


def test_integer_year_column_is_time():
    assert spec(["year", "n"], [[2010, 5], [2011, 7]])["type"] == "line"


def test_time_with_one_category_is_multi_line():
    rows = [["2011-01", "UK", 5], ["2011-01", "FR", 3], ["2011-02", "UK", 6], ["2011-02", "FR", 4]]
    s = spec(["month", "country", "revenue"], rows)
    assert (s["type"], s["x"], s["y"], s["series"]) == ("line", "month", ["revenue"], "country")


def test_too_many_series_falls_back_to_table():
    rows = [["2011-01", f"c{i}", i] for i in range(7)] + [["2011-02", f"c{i}", i] for i in range(7)]
    assert spec(["month", "country", "revenue"], rows) is None


def test_ranking_is_a_bar():
    s = spec(["description", "revenue"], [["A", 30.0], ["B", 20.0], ["C", 10.0]])
    assert (s["type"], s["x"], s["y"], s["x_kind"], s["alternatives"]) == ("bar", "description", ["revenue"], "category", ["bar"])


def test_rank_column_is_ignored():
    s = spec(["rank", "country", "orders"], [[1, "UK", 90], [2, "FR", 40], [3, "DE", 30]])
    assert (s["x"], s["y"]) == ("country", ["orders"])


def test_identifier_column_is_a_label_not_a_measure():
    s = spec(["customerid", "spend"], [[12346, 50.5], [12347, 20.0], [12348, 75.0]])
    assert (s["type"], s["x"], s["y"]) == ("bar", "customerid", ["spend"])


def test_several_comparable_measures_are_grouped():
    s = spec(["country", "orders", "customers"], [["UK", 90, 40], ["FR", 40, 20]])
    assert s["y"] == ["orders", "customers"]


def test_measures_on_wildly_different_scales_share_no_axis():
    s = spec(["country", "orders", "revenue"], [["UK", 90, 900000.0], ["FR", 40, 400000.0]])
    assert s["y"] == ["orders"]


def test_two_categories_one_measure_is_a_series_bar():
    rows = [[c, p, 1] for c in ("UK", "FR", "DE") for p in ("A", "B")]
    s = spec(["country", "product", "n"], rows)
    assert (s["x"], s["series"]) == ("country", "product")


def test_two_measures_is_a_scatter():
    s = spec(["quantity", "price"], [[1, 2.5], [3, 1.5], [5, 0.5]])
    assert (s["type"], s["x"], s["y"]) == ("scatter", "quantity", ["price"])


def test_many_raw_values_is_a_histogram():
    s = spec(["order_value"], [[float(i)] for i in range(20)])
    assert s["type"] == "histogram" and s["x"] == "order_value"


@pytest.mark.parametrize(
    "columns, rows",
    [
        (["revenue"], [[10.0]]),  # a single value is the answer text
        (["country", "revenue"], [["UK", 10.0]]),  # one row
        (["country", "revenue"], []),  # empty
        (["a", "b"], [["x", "y"], ["z", "w"]]),  # no measure
        (["order_value"], [[1.0], [2.0], [3.0]]),  # too few points for a histogram
        (["name", "n"], [[f"p{i}", i] for i in range(40)]),  # too many categories
    ],
)
def test_table_is_the_better_view(columns, rows):
    assert spec(columns, rows) is None


def test_truncated_result_is_not_charted():
    assert spec(["country", "revenue"], [["UK", 1.0], ["FR", 2.0]], truncated=True) is None


def test_nulls_do_not_break_classification():
    s = spec(["month", "revenue"], [["2011-01", None], ["2011-02", 3.0]])
    assert s["type"] == "line"
