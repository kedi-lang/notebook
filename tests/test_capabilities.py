from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import kedi
import pytest
from kedi.agent_adapter.adapters import PydanticAdapter
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from kedi_notebook import NotebookSessionManager
from kedi_notebook.host_environment import PreparedHostEnvironment


class HostEnvironment:
    def prepare(self, *, executable: str, version: str, cwd: Path) -> PreparedHostEnvironment:
        return PreparedHostEnvironment(executable, str(Path(executable).parent.parent))


class NotebookCapabilities(PydanticAdapter):
    capabilities = replace(PydanticAdapter.capabilities, supports_capabilities=True)


@pytest.fixture(autouse=True)
def isolated_configuration():
    kedi.reset_config()
    yield
    kedi.reset_config()


def test_notebook_capability_cells_preserve_scope_without_leaking_to_another_session(tmp_path):
    instructions = []

    def respond(messages, info):
        instructions.append(info.instructions or "")
        return ModelResponse([TextPart("done")])

    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=HostEnvironment(),
        interactive_options={"adapter": NotebookCapabilities(FunctionModel(respond))},
    )
    try:
        first = manager.create(mode="host", python_id=manager.pythons[0].id)
        second = manager.create(mode="host", python_id=manager.pythons[0].id)
        setup = first.execute(
            cell_id="definitions",
            source="> capability: concise:\n  > instructions: Keep the reply brief.\n"
            "> instructions: Base guidance.\n> use: concise\n",
        )
        assert setup["ok"] is True, setup
        for index, prefix in enumerate(
            ("", "> instructions: Base guidance.\n", "> use: concise\n")
        ):
            result = first.execute(
                cell_id=f"query-{index}",
                source=prefix + "[reply] << Report the status.\n> show: <reply>\n",
            )
            assert result["ok"] is True, result
            assert result["stdout"] == "done\n"
        missing = second.execute(cell_id="missing", source="> use: concise")
        assert missing["ok"] is False
        independent = second.execute(
            cell_id="independent", source="[reply] << Report the status.\n> show: <reply>\n"
        )
        assert independent["ok"] is True, independent
    finally:
        manager.close_all()

    assert [text.count("Keep the reply brief.") for text in instructions] == [1, 0, 1, 0]
    assert ["Base guidance." in text for text in instructions] == [True, True, True, False]
