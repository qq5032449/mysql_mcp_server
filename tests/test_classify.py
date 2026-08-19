import pytest
from mysql_mcp_server.sql_classify import classify, strip_comments, first_keyword


class TestStripComments:
    def test_line_comment(self):
        assert strip_comments("-- hi\nSELECT 1") == "  \nSELECT 1"

    def test_hash_comment(self):
        assert strip_comments("# hi\nSELECT 1") == "  \nSELECT 1"

    def test_block_comment(self):
        assert strip_comments("/* hi */ SELECT 1") == "   SELECT 1"

    def test_block_comment_multiline(self):
        assert strip_comments("/* a\nb */ SELECT 1") == "   SELECT 1"


class TestFirstKeyword:
    def test_select(self):
        assert first_keyword("select * from t") == "SELECT"

    def test_leading_paren(self):
        assert first_keyword("(SELECT 1)") == "SELECT"

    def test_empty(self):
        assert first_keyword("") == ""
        assert first_keyword("   ") == ""


class TestClassify:
    @pytest.mark.parametrize("sql", [
        "SELECT * FROM t",
        "select 1",
        "SHOW TABLES",
        "DESCRIBE t",
        "DESC t",
        "EXPLAIN SELECT 1",
        "-- note\nSELECT 1",
        "/* note */ SELECT 1",
        "(SELECT 1)",
        "SELECT * FROM t FOR UPDATE",
    ])
    def test_read(self, sql):
        assert classify(sql) == "read"

    @pytest.mark.parametrize("sql", [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "REPLACE INTO t VALUES (1)",
        "CREATE TABLE t (id INT)",
        "ALTER TABLE t DROP COLUMN c",
        "RENAME TABLE a TO b",
        "SET @x = 1",
        "GRANT ALL ON *.* TO u",
        "CALL p()",
        "LOCK TABLES t WRITE",
        "ANALYZE TABLE t",
        "OPTIMIZE TABLE t",
        "  update t set x=1",
    ])
    def test_write(self, sql):
        assert classify(sql) == "write"

    @pytest.mark.parametrize("sql", [
        "DELETE FROM t",
        "TRUNCATE TABLE t",
        "DROP TABLE t",
        "DROP DATABASE db",
        "drop table t",
    ])
    def test_delete(self, sql):
        assert classify(sql) == "delete"

    @pytest.mark.parametrize("sql", [
        "WITH c AS (SELECT 1) SELECT * FROM c",
        "with cte as (select 1) select * from cte",
    ])
    def test_cte_read(self, sql):
        assert classify(sql) == "read"

    @pytest.mark.parametrize("sql", [
        "WITH c AS (SELECT 1) INSERT INTO t SELECT * FROM c",
        "WITH c AS (SELECT 1) UPDATE t JOIN c ON t.id = c.id SET t.x = 1",
        "WITH c AS (SELECT 1) REPLACE INTO t SELECT * FROM c",
    ])
    def test_cte_write(self, sql):
        assert classify(sql) == "write"

    @pytest.mark.parametrize("sql", [
        "WITH c AS (SELECT 1) DELETE FROM t",
        "WITH c AS (SELECT 1) DELETE t FROM t JOIN c ON t.id = c.id",
    ])
    def test_cte_delete(self, sql):
        assert classify(sql) == "delete"

    def test_cte_nested_parens(self):
        # CTE 内嵌套括号中的 SELECT 不算主语句
        assert classify("WITH c AS (SELECT * FROM (SELECT 1) x) DELETE FROM t") == "delete"

    def test_cte_identifier_not_keyword(self):
        # 标识符 total_select 不会被误判为 SELECT 关键字
        assert classify("WITH c AS (SELECT 1) UPDATE t SET total_select = 2") == "write"

    @pytest.mark.parametrize("sql", ["", "   ", "FOO BAR", "123", "WITH c AS (SELECT 1) MERGE INTO t"])
    def test_unknown_defaults_to_write(self, sql):
        assert classify(sql) == "write"
