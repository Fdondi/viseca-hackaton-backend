import os

os.environ["LEASH_LLM_PROVIDER"] = "none"  # tests never call a model

import pytest

from leash.data import load
from leash.session import Session


@pytest.fixture(scope="session")
def pack():
    return load()


@pytest.fixture
def session():
    return Session()


def run_scenario(session, sid, answer="decline", on_result=None):
    comp = session.compile(session.pack.scenarios[sid]["cardholder_instruction"], sid)
    m = session.confirm(comp["draft"], sid)
    if on_result:
        session.listeners.append(on_result)
    run = session.start(sid, m["mandate_id"])
    res = session.drive(run["run_id"], customer=(lambda r: answer) if answer else None)
    return m, run, {r["source_authorization_id"]: r for r in res}
