import asyncio


def test_default_is_empty():
    from llm_call_context import get_llm_call_context

    assert get_llm_call_context() == {}


def test_set_and_reset():
    from llm_call_context import get_llm_call_context, llm_call_context

    assert get_llm_call_context() == {}
    with llm_call_context(run_id="run-1", step_name="step-1"):
        assert get_llm_call_context() == {"run_id": "run-1", "step_name": "step-1"}
    assert get_llm_call_context() == {}


def test_nested_contexts_restore_outer():
    from llm_call_context import get_llm_call_context, llm_call_context

    with llm_call_context(run_id="outer", step_name="outer-step"):
        assert get_llm_call_context() == {"run_id": "outer", "step_name": "outer-step"}
        with llm_call_context(run_id="inner", step_name="inner-step"):
            assert get_llm_call_context() == {"run_id": "inner", "step_name": "inner-step"}
        assert get_llm_call_context() == {"run_id": "outer", "step_name": "outer-step"}
    assert get_llm_call_context() == {}


def test_survives_asyncio_to_thread():
    """Providers are called via asyncio.to_thread; the context must propagate into the thread."""
    from llm_call_context import get_llm_call_context, llm_call_context

    def read_context_in_thread():
        return get_llm_call_context()

    async def run():
        with llm_call_context(run_id="thread-run", step_name="thread-step"):
            return await asyncio.to_thread(read_context_in_thread)

    result = asyncio.run(run())
    assert result == {"run_id": "thread-run", "step_name": "thread-step"}


def test_context_unset_outside_with_block_in_thread():
    from llm_call_context import get_llm_call_context

    def read_context_in_thread():
        return get_llm_call_context()

    async def run():
        return await asyncio.to_thread(read_context_in_thread)

    result = asyncio.run(run())
    assert result == {}
