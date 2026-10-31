"""Minimal pandas shim for riscv-dv's riscv_utils.gen_config_table()."""
class _Column:
    def __init__(self, df, idx):
        self._df, self._idx = df, idx
    def apply(self, fn):
        return [fn(row[self._idx]) for row in self._df._rows]
class DataFrame:
    def __init__(self, data=None, columns=None):
        self._rows = [list(r) for r in (data or [])]
        self.columns = list(columns or (range(len(self._rows[0])) if self._rows else []))
    def __getitem__(self, name):
        return _Column(self, self.columns.index(name))
    def __setitem__(self, name, values):
        i = self.columns.index(name)
        for row, v in zip(self._rows, values):
            row[i] = v
    def __iter__(self):
        return iter(self._rows)
