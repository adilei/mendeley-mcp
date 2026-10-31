"""Tests for PDF downloads through the API client and MCP tool."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from mendeley_mcp import server
from mendeley_mcp.client import MendeleyClient

PDF = b"%PDF-1.4\nTest PDF content\n%%EOF\n"


def mock_transport(monkeypatch, handler):
    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )


@pytest.mark.asyncio
async def test_download_follows_redirects_without_forwarding_token(monkeypatch, mock_credentials):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.host != "api.mendeley.com":
            assert "Authorization" not in request.headers
        if request.url.path == "/files":
            assert request.url.params["document_id"] == "doc-123"
            return httpx.Response(200, json=[
                {"id": "text-file", "mime_type": "text/plain"},
                {"id": "pdf-file", "mime_type": "application/pdf"},
            ])
        if request.url.path == "/files/pdf-file":
            return httpx.Response(302, headers={"Location": "https://storage.example/download"})
        if request.url.path == "/download":
            return httpx.Response(307, headers={"Location": "https://cdn.example/paper.pdf"})
        assert request.url.path == "/paper.pdf"
        return httpx.Response(200, content=PDF)

    mock_transport(monkeypatch, handler)
    async with MendeleyClient(mock_credentials) as client:
        assert await client.get_file_content("doc-123") == PDF
    assert paths == ["/files", "/files/pdf-file", "/download", "/paper.pdf"]


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [[], [{"id": "text-file", "mime_type": "text/plain"}]])
async def test_download_without_pdf(monkeypatch, mock_credentials, files):
    mock_transport(monkeypatch, lambda request: httpx.Response(200, json=files))
    async with MendeleyClient(mock_credentials) as client:
        assert await client.get_file_content("doc-123") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", [None, "application/pdf"])
async def test_download_rejects_non_pdf_content(monkeypatch, mock_credentials, mime_type):
    def handler(request):
        if request.url.path == "/files":
            return httpx.Response(200, json=[
                {"id": "pdf-file", "file_name": "paper.PDF", "mime_type": mime_type},
            ])
        if request.url.path == "/files/pdf-file":
            return httpx.Response(302, headers={"Location": "https://storage.example/download"})
        return httpx.Response(200, content=b"<html>Error</html>")

    mock_transport(monkeypatch, handler)
    async with MendeleyClient(mock_credentials) as client:
        with pytest.raises(ValueError, match="not a PDF"):
            await client.get_file_content("doc-123")


@pytest.mark.asyncio
async def test_download_missing_location_is_error(monkeypatch, mock_credentials):
    def handler(request):
        if request.url.path == "/files":
            return httpx.Response(200, json=[{"id": "pdf-file", "mime_type": "application/pdf"}])
        return httpx.Response(200, json={})

    mock_transport(monkeypatch, handler)
    async with MendeleyClient(mock_credentials) as client:
        with pytest.raises(ValueError, match="download URL"):
            await client.get_file_content("doc-123")


@pytest.mark.asyncio
async def test_download_refreshes_expired_token(monkeypatch, mock_credentials):
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path == "/files":
            return httpx.Response(200, json=[{"id": "pdf-file", "mime_type": "application/pdf"}])
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "new-access"})
        if request.url.path == "/files/pdf-file":
            attempts += 1
            if attempts == 1:
                return httpx.Response(401)
            assert request.headers["Authorization"] == "Bearer new-access"
            return httpx.Response(302, headers={"Location": "https://storage.example/download"})
        return httpx.Response(200, content=PDF)

    mock_transport(monkeypatch, handler)
    async with MendeleyClient(mock_credentials) as client:
        assert await client.get_file_content("doc-123") == PDF
    assert attempts == 2


@pytest.mark.asyncio
async def test_download_http_error(monkeypatch, mock_credentials):
    mock_transport(monkeypatch, lambda request: httpx.Response(403))
    async with MendeleyClient(mock_credentials) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_file_content("doc-123")


@pytest.fixture
def download_client(monkeypatch):
    client = AsyncMock(spec=MendeleyClient)
    client.get_file_content.return_value = PDF
    monkeypatch.setattr(server, "get_client", AsyncMock(return_value=client))
    return client


@pytest.mark.asyncio
async def test_tool_saves_pdf(tmp_path, download_client):
    path = tmp_path / "paper.pdf"
    result = json.loads(await server.mendeley_download_pdf("doc-123", str(path)))
    assert result == {"document_id": "doc-123", "path": str(path), "size_bytes": len(PDF)}
    assert path.read_bytes() == PDF
    download_client.get_file_content.assert_awaited_once_with("doc-123")


@pytest.mark.asyncio
async def test_tool_does_not_overwrite(tmp_path, download_client):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"existing")
    result = json.loads(await server.mendeley_download_pdf("doc-123", str(path)))
    assert "already exists" in result["error"]
    assert path.read_bytes() == b"existing"
    download_client.get_file_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_does_not_follow_symlink(tmp_path, download_client):
    target = tmp_path / "target.pdf"
    path = tmp_path / "paper.pdf"
    path.symlink_to(target)
    result = json.loads(await server.mendeley_download_pdf("doc-123", str(path)))
    assert "already exists" in result["error"]
    assert not target.exists()
    download_client.get_file_content.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["paper.txt", "missing/paper.pdf", ""])
async def test_tool_invalid_destination(tmp_path, download_client, destination):
    path = str(tmp_path / destination) if destination else ""
    result = json.loads(await server.mendeley_download_pdf("doc-123", path))
    assert "error" in result
    download_client.get_file_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_without_pdf(tmp_path, download_client):
    download_client.get_file_content.return_value = None
    path = tmp_path / "paper.pdf"
    result = json.loads(await server.mendeley_download_pdf("doc-123", str(path)))
    assert "No PDF attachment" in result["error"]
    assert not path.exists()


@pytest.mark.asyncio
async def test_tool_reports_network_failure(tmp_path, download_client):
    download_client.get_file_content.side_effect = httpx.ConnectError("Connection failed")
    path = tmp_path / "paper.pdf"
    result = json.loads(await server.mendeley_download_pdf("doc-123", str(path)))
    assert result == {"error": "Connection failed"}
    assert not path.exists()


@pytest.mark.asyncio
async def test_tool_registered():
    tools = await server.mcp.list_tools()
    tool = next(tool for tool in tools if tool.name == "mendeley_download_pdf")
    assert tool.inputSchema["required"] == ["document_id", "output_path"]
