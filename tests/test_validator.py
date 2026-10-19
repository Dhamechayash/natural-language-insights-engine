import pytest

from app.validator import SqlRejected, validate_sql

OK = [
    "SELECT count(*) FROM data",
    "select country, sum(quantity * unitprice) as revenue from data group by 1 order by 2 desc limit 10;",
    "WITH a AS (SELECT * FROM data) SELECT count(*) FROM a",
    "SELECT * FROM data UNION ALL SELECT * FROM data",
    "SELECT a.stockcode, b.stockcode FROM data a JOIN data b ON a.invoiceno = b.invoiceno AND a.stockcode < b.stockcode",
]

BAD = [
    ("", "Empty"),
    ("DROP TABLE data", "Only SELECT"),
    ("DELETE FROM data", "Only SELECT"),
    ("SELECT 1; DROP TABLE data", "Exactly one"),
    ("SELECT * FROM read_csv('/etc/passwd')", "Table functions"),
    ("SELECT * FROM '/etc/passwd'", None),
    ("COPY data TO '/tmp/x.csv'", None),
    ("SELECT * FROM other_table", "Unknown table"),
    ("SELECT * FROM main.data", "Qualified"),
    ("SELECT read_text('/etc/passwd')", "not allowed"),
    ("SELECT * FROM glob('*')", None),
    ("ATTACH '/tmp/x.db'", None),
    ("PRAGMA database_list", None),
    ("INSTALL httpfs", None),
    ("SELECT * FROM duckdb_settings()", None),
    ("SELEC nonsense", None),
]


@pytest.mark.parametrize("sql", OK)
def test_accepts_select(sql):
    assert validate_sql(sql)


@pytest.mark.parametrize("sql,fragment", BAD)
def test_rejects(sql, fragment):
    with pytest.raises(SqlRejected) as exc:
        validate_sql(sql)
    if fragment:
        assert fragment in exc.value.message
