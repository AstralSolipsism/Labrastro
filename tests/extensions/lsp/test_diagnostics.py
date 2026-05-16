from reuleauxcoder.extensions.lsp.diagnostics import (
    Diagnostic,
    DiagnosticBlock,
    SEVERITY_WARNING,
    diagnostic_from_lsp,
    render_blocks,
)


def test_render_blocks_filters_warnings_by_default_and_escapes_file_name() -> None:
    rendered = render_blocks(
        [
            DiagnosticBlock(
                file_path='src/a"&.py',
                items=[
                    Diagnostic(line=2, character=4, message="bad <value>"),
                    Diagnostic(
                        line=1,
                        character=1,
                        message="warn",
                        severity=SEVERITY_WARNING,
                    ),
                ],
            )
        ]
    )

    assert rendered is not None
    assert '<diagnostics file="src/a&quot;&amp;.py">' in rendered
    assert "ERROR [2:4] bad &lt;value&gt;" in rendered
    assert "WARNING" not in rendered


def test_diagnostic_from_lsp_converts_zero_based_positions() -> None:
    diagnostic = diagnostic_from_lsp(
        {
            "range": {"start": {"line": 3, "character": 8}},
            "severity": 2,
            "message": "warn",
            "code": "W1",
        }
    )

    assert diagnostic.line == 4
    assert diagnostic.character == 9
    assert diagnostic.is_warning
    assert diagnostic.code == "W1"
