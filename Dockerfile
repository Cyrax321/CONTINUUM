FROM python:3.12-slim

COPY . /opt/continuum
# The [mcp] extra ships the SDK behind the continuum-mcp console script, so
# the published image can serve MCP clients too; without it the entry point
# exists but dies on the missing dependency (#838).
RUN pip install --no-cache-dir '/opt/continuum[mcp]' \
    && useradd --create-home continuum

# Default working directory is writable so the demo and CLI can create continuum.db.
WORKDIR /home/continuum
USER continuum

# No command given: run the crash-recovery demo end to end.
# Override it to use the CLI: docker run --rm ghcr.io/cyrax321/continuum continuum --help
# The MCP server likewise: docker run --rm ghcr.io/cyrax321/continuum continuum-mcp --help
CMD ["python", "/opt/continuum/examples/crash_recovery_agent.py"]
