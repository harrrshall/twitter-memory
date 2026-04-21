"""MCP stdio server. Single tool: export_day."""
from __future__ import annotations

from datetime import date as date_cls

from mcp.server.fastmcp import FastMCP

from mcp_server import export, settings

mcp = FastMCP(name="twitter-memory")


@mcp.tool(
    name="export_day",
    description=(
        "Write a complete markdown report of one day's Twitter/X activity to "
        f"{settings.EXPORTS_DIR}/YYYY-MM-DD.md and return the file path plus "
        "inline content.\n\n"
        "Use this tool to answer any question about what the user saw, searched, "
        "or did on Twitter on a specific date. The day boundary follows the user's "
        "local machine timezone.\n\n"
        "Parameters:\n"
        "- date (required, YYYY-MM-DD): local calendar day to export.\n"
        "- exclude (optional, list of section names): omit those sections. "
        f"Allowed sections: {', '.join(settings.ALL_SECTIONS)}. Default: all sections included.\n\n"
        "Returns: { file_path, sections_included, tweet_count, interaction_count, "
        "session_count, search_count, byte_size, content, truncated }. "
        "If truncated=true, 'content' is empty and the caller should read file_path "
        "directly (the file on disk is always complete)."
    ),
)
def export_day(date: str, exclude: list[str] | None = None) -> dict:
    try:
        target = date_cls.fromisoformat(date)
    except ValueError as e:
        raise ValueError(f"Invalid date '{date}'. Expected YYYY-MM-DD. ({e})")
    if not settings.DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {settings.DB_PATH}. "
            "Is the backend running and has data been ingested?"
        )
    return export.write_export(settings.DB_PATH, target, exclude or [])


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
