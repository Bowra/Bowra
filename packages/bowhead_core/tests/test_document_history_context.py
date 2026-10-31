"""Retained history is bound to the original script context and input bytes."""

from dataclasses import asdict

import pytest
from bowhead_core.documents.authoring_context import context
from bowhead_core.documents.authoring_protocol import PROFILE
from bowhead_core.documents.executor import WorkerInput
from bowhead_core.documents.planning_instruction import _retained_history_sources
from bowhead_core.documents.request import DocumentRequestError


@pytest.mark.unit
@pytest.mark.parametrize("fault", [None, "context_digest", "instruction", "history_bytes", "position", "role"])
def test_script_history_reader_matches_its_existing_context_writer(fault):
    instruction = "Use the confirmed quarter."
    template = WorkerInput("template.pptx", "template", b"Synthetic template")
    history = WorkerInput("Document clarification history.json", "data", b'{"answers":[{"question":"Quarter?","answer":"Q3"}]}')
    retained = history if fault != "role" else WorkerInput(history.name, "template", history.content)
    inputs = (template, retained)
    if fault == "position":
        inputs += (WorkerInput("later.json", "data", b"{}"),)
    record = asdict(context(instruction, inputs, api_reference="Synthetic test reference"))
    if fault == "context_digest":
        record["digest"] = "0" * 64
    if fault == "instruction":
        instruction = "A different request."
    if fault == "history_bytes":
        history = WorkerInput(history.name, history.role, b'{"answers":[]}')
    if fault is not None:
        with pytest.raises(DocumentRequestError, match="document_checkpoint_unavailable"):
            _retained_history_sources(record, instruction=instruction, history=history, profile=PROFILE)
        return
    sources, expected = _retained_history_sources(record, instruction=instruction, history=history, profile=PROFILE)
    assert sources[-1] == expected and expected["name"] == history.name
