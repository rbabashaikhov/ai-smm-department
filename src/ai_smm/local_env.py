"""Loading a developer's local .env -- only from an explicit entry point.

No module of the package loads .env when it is imported. Importing
ai_smm.publishing.threads or ai_smm.storage used to call load_dotenv(),
which put the developer's real tokens into any process that imported
them: a test run, the HTTP API (through the application layer), a REPL.

Now only an entry point opts in: the `ai-smm` CLI and the
`ai-smm-worker` process call load_local_dotenv() before reading their
settings, and the local scripts that need it do the same.

Production semantics do not change. Images never contain a .env
(.dockerignore), containers get their environment from compose, and
load_dotenv never overrides a variable that is already set -- so in a
container this was, and is, a no-op. The search is the one the import-time
call made: upwards from the package's own directory, i.e. the repository
root in a source checkout. PYTHON_DOTENV_DISABLED is honoured.
"""
from __future__ import annotations


def load_local_dotenv() -> bool:
    """Load the nearest .env above the package; True if one was loaded."""

    from dotenv import find_dotenv, load_dotenv

    # find_dotenv walks up from the directory of the file that calls it:
    # this module, inside the package -- the same start the import-time
    # calls in publishing/threads.py and storage.py had.
    path = find_dotenv(usecwd=False)

    if not path:
        return False

    return load_dotenv(path, override=False)
