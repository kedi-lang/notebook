# Kedi Notebook

Kedi Notebook is the local, incremental notebook interface for the Kedi
Programming Language. It provides editable Kedi, terminal, and Markdown cells,
with either browser-owned Pyodide or a selected host Python interpreter.

## Install

```bash
python -m pip install kedi-notebook
```

For repository development:

```bash
uv sync --group dev
```

## Run

```bash
kedi-notebook
```

The same server is exposed by Kedi when this package is installed:

```bash
kedi notebook
```

The default address is <http://127.0.0.1:8788/notebook/>. Add one or more host
Python installations explicitly when needed:

```bash
kedi-notebook --python /opt/homebrew/bin/python3.11
kedi notebook --python ~/.pyenv/versions/3.12.4/bin/python --port 8899
```

Use `--host`, `--port`, `--cwd`, and `--no-open` to control local serving.

## Runtime Model

Browser mode starts one persistent Pyodide worker as the page loads. Host mode
starts one persistent worker with the selected Python executable. Kedi cells
execute incrementally against that session; completed cells remain editable
and rerunnable, and output is displayed under its source.

Cells beginning with `!` are terminal cells. Host commands run in the notebook
working directory, with `!python` and `!pip` bound to the selected interpreter.
Browser mode supports `!pip install`, `!uv add`, `!pip list`, `!echo`, and
`!pwd`. Terminal output streams into the active cell while it runs.

Notebook execution is non-transactional. Rerunning a cell is a new execution
attempt against current state; it does not roll back previous side effects.
Displayed cell numbers follow notebook order and remain unchanged across
reruns. Adding, moving, or deleting cells recomputes affected positions.
