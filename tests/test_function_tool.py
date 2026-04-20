"""Tests for the @function_tool decorator."""

from __future__ import annotations

from typing import Literal

from tools import ToolSpec, function_tool


class TestBasicShape:
    def test_returns_toolspec(self) -> None:
        @function_tool()
        def echo(s: str) -> str:
            return s
        assert isinstance(echo, ToolSpec)
        assert echo.name == "echo"
        assert echo.function is not None

    def test_name_override(self) -> None:
        @function_tool(name="custom_name")
        def original(x: str) -> str:
            return x
        assert original.name == "custom_name"

    def test_description_override(self) -> None:
        @function_tool(description="custom description")
        def tool(x: str) -> str:
            """ignored — decorator override wins."""
            return x
        assert tool.description == "custom description"

    def test_description_from_docstring(self) -> None:
        @function_tool()
        def tool(x: str) -> str:
            """Summary line used as description."""
            return x
        assert tool.description == "Summary line used as description."

    def test_multiline_docstring_summary_only(self) -> None:
        @function_tool()
        def tool(x: str) -> str:
            """Summary line.

            Args:
                x: not included in summary.
            """
            return x
        assert tool.description == "Summary line."


class TestSchema:
    def test_primitive_types(self) -> None:
        @function_tool()
        def tool(s: str, n: int, f: float, b: bool) -> str:
            return ""
        props = tool.input_schema["properties"]
        assert props["s"]["type"] == "string"
        assert props["n"]["type"] == "integer"
        assert props["f"]["type"] == "number"
        assert props["b"]["type"] == "boolean"

    def test_defaults_recorded(self) -> None:
        @function_tool()
        def tool(name: str, loud: bool = False) -> str:
            return name
        props = tool.input_schema["properties"]
        assert "default" not in props["name"]
        assert props["loud"]["default"] is False
        assert tool.input_schema["required"] == ["name"]

    def test_list_annotation(self) -> None:
        @function_tool()
        def tool(tags: list[str]) -> str:
            return ""
        schema = tool.input_schema["properties"]["tags"]
        assert schema["type"] == "array"
        assert schema["items"]["type"] == "string"

    def test_optional_strips_none(self) -> None:
        @function_tool()
        def tool(x: str | None = None) -> str:
            return x or ""
        schema = tool.input_schema["properties"]["x"]
        assert schema["type"] == "string"

    def test_literal_becomes_enum(self) -> None:
        @function_tool()
        def tool(mode: Literal["fast", "slow"]) -> str:
            return mode
        schema = tool.input_schema["properties"]["mode"]
        assert schema["type"] == "string"
        assert set(schema["enum"]) == {"fast", "slow"}

    def test_arg_docstring_becomes_description(self) -> None:
        @function_tool()
        def tool(name: str, age: int = 0) -> str:
            """Greet someone.

            Args:
                name: The person to greet.
                age: Their age in years.
            """
            return ""
        props = tool.input_schema["properties"]
        assert props["name"]["description"] == "The person to greet."
        assert props["age"]["description"] == "Their age in years."

    def test_no_required_when_all_defaults(self) -> None:
        @function_tool()
        def tool(a: str = "x", b: int = 1) -> str:
            return ""
        assert "required" not in tool.input_schema

    def test_kwargs_and_self_stripped(self) -> None:
        class Host:
            @function_tool()
            def method(self, x: str, **kw: object) -> str:
                return x
        # method is a ToolSpec now; self + **kw should not be in the schema
        props = Host.method.input_schema["properties"]
        assert set(props.keys()) == {"x"}
