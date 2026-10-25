"""OpenAPI 3.1 specification for the CONTINUUM sidecar wire protocol.

Provides a canonical OpenAPI 3.1 definition for all sidecar endpoints:
- Action lifecycle: intercept_action, complete_action, fail_action, reconcile_action, compensate_action
- Observability and progress: record_progress, list_actions
- Durability and recovery: checkpoint, validate, resume, confirm
"""

from __future__ import annotations

import json
from typing import Any, cast

OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "CONTINUUM Wire Protocol",
        "version": "1.0.0",
        "description": (
            "Language-agnostic wire protocol for CONTINUUM verifiable semantic recovery. "
            "Allows any client (Python, TypeScript, Go, etc.) to drive crash recovery, "
            "idempotent action claims, and checkpointing."
        ),
    },
    "servers": [{"url": "http://127.0.0.1:8765", "description": "Local sidecar default bind"}],
    "paths": {
        "/intercept_action": {
            "post": {
                "summary": "Claim an action idempotency lease",
                "description": (
                    "Atomically check whether an action has already run. If not, claims "
                    "execution rights; if previously completed, returns the cached result."
                ),
                "operationId": "interceptAction",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/InterceptActionRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Claim outcome or reusable result",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InterceptActionResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                    "500": {"$ref": "#/components/responses/500Internal"},
                },
            }
        },
        "/complete_action": {
            "post": {
                "summary": "Record action completion and durable result",
                "description": "Saves the completed outcome of a claimed action.",
                "operationId": "completeAction",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/CompleteActionRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Action completed successfully",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CompleteActionResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                    "500": {"$ref": "#/components/responses/500Internal"},
                },
            }
        },
        "/fail_action": {
            "post": {
                "summary": "Record action failure",
                "description": "Records that an action failed and did not produce a side effect.",
                "operationId": "failAction",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/FailActionRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Action marked failed",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ActionStatusResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                },
            }
        },
        "/record_progress": {
            "post": {
                "summary": "Record task progress and metrics",
                "description": "Records task progress metrics into the append-only event log.",
                "operationId": "recordProgress",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RecordProgressRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Progress accepted",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RecordProgressResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                },
            }
        },
        "/checkpoint": {
            "post": {
                "summary": "Create durable checkpoint",
                "description": "Snapshots semantic state, pins dependencies, and archives log head.",
                "operationId": "checkpoint",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/CheckpointRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Checkpoint recorded",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CheckpointResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                },
            }
        },
        "/validate": {
            "post": {
                "summary": "Validate environment and dependencies",
                "description": "Checks declared dependency pins against the active environment.",
                "operationId": "validate",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ValidateRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Validation result",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ValidateResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                },
            }
        },
        "/resume": {
            "post": {
                "summary": "Compute recovery resume decision",
                "description": "Projects event history into contract guidance and safe restart parameters.",
                "operationId": "resume",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ResumeRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Resume decision and contract",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ResumeResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                },
            }
        },
        "/confirm": {
            "post": {
                "summary": "Confirm human review",
                "description": (
                    "Confirms human review of a paused run. Requires a dedicated confirm "
                    "credential; normal client tokens are rejected with 403 Forbidden."
                ),
                "operationId": "confirm",
                "security": [{"ConfirmAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ConfirmRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Review confirmed and run unblocked",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ResumeResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                },
            }
        },
        "/list_actions": {
            "post": {
                "summary": "List action audit ledger",
                "description": "Returns all claimed and tracked actions for a run.",
                "operationId": "listActions",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RunIdRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Action list and unresolved counts",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ListActionsResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                },
            }
        },
        "/healthz": {
            "get": {
                "summary": "Health probe",
                "description": "Liveness check for container orchestration and load balancers.",
                "operationId": "healthz",
                "responses": {
                    "200": {
                        "description": "Service healthy",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string", "example": "ok"},
                                        "version": {"type": "string", "example": "1.0"},
                                    },
                                    "required": ["status", "version"],
                                }
                            }
                        },
                    }
                },
            }
        },
        "/openapi.json": {
            "get": {
                "summary": "OpenAPI Specification",
                "description": "Returns this OpenAPI 3.1 JSON definition.",
                "operationId": "getOpenApiSpec",
                "responses": {
                    "200": {
                        "description": "OpenAPI 3.1 specification document",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
    },
    "components": {
        "securitySchemes": {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Per-client authorization token (CONTINUUM_SERVE_CLIENT_TOKENS)",
            },
            "ConfirmAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Dedicated confirm credential (CONTINUUM_SERVE_CONFIRM_TOKEN)",
            },
        },
        "responses": {
            "400BadRequest": {
                "description": "Bad parameters or malformed JSON",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                },
            },
            "403Forbidden": {
                "description": "Missing or invalid authorization credential",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                },
            },
            "500Internal": {
                "description": "Server or storage execution error",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                },
            },
        },
        "schemas": {
            "ErrorResponse": {
                "type": "object",
                "properties": {
                    "error": {"type": "string", "description": "Human-readable error description"},
                    "code": {"type": "string", "description": "Canonical wire error code"},
                },
                "required": ["error", "code"],
            },
            "RunIdRequest": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
            "InterceptActionRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_type": {"type": "string"},
                    "arguments": {"type": "object"},
                    "key": {"type": "string"},
                    "scoped_to_run": {"type": "boolean", "default": True},
                    "grant": {"type": "object"},
                },
                "required": ["run_id", "action_type"],
            },
            "InterceptActionResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_id": {"type": "string"},
                    "action_type": {"type": "string"},
                    "action_key": {"type": "string"},
                    "proceed": {"type": "boolean"},
                    "status": {"type": "string"},
                    "reusable_result": {"type": "object"},
                    "previous_result": {"type": "object"},
                    "reconciliation_required": {"type": "boolean"},
                },
                "required": ["run_id", "action_type", "proceed"],
            },
            "CompleteActionRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_key": {"type": "string"},
                    "result": {},
                    "consumed_inputs": {"type": "object"},
                },
                "required": ["run_id", "action_key"],
            },
            "CompleteActionResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_key": {"type": "string"},
                    "status": {"type": "string", "enum": ["completed"]},
                    "result_hash": {"type": "string"},
                },
                "required": ["run_id", "action_key", "status"],
            },
            "FailActionRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_key": {"type": "string"},
                    "error": {"type": "string"},
                },
                "required": ["run_id", "action_key"],
            },
            "ActionStatusResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_key": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["run_id", "action_key", "status"],
            },
            "RecordProgressRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "completed": {"type": "integer"},
                    "total": {"type": "integer"},
                    "failed": {"type": "integer"},
                    "goal": {"type": "string"},
                    "notes": {"type": "string"},
                    "env": {},
                },
                "required": ["run_id"],
            },
            "RecordProgressResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "completed": {"type": "integer"},
                    "total": {"type": "integer"},
                    "failed": {"type": "integer"},
                    "pending": {"type": "integer"},
                    "source_sequence": {"type": "integer"},
                },
                "required": ["run_id", "completed"],
            },
            "CheckpointRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "env": {},
                },
                "required": ["run_id"],
            },
            "CheckpointResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "checkpoint_id": {"type": "string"},
                    "sequence": {"type": "integer"},
                    "hash": {"type": "string"},
                },
                "required": ["run_id", "checkpoint_id"],
            },
            "ValidateRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "expected_model": {"type": "string"},
                    "env": {},
                },
                "required": ["run_id"],
            },
            "ValidateResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "safe": {"type": "boolean"},
                    "mode": {"type": "string"},
                    "components": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["run_id", "safe", "mode"],
            },
            "ResumeRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "expected_model": {"type": "string"},
                    "env": {},
                },
                "required": ["run_id"],
            },
            "ResumeResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "mode": {"type": "string"},
                    "safe": {"type": "boolean"},
                    "next_allowed_action": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["run_id", "mode", "safe"],
            },
            "ConfirmRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "expected_model": {"type": "string"},
                },
                "required": ["run_id"],
            },
            "ListActionsResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "actions": {"type": "array", "items": {"type": "object"}},
                    "unresolved": {"type": "integer"},
                },
                "required": ["run_id", "actions", "unresolved"],
            },
        },
    },
}


def get_openapi_spec() -> dict[str, Any]:
    """Return the canonical OpenAPI 3.1 specification dictionary."""
    return cast(dict[str, Any], json.loads(json.dumps(OPENAPI_SPEC)))
