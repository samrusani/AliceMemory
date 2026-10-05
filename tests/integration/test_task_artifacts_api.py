from __future__ import annotations

import io
import json
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4
from xml.sax.saxutils import escape
import zipfile

import anyio
import psycopg

import alicebot_api.main as main_module
from alicebot_api.routers import legacy_gated as legacy_gated_router
from alicebot_api.routers import memories_legacy as memories_legacy_router
from alicebot_api.config import Settings
from alicebot_api.artifacts import TASK_ARTIFACT_CHUNK_RETRIEVAL_MATCHING_RULE
from alicebot_api.db import set_current_user, user_connection
from alicebot_api.store import ContinuityStore


def _escape_pdf_literal_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_pdf_bytes(
    pages: list[list[str]],
    *,
    compress_streams: bool = True,
    textless: bool = False,
) -> bytes:
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_refs: list[str] = []
    next_object_id = 4
    for page_lines in pages:
        page_object_id = next_object_id
        content_object_id = next_object_id + 1
        next_object_id += 2
        page_refs.append(f"{page_object_id} 0 R")

        if textless:
            content_stream = b"q 10 10 100 100 re S Q\n"
        else:
            commands = [b"BT", b"/F1 12 Tf", b"72 720 Td"]
            for index, line in enumerate(page_lines):
                if index > 0:
                    commands.append(b"T*")
                commands.append(f"({_escape_pdf_literal_string(line)}) Tj".encode("latin-1"))
            commands.append(b"ET")
            content_stream = b"\n".join(commands) + b"\n"

        if compress_streams:
            encoded_stream = zlib.compress(content_stream)
            content_body = (
                f"<< /Length {len(encoded_stream)} /Filter /FlateDecode >>\n".encode("ascii")
                + b"stream\n"
                + encoded_stream
                + b"\nendstream"
            )
        else:
            content_body = (
                f"<< /Length {len(content_stream)} >>\n".encode("ascii") + b"stream\n" + content_stream + b"endstream"
            )

        objects[page_object_id] = (
            f"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> "
            f"/MediaBox [0 0 612 792] /Contents {content_object_id} 0 R >>"
        ).encode("ascii")
        objects[content_object_id] = content_body

    objects[2] = (f"<< /Type /Pages /Count {len(page_refs)} /Kids [{' '.join(page_refs)}] >>").encode("ascii")

    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    max_object_id = max(objects)
    offsets = [0] * (max_object_id + 1)
    for object_id in range(1, max_object_id + 1):
        offsets[object_id] = len(document)
        document.extend(f"{object_id} 0 obj\n".encode("ascii"))
        document.extend(objects[object_id])
        document.extend(b"\nendobj\n")

    xref_offset = len(document)
    document.extend(f"xref\n0 {max_object_id + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for object_id in range(1, max_object_id + 1):
        document.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    document.extend(
        (f"trailer\n<< /Size {max_object_id + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n").encode("ascii")
    )
    return bytes(document)


def _build_docx_bytes(
    paragraphs: list[str],
    *,
    include_document_xml: bool = True,
    malformed_document_xml: bool = False,
) -> bytes:
    document_xml = (
        b"<w:document"
        if malformed_document_xml
        else (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            + "".join(
                (f'<w:p><w:r><w:t xml:space="preserve">{escape(paragraph)}</w:t></w:r></w:p>')
                for paragraph in paragraphs
            )
            + (
                "<w:sectPr>"
                '<w:pgSz w:w="12240" w:h="15840"/>'
                '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
                'w:header="708" w:footer="708" w:gutter="0"/>'
                "</w:sectPr>"
                "</w:body>"
                "</w:document>"
            )
        )
    )

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        entries = {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>"
            ).encode("utf-8"),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/>'
                "</Relationships>"
            ).encode("utf-8"),
        }
        if include_document_xml:
            entries["word/document.xml"] = document_xml

        for name, payload in entries.items():
            info = zipfile.ZipInfo(filename=name)
            info.date_time = (2026, 3, 13, 10, 0, 0)
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, payload)

    return archive_buffer.getvalue()


def _build_rfc822_email_bytes(
    *,
    headers: list[tuple[str, str]] | None = None,
    plain_body: str | None = None,
    plain_parts: list[str] | None = None,
    html_body: str | None = None,
    attachment_text: str | None = None,
    nested_message_bytes: bytes | None = None,
    malformed_multipart: bool = False,
) -> bytes:
    header_lines = [
        f"{name}: {value}"
        for name, value in (
            headers
            if headers is not None
            else [
                ("From", "Alice <alice@example.com>"),
                ("To", "Bob <bob@example.com>"),
                ("Subject", "Sprint Update"),
            ]
        )
    ]
    if malformed_multipart:
        return "\r\n".join(
            [
                *header_lines,
                "MIME-Version: 1.0",
                "Content-Type: multipart/mixed",
                "",
                "--broken-boundary",
                'Content-Type: text/plain; charset="utf-8"',
                "",
                "broken",
                "--broken-boundary--",
                "",
            ]
        ).encode("utf-8")

    if plain_parts is None and html_body is None and attachment_text is None and nested_message_bytes is None:
        return "\r\n".join(
            [
                *header_lines,
                'Content-Type: text/plain; charset="utf-8"',
                "Content-Transfer-Encoding: 8bit",
                "",
                plain_body or "",
            ]
        ).encode("utf-8")

    boundary = "alicebot-boundary-001"
    lines = [
        *header_lines,
        "MIME-Version: 1.0",
        f'Content-Type: multipart/mixed; boundary="{boundary}"',
        "",
    ]
    for part_text in plain_parts or []:
        lines.extend(
            [
                f"--{boundary}",
                'Content-Type: text/plain; charset="utf-8"',
                "Content-Transfer-Encoding: 8bit",
                "",
                part_text,
            ]
        )
    if html_body is not None:
        lines.extend(
            [
                f"--{boundary}",
                'Content-Type: text/html; charset="utf-8"',
                "Content-Transfer-Encoding: 8bit",
                "",
                html_body,
            ]
        )
    if attachment_text is not None:
        lines.extend(
            [
                f"--{boundary}",
                'Content-Type: text/plain; charset="utf-8"',
                'Content-Disposition: attachment; filename="note.txt"',
                "Content-Transfer-Encoding: 8bit",
                "",
                attachment_text,
            ]
        )
    if nested_message_bytes is not None:
        lines.extend(
            [
                f"--{boundary}",
                "Content-Type: message/rfc822",
                "Content-Transfer-Encoding: 8bit",
                "",
                nested_message_bytes.decode("utf-8"),
            ]
        )
    lines.extend([f"--{boundary}--", ""])
    return "\r\n".join(lines).encode("utf-8")


def invoke_request(
    method: str,
    path: str,
    *,
    query_params: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    encoded_body = b"" if payload is None else json.dumps(payload).encode()
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}

        request_received = True
        return {"type": "http.request", "body": encoded_body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    query_string = urlencode(query_params or {}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start_message["status"], json.loads(body)


def seed_task(database_url: str, *, email: str) -> dict[str, UUID]:
    user_id = uuid4()

    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, email, email.split("@", 1)[0].title())
        thread = store.create_thread("Artifact thread")
        tool = store.create_tool(
            tool_key="proxy.echo",
            name="Proxy Echo",
            description="Deterministic proxy handler.",
            version="1.0.0",
            metadata_version="tool_metadata_v0",
            active=True,
            tags=["proxy"],
            action_hints=["tool.run"],
            scope_hints=["workspace"],
            domain_hints=[],
            risk_hints=[],
            metadata={"transport": "proxy"},
        )
        task = store.create_task(
            thread_id=thread["id"],
            tool_id=tool["id"],
            status="approved",
            request={
                "thread_id": str(thread["id"]),
                "tool_id": str(tool["id"]),
                "action": "tool.run",
                "scope": "workspace",
                "domain_hint": None,
                "risk_hint": None,
                "attributes": {},
            },
            tool={
                "id": str(tool["id"]),
                "tool_key": "proxy.echo",
                "name": "Proxy Echo",
                "description": "Deterministic proxy handler.",
                "version": "1.0.0",
                "metadata_version": "tool_metadata_v0",
                "active": True,
                "tags": ["proxy"],
                "action_hints": ["tool.run"],
                "scope_hints": ["workspace"],
                "domain_hints": [],
                "risk_hints": [],
                "metadata": {"transport": "proxy"},
                "created_at": tool["created_at"].isoformat(),
            },
            latest_approval_id=None,
            latest_execution_id=None,
        )

    return {
        "user_id": user_id,
        "task_id": task["id"],
    }


def test_task_artifact_endpoints_register_list_detail_isolate_and_reject_duplicates(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task(migrated_database_urls["app"], email="intruder@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    first_file = workspace_path / "docs" / "spec.txt"
    first_file.parent.mkdir(parents=True)
    first_file.write_text("spec")
    second_file = workspace_path / "notes" / "plan.md"
    second_file.parent.mkdir(parents=True)
    second_file.write_text("plan")
    outside_file = tmp_path / "escape.txt"
    outside_file.write_text("escape")

    first_create_status, first_create_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(first_file),
            "media_type_hint": "text/plain",
        },
    )
    second_create_status, second_create_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(second_file),
            "media_type_hint": "text/markdown",
        },
    )
    list_status, list_payload = invoke_request(
        "GET",
        "/v0/task-artifacts",
        query_params={"user_id": str(owner["user_id"])},
    )
    detail_status, detail_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{first_create_payload['artifact']['id']}",
        query_params={"user_id": str(owner["user_id"])},
    )
    duplicate_status, duplicate_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(first_file),
            "media_type_hint": "text/plain",
        },
    )
    escaped_status, escaped_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(outside_file),
        },
    )
    isolated_list_status, isolated_list_payload = invoke_request(
        "GET",
        "/v0/task-artifacts",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_detail_status, isolated_detail_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{first_create_payload['artifact']['id']}",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_create_status, isolated_create_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(intruder["user_id"]),
            "local_path": str(first_file),
        },
    )

    assert first_create_status == 201
    assert first_create_payload == {
        "artifact": {
            "id": first_create_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "pending",
            "relative_path": "docs/spec.txt",
            "media_type_hint": "text/plain",
            "created_at": first_create_payload["artifact"]["created_at"],
            "updated_at": first_create_payload["artifact"]["updated_at"],
        }
    }

    assert second_create_status == 201
    assert second_create_payload == {
        "artifact": {
            "id": second_create_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "pending",
            "relative_path": "notes/plan.md",
            "media_type_hint": "text/markdown",
            "created_at": second_create_payload["artifact"]["created_at"],
            "updated_at": second_create_payload["artifact"]["updated_at"],
        }
    }

    assert list_status == 200
    assert list_payload == {
        "items": [
            first_create_payload["artifact"],
            second_create_payload["artifact"],
        ],
        "summary": {"total_count": 2, "order": ["created_at_asc", "id_asc"]},
    }

    assert detail_status == 200
    assert detail_payload == {"artifact": first_create_payload["artifact"]}

    assert duplicate_status == 409
    assert duplicate_payload == {
        "detail": {"code": "conflict", "message": "The request conflicts with the current resource state"}
    }

    assert escaped_status == 400
    assert escaped_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}

    assert isolated_list_status == 200
    assert isolated_list_payload == {
        "items": [],
        "summary": {"total_count": 0, "order": ["created_at_asc", "id_asc"]},
    }

    assert isolated_detail_status == 404
    assert isolated_detail_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }

    assert isolated_create_status == 404
    assert isolated_create_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_task_artifact_ingestion_and_chunk_endpoints_are_deterministic_and_isolated(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task(migrated_database_urls["app"], email="intruder@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    supported_file = workspace_path / "docs" / "spec.txt"
    supported_file.parent.mkdir(parents=True)
    supported_file.write_text(("A" * 998) + "\r\n" + ("B" * 5) + "\rC")
    unsupported_file = workspace_path / "docs" / "manual.bin"
    unsupported_file.write_bytes(b"\x00\x01\x02")

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(supported_file),
            "media_type_hint": "text/plain",
        },
    )
    assert register_status == 201

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    chunk_list_status, chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(owner["user_id"])},
    )
    isolated_chunk_list_status, isolated_chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_ingest_status, isolated_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(intruder["user_id"])},
    )

    unsupported_register_status, unsupported_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(unsupported_file),
            "media_type_hint": "application/octet-stream",
        },
    )
    assert unsupported_register_status == 201
    unsupported_ingest_status, unsupported_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{unsupported_register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )

    assert ingest_status == 200
    assert ingest_payload == {
        "artifact": {
            "id": register_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "ingested",
            "relative_path": "docs/spec.txt",
            "media_type_hint": "text/plain",
            "created_at": register_payload["artifact"]["created_at"],
            "updated_at": ingest_payload["artifact"]["updated_at"],
        },
        "summary": {
            "total_count": 2,
            "total_characters": 1006,
            "media_type": "text/plain",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }

    assert chunk_list_status == 200
    assert chunk_list_payload == {
        "items": [
            {
                "id": chunk_list_payload["items"][0]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": 1000,
                "text": ("A" * 998) + "\n" + "B",
                "created_at": chunk_list_payload["items"][0]["created_at"],
                "updated_at": chunk_list_payload["items"][0]["updated_at"],
            },
            {
                "id": chunk_list_payload["items"][1]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 2,
                "char_start": 1000,
                "char_end_exclusive": 1006,
                "text": "BBBB\nC",
                "created_at": chunk_list_payload["items"][1]["created_at"],
                "updated_at": chunk_list_payload["items"][1]["updated_at"],
            },
        ],
        "summary": {
            "total_count": 2,
            "total_characters": 1006,
            "media_type": "text/plain",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }

    assert isolated_chunk_list_status == 404
    assert isolated_chunk_list_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }

    assert isolated_ingest_status == 404
    assert isolated_ingest_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }

    assert unsupported_ingest_status == 400
    assert unsupported_ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_task_artifact_pdf_ingestion_and_chunk_endpoints_are_deterministic_and_isolated(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task(migrated_database_urls["app"], email="intruder@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    pdf_file = workspace_path / "docs" / "spec.pdf"
    pdf_file.parent.mkdir(parents=True)
    pdf_file.write_bytes(_build_pdf_bytes([["A" * 998, "B" * 5, "C"]]))

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(pdf_file),
            "media_type_hint": "application/pdf",
        },
    )
    assert register_status == 201

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    chunk_list_status, chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(owner["user_id"])},
    )
    isolated_chunk_list_status, isolated_chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_ingest_status, isolated_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(intruder["user_id"])},
    )

    assert ingest_status == 200
    assert ingest_payload == {
        "artifact": {
            "id": register_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "ingested",
            "relative_path": "docs/spec.pdf",
            "media_type_hint": "application/pdf",
            "created_at": register_payload["artifact"]["created_at"],
            "updated_at": ingest_payload["artifact"]["updated_at"],
        },
        "summary": {
            "total_count": 2,
            "total_characters": 1006,
            "media_type": "application/pdf",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }

    assert chunk_list_status == 200
    assert chunk_list_payload == {
        "items": [
            {
                "id": chunk_list_payload["items"][0]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": 1000,
                "text": ("A" * 998) + "\n" + "B",
                "created_at": chunk_list_payload["items"][0]["created_at"],
                "updated_at": chunk_list_payload["items"][0]["updated_at"],
            },
            {
                "id": chunk_list_payload["items"][1]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 2,
                "char_start": 1000,
                "char_end_exclusive": 1006,
                "text": "BBBB\nC",
                "created_at": chunk_list_payload["items"][1]["created_at"],
                "updated_at": chunk_list_payload["items"][1]["updated_at"],
            },
        ],
        "summary": {
            "total_count": 2,
            "total_characters": 1006,
            "media_type": "application/pdf",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }

    assert isolated_chunk_list_status == 404
    assert isolated_chunk_list_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }

    assert isolated_ingest_status == 404
    assert isolated_ingest_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_task_artifact_docx_ingestion_and_chunk_endpoints_are_deterministic_and_isolated(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task(migrated_database_urls["app"], email="intruder@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    docx_file = workspace_path / "docs" / "spec.docx"
    docx_file.parent.mkdir(parents=True)
    docx_file.write_bytes(_build_docx_bytes(["A" * 998, "B" * 5, "C"]))

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(docx_file),
            "media_type_hint": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
    )
    assert register_status == 201

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    chunk_list_status, chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(owner["user_id"])},
    )
    isolated_chunk_list_status, isolated_chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_ingest_status, isolated_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(intruder["user_id"])},
    )

    assert ingest_status == 200
    assert ingest_payload == {
        "artifact": {
            "id": register_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "ingested",
            "relative_path": "docs/spec.docx",
            "media_type_hint": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "created_at": register_payload["artifact"]["created_at"],
            "updated_at": ingest_payload["artifact"]["updated_at"],
        },
        "summary": {
            "total_count": 2,
            "total_characters": 1006,
            "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }

    assert chunk_list_status == 200
    assert chunk_list_payload == {
        "items": [
            {
                "id": chunk_list_payload["items"][0]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": 1000,
                "text": ("A" * 998) + "\n" + "B",
                "created_at": chunk_list_payload["items"][0]["created_at"],
                "updated_at": chunk_list_payload["items"][0]["updated_at"],
            },
            {
                "id": chunk_list_payload["items"][1]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 2,
                "char_start": 1000,
                "char_end_exclusive": 1006,
                "text": "BBBB\nC",
                "created_at": chunk_list_payload["items"][1]["created_at"],
                "updated_at": chunk_list_payload["items"][1]["updated_at"],
            },
        ],
        "summary": {
            "total_count": 2,
            "total_characters": 1006,
            "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }

    assert isolated_chunk_list_status == 404
    assert isolated_chunk_list_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }

    assert isolated_ingest_status == 404
    assert isolated_ingest_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_task_artifact_rfc822_ingestion_and_chunk_endpoints_are_deterministic_and_isolated(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task(migrated_database_urls["app"], email="intruder@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    email_file = workspace_path / "mail" / "update.eml"
    email_file.parent.mkdir(parents=True)
    email_file.write_bytes(
        _build_rfc822_email_bytes(
            plain_body=("A" * 916) + "\r\n" + ("B" * 5) + "\rC",
        )
    )

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(email_file),
            "media_type_hint": "message/rfc822",
        },
    )
    assert register_status == 201

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    chunk_list_status, chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(owner["user_id"])},
    )
    isolated_chunk_list_status, isolated_chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(intruder["user_id"])},
    )
    isolated_ingest_status, isolated_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(intruder["user_id"])},
    )

    header_block = "From: Alice <alice@example.com>\nTo: Bob <bob@example.com>\nSubject: Sprint Update\n\n"
    assert ingest_status == 200
    assert ingest_payload == {
        "artifact": {
            "id": register_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "ingested",
            "relative_path": "mail/update.eml",
            "media_type_hint": "message/rfc822",
            "created_at": register_payload["artifact"]["created_at"],
            "updated_at": ingest_payload["artifact"]["updated_at"],
        },
        "summary": {
            "total_count": 2,
            "total_characters": 1006,
            "media_type": "message/rfc822",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }

    assert chunk_list_status == 200
    assert chunk_list_payload == {
        "items": [
            {
                "id": chunk_list_payload["items"][0]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": 1000,
                "text": header_block + ("A" * 916) + "\n" + "B",
                "created_at": chunk_list_payload["items"][0]["created_at"],
                "updated_at": chunk_list_payload["items"][0]["updated_at"],
            },
            {
                "id": chunk_list_payload["items"][1]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 2,
                "char_start": 1000,
                "char_end_exclusive": 1006,
                "text": "BBBB\nC",
                "created_at": chunk_list_payload["items"][1]["created_at"],
                "updated_at": chunk_list_payload["items"][1]["updated_at"],
            },
        ],
        "summary": {
            "total_count": 2,
            "total_characters": 1006,
            "media_type": "message/rfc822",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }

    assert isolated_chunk_list_status == 404
    assert isolated_chunk_list_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }

    assert isolated_ingest_status == 404
    assert isolated_ingest_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }


def test_task_artifact_rfc822_ingestion_excludes_nested_email_bodies(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    email_file = workspace_path / "mail" / "forwarded.eml"
    email_file.parent.mkdir(parents=True)
    email_file.write_bytes(
        _build_rfc822_email_bytes(
            plain_parts=["Outer body"],
            nested_message_bytes=_build_rfc822_email_bytes(
                headers=[
                    ("From", "Nested <nested@example.com>"),
                    ("To", "Team <team@example.com>"),
                    ("Subject", "Nested"),
                ],
                plain_body="Inner body",
            ),
        )
    )

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(email_file),
            "media_type_hint": "message/rfc822",
        },
    )
    assert register_status == 201

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    chunk_list_status, chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(owner["user_id"])},
    )

    expected_text = "From: Alice <alice@example.com>\nTo: Bob <bob@example.com>\nSubject: Sprint Update\n\nOuter body"
    assert ingest_status == 200
    assert ingest_payload == {
        "artifact": {
            "id": register_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "ingested",
            "relative_path": "mail/forwarded.eml",
            "media_type_hint": "message/rfc822",
            "created_at": register_payload["artifact"]["created_at"],
            "updated_at": ingest_payload["artifact"]["updated_at"],
        },
        "summary": {
            "total_count": 1,
            "total_characters": len(expected_text),
            "media_type": "message/rfc822",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }

    assert chunk_list_status == 200
    assert chunk_list_payload == {
        "items": [
            {
                "id": chunk_list_payload["items"][0]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": len(expected_text),
                "text": expected_text,
                "created_at": chunk_list_payload["items"][0]["created_at"],
                "updated_at": chunk_list_payload["items"][0]["updated_at"],
            }
        ],
        "summary": {
            "total_count": 1,
            "total_characters": len(expected_text),
            "media_type": "message/rfc822",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }


def test_task_artifact_ingestion_supports_markdown_and_reingest_is_idempotent(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    markdown_file = workspace_path / "notes" / "plan.md"
    markdown_file.parent.mkdir(parents=True)
    markdown_file.write_text("# Plan\r\n\r\n- Ship ingestion\n- Keep scope narrow\r")

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(markdown_file),
            "media_type_hint": "text/markdown",
        },
    )
    assert register_status == 201

    first_ingest_status, first_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    second_ingest_status, second_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    chunk_list_status, chunk_list_payload = invoke_request(
        "GET",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/chunks",
        query_params={"user_id": str(owner["user_id"])},
    )

    assert first_ingest_status == 200
    assert first_ingest_payload == {
        "artifact": {
            "id": register_payload["artifact"]["id"],
            "task_id": str(owner["task_id"]),
            "task_workspace_id": workspace_payload["workspace"]["id"],
            "status": "registered",
            "ingestion_status": "ingested",
            "relative_path": "notes/plan.md",
            "media_type_hint": "text/markdown",
            "created_at": register_payload["artifact"]["created_at"],
            "updated_at": first_ingest_payload["artifact"]["updated_at"],
        },
        "summary": {
            "total_count": 1,
            "total_characters": 45,
            "media_type": "text/markdown",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }
    assert second_ingest_status == 200
    assert second_ingest_payload == first_ingest_payload
    assert chunk_list_status == 200
    assert chunk_list_payload == {
        "items": [
            {
                "id": chunk_list_payload["items"][0]["id"],
                "task_artifact_id": register_payload["artifact"]["id"],
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": 45,
                "text": "# Plan\n\n- Ship ingestion\n- Keep scope narrow\n",
                "created_at": chunk_list_payload["items"][0]["created_at"],
                "updated_at": chunk_list_payload["items"][0]["updated_at"],
            }
        ],
        "summary": {
            "total_count": 1,
            "total_characters": 45,
            "media_type": "text/markdown",
            "chunking_rule": "normalized_utf8_text_fixed_window_1000_chars_v1",
            "order": ["sequence_no_asc", "id_asc"],
        },
    }


def test_task_artifact_ingestion_rejects_invalid_utf8_content(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    broken_file = workspace_path / "docs" / "broken.txt"
    broken_file.parent.mkdir(parents=True)
    broken_file.write_bytes(b"\xff\xfe\xfd")

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(broken_file),
            "media_type_hint": "text/plain",
        },
    )
    assert register_status == 201

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )

    assert ingest_status == 400
    assert ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_task_artifact_ingestion_rejects_textless_pdf_content(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    textless_pdf = workspace_path / "docs" / "scanned.pdf"
    textless_pdf.parent.mkdir(parents=True)
    textless_pdf.write_bytes(_build_pdf_bytes([[]], textless=True))

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(textless_pdf),
            "media_type_hint": "application/pdf",
        },
    )
    assert register_status == 201

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )

    assert ingest_status == 400
    assert ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_task_artifact_ingestion_rejects_textless_or_malformed_docx(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    textless_docx = workspace_path / "docs" / "empty.docx"
    textless_docx.parent.mkdir(parents=True)
    textless_docx.write_bytes(_build_docx_bytes(["", ""]))
    malformed_docx = workspace_path / "docs" / "broken.docx"
    malformed_docx.write_bytes(_build_docx_bytes(["broken"], malformed_document_xml=True))

    textless_register_status, textless_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(textless_docx),
            "media_type_hint": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
    )
    malformed_register_status, malformed_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(malformed_docx),
            "media_type_hint": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
    )
    assert textless_register_status == 201
    assert malformed_register_status == 201

    textless_ingest_status, textless_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{textless_register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    malformed_ingest_status, malformed_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{malformed_register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )

    assert textless_ingest_status == 400
    assert textless_ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert malformed_ingest_status == 400
    assert malformed_ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_task_artifact_ingestion_rejects_textless_or_malformed_rfc822_email(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    textless_email = workspace_path / "mail" / "empty.eml"
    textless_email.parent.mkdir(parents=True)
    textless_email.write_bytes(_build_rfc822_email_bytes(html_body="<p>html only</p>"))
    malformed_email = workspace_path / "mail" / "broken.eml"
    malformed_email.write_bytes(_build_rfc822_email_bytes(malformed_multipart=True))

    textless_register_status, textless_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(textless_email),
            "media_type_hint": "message/rfc822",
        },
    )
    malformed_register_status, malformed_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(malformed_email),
            "media_type_hint": "message/rfc822",
        },
    )
    assert textless_register_status == 201
    assert malformed_register_status == 201

    textless_ingest_status, textless_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{textless_register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    malformed_ingest_status, malformed_ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{malformed_register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )

    assert textless_ingest_status == 400
    assert textless_ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}
    assert malformed_ingest_status == 400
    assert malformed_ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_task_artifact_ingestion_enforces_rooted_workspace_paths(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    safe_file = workspace_path / "docs" / "spec.pdf"
    safe_file.parent.mkdir(parents=True)
    safe_file.write_bytes(_build_pdf_bytes([["spec"]]))
    outside_file = tmp_path / "escape.pdf"
    outside_file.write_bytes(_build_pdf_bytes([["escape"]]))

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(safe_file),
            "media_type_hint": "application/pdf",
        },
    )
    assert register_status == 201

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        set_current_user(conn, owner["user_id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE task_artifacts
                SET relative_path = '../../../escape.pdf'
                WHERE id = %s
                """,
                (register_payload["artifact"]["id"],),
            )
            conn.commit()

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )

    assert ingest_status == 400
    assert ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_task_artifact_docx_ingestion_enforces_rooted_workspace_paths(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    safe_file = workspace_path / "docs" / "spec.docx"
    safe_file.parent.mkdir(parents=True)
    safe_file.write_bytes(_build_docx_bytes(["spec"]))
    outside_file = tmp_path / "escape.docx"
    outside_file.write_bytes(_build_docx_bytes(["escape"]))

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(safe_file),
            "media_type_hint": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
    )
    assert register_status == 201

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        set_current_user(conn, owner["user_id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE task_artifacts
                SET relative_path = '../../../escape.docx'
                WHERE id = %s
                """,
                (register_payload["artifact"]["id"],),
            )
            conn.commit()

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )

    assert ingest_status == 400
    assert ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_task_artifact_rfc822_ingestion_enforces_rooted_workspace_paths(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    workspace_status, workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert workspace_status == 201

    workspace_path = Path(workspace_payload["workspace"]["local_path"])
    safe_file = workspace_path / "mail" / "update.eml"
    safe_file.parent.mkdir(parents=True)
    safe_file.write_bytes(_build_rfc822_email_bytes(plain_body="spec"))
    outside_file = tmp_path / "escape.eml"
    outside_file.write_bytes(_build_rfc822_email_bytes(plain_body="escape"))

    register_status, register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(safe_file),
            "media_type_hint": "message/rfc822",
        },
    )
    assert register_status == 201

    with psycopg.connect(migrated_database_urls["admin"]) as conn:
        set_current_user(conn, owner["user_id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE task_artifacts
                SET relative_path = '../../../escape.eml'
                WHERE id = %s
                """,
                (register_payload["artifact"]["id"],),
            )
            conn.commit()

    ingest_status, ingest_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )

    assert ingest_status == 400
    assert ingest_payload == {"detail": {"code": "invalid_request", "message": "The request is invalid"}}


def test_task_artifact_chunk_retrieval_endpoints_are_scoped_deterministic_and_isolated(
    migrated_database_urls,
    monkeypatch,
    tmp_path,
) -> None:
    owner = seed_task(migrated_database_urls["app"], email="owner@example.com")
    intruder = seed_task(migrated_database_urls["app"], email="intruder@example.com")
    workspace_root = tmp_path / "task-workspaces"
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        legacy_gated_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )
    monkeypatch.setattr(
        memories_legacy_router,
        "get_settings",
        lambda: Settings(
            database_url=migrated_database_urls["app"],
            task_workspace_root=str(workspace_root),
        ),
    )

    owner_workspace_status, owner_workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/workspace",
        payload={"user_id": str(owner["user_id"])},
    )
    assert owner_workspace_status == 201
    owner_workspace_path = Path(owner_workspace_payload["workspace"]["local_path"])

    docs_file = owner_workspace_path / "docs" / "a.txt"
    docs_file.parent.mkdir(parents=True)
    docs_file.write_text("beta alpha doc")
    notes_file = owner_workspace_path / "notes" / "b.md"
    notes_file.parent.mkdir(parents=True)
    notes_file.write_text("alpha beta note")
    weak_file = owner_workspace_path / "notes" / "c.txt"
    weak_file.write_text("beta only")
    pending_file = owner_workspace_path / "notes" / "hidden.txt"
    pending_file.write_text("alpha beta hidden")

    docs_register_status, docs_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{owner_workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(docs_file),
            "media_type_hint": "text/plain",
        },
    )
    notes_register_status, notes_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{owner_workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(notes_file),
            "media_type_hint": "text/markdown",
        },
    )
    weak_register_status, weak_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{owner_workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(weak_file),
            "media_type_hint": "text/plain",
        },
    )
    pending_register_status, pending_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{owner_workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(owner["user_id"]),
            "local_path": str(pending_file),
            "media_type_hint": "text/plain",
        },
    )
    assert docs_register_status == 201
    assert notes_register_status == 201
    assert weak_register_status == 201
    assert pending_register_status == 201

    docs_ingest_status, _ = invoke_request(
        "POST",
        f"/v0/task-artifacts/{docs_register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    notes_ingest_status, _ = invoke_request(
        "POST",
        f"/v0/task-artifacts/{notes_register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    weak_ingest_status, _ = invoke_request(
        "POST",
        f"/v0/task-artifacts/{weak_register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(owner["user_id"])},
    )
    assert docs_ingest_status == 200
    assert notes_ingest_status == 200
    assert weak_ingest_status == 200

    with user_connection(migrated_database_urls["app"], owner["user_id"]) as conn:
        store = ContinuityStore(conn)
        store.create_task_artifact_chunk(
            task_artifact_id=UUID(pending_register_payload["artifact"]["id"]),
            sequence_no=1,
            char_start=0,
            char_end_exclusive=17,
            text="alpha beta hidden",
        )

    intruder_workspace_status, intruder_workspace_payload = invoke_request(
        "POST",
        f"/v0/tasks/{intruder['task_id']}/workspace",
        payload={"user_id": str(intruder["user_id"])},
    )
    assert intruder_workspace_status == 201
    intruder_workspace_path = Path(intruder_workspace_payload["workspace"]["local_path"])
    intruder_file = intruder_workspace_path / "docs" / "secret.txt"
    intruder_file.parent.mkdir(parents=True)
    intruder_file.write_text("alpha beta intruder")

    intruder_register_status, intruder_register_payload = invoke_request(
        "POST",
        f"/v0/task-workspaces/{intruder_workspace_payload['workspace']['id']}/artifacts",
        payload={
            "user_id": str(intruder["user_id"]),
            "local_path": str(intruder_file),
            "media_type_hint": "text/plain",
        },
    )
    assert intruder_register_status == 201
    intruder_ingest_status, _ = invoke_request(
        "POST",
        f"/v0/task-artifacts/{intruder_register_payload['artifact']['id']}/ingest",
        payload={"user_id": str(intruder["user_id"])},
    )
    assert intruder_ingest_status == 200

    task_retrieve_status, task_retrieve_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/retrieve",
        payload={"user_id": str(owner["user_id"]), "query": "Alpha beta"},
    )
    artifact_retrieve_status, artifact_retrieve_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{notes_register_payload['artifact']['id']}/chunks/retrieve",
        payload={"user_id": str(owner["user_id"]), "query": "Alpha beta"},
    )
    empty_retrieve_status, empty_retrieve_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/retrieve",
        payload={"user_id": str(owner["user_id"]), "query": "missing"},
    )
    isolated_task_retrieve_status, isolated_task_retrieve_payload = invoke_request(
        "POST",
        f"/v0/tasks/{owner['task_id']}/artifact-chunks/retrieve",
        payload={"user_id": str(intruder["user_id"]), "query": "Alpha beta"},
    )
    isolated_artifact_retrieve_status, isolated_artifact_retrieve_payload = invoke_request(
        "POST",
        f"/v0/task-artifacts/{notes_register_payload['artifact']['id']}/chunks/retrieve",
        payload={"user_id": str(intruder["user_id"]), "query": "Alpha beta"},
    )

    assert task_retrieve_status == 200
    assert task_retrieve_payload == {
        "items": [
            {
                "id": task_retrieve_payload["items"][0]["id"],
                "task_id": str(owner["task_id"]),
                "task_artifact_id": docs_register_payload["artifact"]["id"],
                "relative_path": "docs/a.txt",
                "media_type": "text/plain",
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": 14,
                "text": "beta alpha doc",
                "match": {
                    "matched_query_terms": ["alpha", "beta"],
                    "matched_query_term_count": 2,
                    "first_match_char_start": 0,
                },
            },
            {
                "id": task_retrieve_payload["items"][1]["id"],
                "task_id": str(owner["task_id"]),
                "task_artifact_id": notes_register_payload["artifact"]["id"],
                "relative_path": "notes/b.md",
                "media_type": "text/markdown",
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": 15,
                "text": "alpha beta note",
                "match": {
                    "matched_query_terms": ["alpha", "beta"],
                    "matched_query_term_count": 2,
                    "first_match_char_start": 0,
                },
            },
            {
                "id": task_retrieve_payload["items"][2]["id"],
                "task_id": str(owner["task_id"]),
                "task_artifact_id": weak_register_payload["artifact"]["id"],
                "relative_path": "notes/c.txt",
                "media_type": "text/plain",
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": 9,
                "text": "beta only",
                "match": {
                    "matched_query_terms": ["beta"],
                    "matched_query_term_count": 1,
                    "first_match_char_start": 0,
                },
            },
        ],
        "summary": {
            "total_count": 3,
            "searched_artifact_count": 3,
            "query": "Alpha beta",
            "query_terms": ["alpha", "beta"],
            "matching_rule": TASK_ARTIFACT_CHUNK_RETRIEVAL_MATCHING_RULE,
            "order": [
                "matched_query_term_count_desc",
                "first_match_char_start_asc",
                "relative_path_asc",
                "sequence_no_asc",
                "id_asc",
            ],
            "scope": {
                "kind": "task",
                "task_id": str(owner["task_id"]),
            },
        },
    }

    assert artifact_retrieve_status == 200
    assert artifact_retrieve_payload == {
        "items": [
            {
                "id": artifact_retrieve_payload["items"][0]["id"],
                "task_id": str(owner["task_id"]),
                "task_artifact_id": notes_register_payload["artifact"]["id"],
                "relative_path": "notes/b.md",
                "media_type": "text/markdown",
                "sequence_no": 1,
                "char_start": 0,
                "char_end_exclusive": 15,
                "text": "alpha beta note",
                "match": {
                    "matched_query_terms": ["alpha", "beta"],
                    "matched_query_term_count": 2,
                    "first_match_char_start": 0,
                },
            }
        ],
        "summary": {
            "total_count": 1,
            "searched_artifact_count": 1,
            "query": "Alpha beta",
            "query_terms": ["alpha", "beta"],
            "matching_rule": TASK_ARTIFACT_CHUNK_RETRIEVAL_MATCHING_RULE,
            "order": [
                "matched_query_term_count_desc",
                "first_match_char_start_asc",
                "relative_path_asc",
                "sequence_no_asc",
                "id_asc",
            ],
            "scope": {
                "kind": "artifact",
                "task_id": str(owner["task_id"]),
                "task_artifact_id": notes_register_payload["artifact"]["id"],
            },
        },
    }

    assert empty_retrieve_status == 200
    assert empty_retrieve_payload == {
        "items": [],
        "summary": {
            "total_count": 0,
            "searched_artifact_count": 3,
            "query": "missing",
            "query_terms": ["missing"],
            "matching_rule": TASK_ARTIFACT_CHUNK_RETRIEVAL_MATCHING_RULE,
            "order": [
                "matched_query_term_count_desc",
                "first_match_char_start_asc",
                "relative_path_asc",
                "sequence_no_asc",
                "id_asc",
            ],
            "scope": {
                "kind": "task",
                "task_id": str(owner["task_id"]),
            },
        },
    }

    assert isolated_task_retrieve_status == 404
    assert isolated_task_retrieve_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }

    assert isolated_artifact_retrieve_status == 404
    assert isolated_artifact_retrieve_payload == {
        "detail": {"code": "not_found", "message": "The requested resource was not found"}
    }
