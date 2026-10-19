from evals.run import contains_all


def test_extra_values_are_fine():
    assert contains_all(["a", 10.0], ["x", "a", 10.004, 99])


def test_missing_value_fails():
    assert not contains_all(["a", 10.0], ["a", 11.0])


def test_duplicates_must_each_match():
    assert not contains_all([5, 5], [5])


def test_strings_case_insensitive():
    assert contains_all(["France"], ["france"])
