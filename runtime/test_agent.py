"""Offline check of the agent loop: a fake model and a fake tool, no network, no API key.

Run: uv run python test_agent.py
"""
import asyncio

import agent

ADD_TOOL = {"server": {}, "read_only": True, "schema": {"type": "function", "function": {"name": "add", "parameters": {}}}}


def tool_call_reply(name, arguments_json):
    """Build a model reply that asks for one tool call, in the same shape OpenRouter returns."""
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments_json}}]}


async def check_tool_then_answer():
    """The model asks for `add`, gets the tool's result back, then answers using it."""
    replies = iter([tool_call_reply("add", '{"a": 2, "b": 2}'), {"role": "assistant", "content": "2 + 2 = 4"}])
    seen_by_model = []

    async def fake_model(messages, tool_schemas):
        """Return the next scripted reply and remember what the model was shown."""
        seen_by_model.append(list(messages))
        return next(replies), 10

    async def fake_tool(tool, arguments):
        """Pretend to be the add tool."""
        return str(arguments["a"] + arguments["b"])

    answer, steps = await agent.run_agent("what is 2+2?", [ADD_TOOL], fake_model, fake_tool)
    assert answer == "2 + 2 = 4", answer
    assert [step["type"] for step in steps] == ["model", "tool", "model"], steps
    assert steps[1]["result"] == "4" and steps[1]["arguments"] == {"a": 2, "b": 2}
    assert seen_by_model[1][-1] == {"role": "tool", "tool_call_id": "call-1", "content": "4"}  # result went back to the model


async def check_step_limit_and_unknown_tool():
    """A model that keeps asking for a tool that doesn't exist is stopped at MAX_STEPS instead of looping forever."""
    async def stuck_model(messages, tool_schemas):
        """Always ask for a missing tool."""
        return tool_call_reply("delete_everything", "{}"), 5

    answer, steps = await agent.run_agent("go", [ADD_TOOL], stuck_model)
    assert answer.startswith("Stopped"), answer
    assert sum(step["type"] == "model" for step in steps) == agent.MAX_STEPS
    assert steps[1]["result"] == "unknown tool: delete_everything"


asyncio.run(check_tool_then_answer())
asyncio.run(check_step_limit_and_unknown_tool())
print("agent loop checks passed")
