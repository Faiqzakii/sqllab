import unittest

from sql_lab_extractor.query import QueryError, build_column_chunks, merge_column_pages


class ColumnChunkTests(unittest.TestCase):
    def test_splits_explicit_select_list_with_key_in_every_chunk(self):
        sql = "SELECT assignment_id, name, grade, note FROM sample ORDER BY assignment_id"
        chunks = build_column_chunks(sql, 3, "assignment_id")
        self.assertEqual(
            chunks,
            [
                "SELECT assignment_id, name, grade FROM sample ORDER BY assignment_id",
                "SELECT assignment_id, note FROM sample ORDER BY assignment_id",
            ],
        )
        for chunk in chunks:
            self.assertIn("assignment_id", chunk)

    def test_handles_quoted_strings_and_nested_expressions(self):
        sql = (
            "SELECT id, concat(a, ', b') AS label, "
            "CASE WHEN x > 1 THEN 'big, (yes)' ELSE 'small' END AS size "
            "FROM t ORDER BY id"
        )
        chunks = build_column_chunks(sql, 2, "id")
        self.assertEqual(
            chunks,
            [
                "SELECT id, concat(a, ', b') AS label FROM t ORDER BY id",
                "SELECT id, CASE WHEN x > 1 THEN 'big, (yes)' ELSE 'small' END AS size FROM t ORDER BY id",
            ],
        )

    def test_matches_key_through_explicit_alias(self):
        chunks = build_column_chunks(
            "SELECT s.id AS assignment_id, s.name FROM sample s ORDER BY 1", 2, "assignment_id"
        )
        self.assertEqual(chunks, ["SELECT s.id AS assignment_id, s.name FROM sample s ORDER BY 1"])

    def test_matches_qualified_key_column(self):
        sql = "SELECT t.assignment_id, t.name FROM sample t ORDER BY t.assignment_id"
        chunks = build_column_chunks(sql, 2, "assignment_id")
        self.assertEqual(chunks, ["SELECT t.assignment_id, t.name FROM sample t ORDER BY t.assignment_id"])

    def test_accepts_trailing_semicolon_and_distinct(self):
        self.assertEqual(
            build_column_chunks("SELECT id, a FROM t ORDER BY id;", 2, "id"),
            ["SELECT id, a FROM t ORDER BY id"],
        )
        self.assertEqual(
            build_column_chunks("SELECT DISTINCT id, a FROM t", 2, "id"),
            ["SELECT DISTINCT id, a FROM t"],
        )

    def test_single_key_column_yields_single_chunk(self):
        self.assertEqual(
            build_column_chunks("SELECT id FROM t ORDER BY id", 5, "id"),
            ["SELECT id FROM t ORDER BY id"],
        )
        self.assertEqual(build_column_chunks("SELECT id FROM t", 1, "id"), ["SELECT id FROM t"])

    def test_refuses_select_star(self):
        with self.assertRaises(QueryError):
            build_column_chunks("SELECT * FROM sample ORDER BY id", 3, "id")
        with self.assertRaises(QueryError):
            build_column_chunks("SELECT t.*, id FROM sample t", 3, "id")

    def test_refuses_cte(self):
        with self.assertRaises(QueryError):
            build_column_chunks("WITH x AS (SELECT id FROM t) SELECT id, a FROM x", 2, "id")

    def test_rejects_invalid_chunk_size(self):
        for size in (0, -1):
            with self.subTest(size=size), self.assertRaises(QueryError):
                build_column_chunks("SELECT id, a FROM t", size, "id")
        with self.assertRaises(QueryError):
            build_column_chunks("SELECT id, a FROM t", 1, "id")

    def test_refuses_unsafe_sql(self):
        for sql in (
            "SELECT id FROM t; DROP TABLE t",
            "SELECT id FROM t -- note",
            "SELECT id FROM t /* note */",
            "DELETE FROM t",
            "SELECT id FROM t UNION SELECT id FROM t2",
        ):
            with self.subTest(sql=sql), self.assertRaises(QueryError):
                build_column_chunks(sql, 2, "id")

    def test_refuses_unparseable_sql(self):
        for sql in ("", "SELECT id", "SELECT id, FROM t", "SELECT (id FROM t", "SELECT FROM t"):
            with self.subTest(sql=sql), self.assertRaises(QueryError):
                build_column_chunks(sql, 2, "id")

    def test_rejects_missing_and_duplicate_key(self):
        with self.assertRaises(QueryError):
            build_column_chunks("SELECT id, name FROM t", 2, "missing_col")
        with self.assertRaises(QueryError):
            build_column_chunks("SELECT id, id FROM t", 2, "id")
        with self.assertRaises(QueryError):
            build_column_chunks("SELECT id, other AS id FROM t", 2, "id")

    def test_rejects_invalid_chunk_size(self):
        for size in (0, -1, 1):
            with self.subTest(size=size), self.assertRaises(QueryError):
                build_column_chunks("SELECT id, a FROM t", size, "id")

    def test_merge_joins_chunks_by_key_not_index(self):
        chunk_a = [{"id": 1, "name": "b"}, {"id": 2, "name": "a"}]
        chunk_b = [{"id": 2, "grade": "B"}, {"id": 1, "grade": "A"}]
        merged = merge_column_pages([chunk_a, chunk_b], "id")
        self.assertEqual(
            merged,
            [
                {"id": 1, "name": "b", "grade": "A"},
                {"id": 2, "name": "a", "grade": "B"},
            ],
        )

    def test_merge_rejects_missing_key(self):
        with self.assertRaises(QueryError):
            merge_column_pages([[{"id": 1, "name": "a"}], [{"name": "b"}]], "id")

    def test_merge_rejects_duplicate_key_within_chunk(self):
        with self.assertRaises(QueryError):
            merge_column_pages([[{"id": 1}, {"id": 1}]], "id")

    def test_merge_rejects_conflicting_values(self):
        with self.assertRaises(QueryError):
            merge_column_pages([[{"id": 1, "v": "a"}], [{"id": 1, "v": "b"}]], "id")
    def test_merge_rejects_non_identical_key_sets(self):
        with self.assertRaisesRegex(QueryError, "Kumpulan kunci"):
            merge_column_pages(
                [[{"id": 1, "name": "first"}], [{"id": 2, "grade": "A"}]],
                "id",
            )

    def test_merge_requires_chunks_and_valid_key(self):
        with self.assertRaises(QueryError):
            merge_column_pages([], "id")
        with self.assertRaises(QueryError):
            merge_column_pages([[{"id": 1}]], "bad key!")


if __name__ == "__main__":
    unittest.main()
