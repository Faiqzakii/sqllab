from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, TextIO


class AtomicWriter:
    def __init__(self, target: Path, output_format: str, columns: list[str]):
        if output_format not in {"csv", "jsonl"}:
            raise ValueError("Format output wajib csv atau jsonl")
        self.target = target
        self.output_format = output_format
        self.columns = columns
        self._file: TextIO | None = None
        self._temporary: Path | None = None
        self._csv: csv.DictWriter[str] | None = None

    def __enter__(self) -> "AtomicWriter":
        self.target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{self.target.name}.", suffix=".partial", dir=self.target.parent)
        self._temporary = Path(name)
        self._file = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        if self.output_format == "csv":
            self._csv = csv.DictWriter(self._file, fieldnames=self.columns)
            self._csv.writeheader()
        return self

    def write_rows(self, rows: Iterable[dict[str, object]]) -> None:
        if self._file is None:
            raise RuntimeError("Writer belum dibuka")
        expected = set(self.columns)
        for row in rows:
            if set(row) != expected:
                raise ValueError("Schema drift terdeteksi")
            if self._csv is not None:
                self._csv.writerow(row)
            else:
                self._file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def __exit__(self, error_type: object, error: object, traceback: object) -> None:
        if self._file is None or self._temporary is None:
            return
        try:
            if error_type is None:
                self._file.flush()
                os.fsync(self._file.fileno())
            self._file.close()
            if error_type is None:
                os.replace(self._temporary, self.target)
            else:
                self._temporary.unlink(missing_ok=True)
        finally:
            self._file = None
