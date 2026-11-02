"""OpenAPI 3.1 specification for the CONTINUUM sidecar wire protocol.

Provides a canonical OpenAPI 3.1 definition for all sidecar endpoints:
- Action lifecycle: intercept_action, complete_action, fail_action, reconcile_action, compensate_action
- Observability and progress: record_progress, list_actions
- Durability and recovery: checkpoint, validate, resume, confirm

The document describes the wire the reference sidecar actually speaks. Every
``required`` list, property name and nullability below is grounded in a handler
in ``server.py``, and ``tests/test_openapi_wire_drift.py`` drives each endpoint
and compares the two, so the contract and the implementation cannot drift apart
again (issue #1894).
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
                    "execution rights; if previously completed, returns the cached result; "
                    "if a previous attempt was interrupted, refuses to proceed and asks "
                    "for external reconciliation."
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
                        "description": "Claim outcome, cached result, or refusal to proceed",
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
                "description": (
                    "Records that an action failed and produced no side effect, "
                    "or (with ``certain`` unset) that the outcome is uncertain."
                ),
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
                    "500": {"$ref": "#/components/responses/500Internal"},
                },
            }
        },
        "/reconcile_action": {
            "post": {
                "summary": "Resolve an uncertain action from external evidence",
                "description": (
                    "Records what the outside world says about an action whose outcome "
                    "the run could not determine. ``occurred`` is the evidence; the "
                    "action becomes completed or failed from it and is never retried "
                    "blindly."
                ),
                "operationId": "reconcileAction",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ReconcileActionRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Action settled from external evidence",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ReconcileActionResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                    "500": {"$ref": "#/components/responses/500Internal"},
                },
            }
        },
        "/compensate_action": {
            "post": {
                "summary": "Register a compensating rollback for a completed action",
                "description": (
                    "Records that a completed action was undone by a compensating "
                    "action, so later recovery can see the effect was reversed."
                ),
                "operationId": "compensateAction",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/CompensateActionRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Compensation recorded against the action",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CompensateActionResponse"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/400BadRequest"},
                    "403": {"$ref": "#/components/responses/403Forbidden"},
                    "500": {"$ref": "#/components/responses/500Internal"},
                },
            }
        },
        "/record_progress": {
            "post": {
                "summary": "Record task progress and metrics",
                "description": (
                    "Records task progress counters into the append-only event log. "
                    "The first call for a run creates it and must carry ``goal``; later "
                    "calls only need ``run_id`` and ``completed``."
                ),
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
                "description": (
                    "Projects event history into contract guidance and safe restart "
                    "parameters. ``run_id`` may be omitted: a session that was "
                    "interrupted and restarted has no id to send, so an absent one "
                    'means "the run that was interrupted" and the server answers '
                    "with mode ``no_active_run`` when there is none."
                ),
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
                                "schema": {"$ref": "#/components/schemas/ConfirmResponse"}
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
                                "schema": {"$ref": "#/components/schemas/HealthResponse"}
                            }
                        },
                    }
                },
            }
        },
        "/readyz": {
            "get": {
                "summary": "Readiness probe",
                "description": (
                    "Readiness check, answered identically to /healthz so a probe can use either."
                ),
                "operationId": "readyz",
                "responses": {
                    "200": {
                        "description": "Service ready",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/HealthResponse"}
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
        "/spec": {
            "get": {
                "summary": "OpenAPI Specification alias",
                "description": "Alias for /openapi.json, so clients used to either path work.",
                "operationId": "getSpecAlias",
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
            "HealthResponse": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "example": "ok"},
                    "version": {"type": "string", "example": "1.0"},
                },
                "required": ["status", "version"],
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
                    "arguments": {
                        "type": "object",
                        "description": "Arguments the idempotency key is derived from.",
                    },
                    "key": {
                        "type": "string",
                        "description": (
                            "Explicit idempotency key. Omitted means the key is derived "
                            "from action_type and arguments."
                        ),
                    },
                    "scoped_to_run": {"type": "boolean", "default": True},
                    "grant": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "scope": {"type": "string"},
                        },
                        "description": "Single-use authority attached to this attempt.",
                    },
                },
                "required": ["run_id", "action_type"],
            },
            "InterceptActionResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_type": {"type": "string"},
                    "proceed": {
                        "type": "boolean",
                        "description": "True when the caller may perform the action.",
                    },
                    "action_key": {
                        "type": "string",
                        "description": (
                            "Idempotency key of the claimed or completed action; pass it "
                            "to complete_action, fail_action or reconcile_action. Absent "
                            "on refusal paths that carry no key (grant denied)."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "description": (
                            "Ledger status of the action this claim resolved "
                            "(started, completed, unknown)."
                        ),
                    },
                    "external_id": {
                        "type": ["string", "null"],
                        "description": "Caller-supplied id of the external effect, if any.",
                    },
                    "previous_result": {
                        "type": ["object", "null"],
                        "description": "Cached result of an already-completed action.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the claim refuses to proceed, if it does.",
                    },
                    "reason_code": {
                        "type": "string",
                        "enum": ["grant_denied"],
                        "description": "Canonical refusal reason, if the claim was refused.",
                    },
                    "grant_id": {
                        "type": "string",
                        "description": "The consumed single-use authority, on a grant refusal.",
                    },
                    "guidance": {
                        "type": "string",
                        "description": "What the caller should do next.",
                    },
                },
                "required": ["run_id", "action_type", "proceed"],
            },
            "CompleteActionRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_key": {"type": "string"},
                    "result": {
                        "description": (
                            "The action's outcome. Read back by a later claim as "
                            "previous_result; any JSON value is recorded."
                        )
                    },
                    "external_id": {
                        "type": ["string", "null"],
                        "description": "Id of the external effect this completion produced.",
                    },
                    "consumed_inputs": {"type": "object"},
                },
                "required": ["run_id", "action_key"],
            },
            "CompleteActionResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_id": {"type": "string"},
                    "action_type": {"type": "string"},
                    "status": {"type": "string", "enum": ["completed"]},
                    "external_id": {
                        "type": ["string", "null"],
                        "description": "The external id recorded with the completion, if any.",
                    },
                },
                "required": ["run_id", "action_id", "status"],
            },
            "FailActionRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_key": {"type": "string"},
                    "error": {"type": "string", "description": "Why the action failed."},
                    "certain": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "True asserts no side effect happened. False (the default) "
                            "records an uncertain outcome instead of a clean failure."
                        ),
                    },
                },
                "required": ["run_id", "action_key", "error"],
            },
            "ActionStatusResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["failed", "unknown"],
                        "description": (
                            "failed when the failure is certain, unknown when the "
                            "side effect may still have landed."
                        ),
                    },
                    "side_effect_uncertain": {
                        "type": "boolean",
                        "description": "True when the failure did not rule out a side effect.",
                    },
                },
                "required": ["run_id", "action_id", "status"],
            },
            "ReconcileActionRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_key": {"type": "string"},
                    "occurred": {
                        "type": "boolean",
                        "description": "External evidence: the effect happened, or did not.",
                    },
                    "external_id": {
                        "type": ["string", "null"],
                        "description": "Receipt for the effect, when it occurred.",
                    },
                    "note": {"type": "string", "default": ""},
                    "consumed_inputs": {"type": "object"},
                },
                "required": ["run_id", "action_key", "occurred"],
            },
            "ReconcileActionResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["completed", "failed"],
                        "description": "Settled from the evidence: completed or failed.",
                    },
                    "external_id": {
                        "type": ["string", "null"],
                        "description": "The external id kept on record, if any.",
                    },
                    "side_effect_uncertain": {"type": "boolean"},
                },
                "required": ["run_id", "action_id", "status"],
            },
            "CompensateActionRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_key": {"type": "string"},
                    "note": {
                        "type": "string",
                        "default": "",
                        "description": "Why the compensation was needed.",
                    },
                    "by": {
                        "type": ["string", "null"],
                        "description": "Who or what performed the compensation.",
                    },
                },
                "required": ["run_id", "action_key"],
            },
            "CompensateActionResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "action_id": {"type": "string"},
                    "action_type": {"type": "string"},
                    "status": {"type": "string", "enum": ["compensated"]},
                    "compensated_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ids of the compensating actions recorded so far.",
                    },
                },
                "required": ["run_id", "action_id", "status"],
            },
            "RecordProgressRequest": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "completed": {"type": "integer"},
                    "total": {"type": "integer"},
                    "failed": {"type": "integer", "default": 0},
                    "goal": {
                        "type": ["string", "null"],
                        "description": "Task goal; required on the first call, which creates the run.",
                    },
                    "env": {
                        "description": (
                            "Pinned environment as a mapping of resource to version, or "
                            "a list of name=version strings."
                        )
                    },
                },
                "required": ["run_id", "completed"],
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
                    "reason": {"type": "string", "default": ""},
                    "env": {},
                },
                "required": ["run_id"],
            },
            "CheckpointResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "checkpoint_id": {"type": "string"},
                    "version": {
                        "type": "integer",
                        "description": "How many checkpoints this run has recorded.",
                    },
                    "trigger": {
                        "type": "string",
                        "description": "Why the checkpoint was taken (manual, compaction, ...).",
                    },
                    "integrity_hash": {
                        "type": "string",
                        "description": "Hash of the checkpointed state.",
                    },
                    "completed": {"type": "integer"},
                    "source_sequence": {
                        "type": "integer",
                        "description": "Event sequence the checkpoint was taken at.",
                    },
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
                    "checkpoint_version": {"type": "integer"},
                    "reason": {"type": "string"},
                    "components": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "component": {"type": "string"},
                                "component_id": {"type": ["string", "null"]},
                                "status": {"type": "string"},
                                "detail": {"type": "string"},
                            },
                        },
                    },
                    "environment_changes": {"type": "array", "items": {"type": "string"}},
                    "constraint_pins": {"type": "object"},
                    "liveness": {"type": "object"},
                },
                "required": ["run_id", "safe", "mode"],
            },
            "ResumeRequest": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": ["string", "null"],
                        "description": (
                            "Optional. Omitted means the active run; with no active run "
                            "the server answers mode no_active_run."
                        ),
                    },
                    "expected_model": {"type": "string"},
                    "env": {},
                },
            },
            "ResumeResponse": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": ["string", "null"],
                        "description": "Null when there is no active run to resume.",
                    },
                    "mode": {
                        "type": "string",
                        "description": (
                            "Recovery decision a client branches on: no_active_run, "
                            "resume, request_human, blocked, ..."
                        ),
                    },
                    "safe": {"type": "boolean"},
                    "message": {
                        "type": "string",
                        "description": "Present in the no_active_run mode.",
                    },
                    "goal": {"type": ["string", "null"]},
                    "next_allowed_action": {"type": ["string", "null"]},
                    "human_steps": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "array", "items": {"type": "string"}},
                    "repairs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "kind": {"type": "string"},
                                "target": {"type": "string"},
                                "reason": {"type": "string"},
                                "requires_human": {"type": "boolean"},
                            },
                        },
                    },
                    "uncertain_actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action_id": {"type": "string"},
                                "action_type": {"type": "string"},
                                "status": {"type": "string"},
                            },
                        },
                    },
                    "progress": {
                        "type": "object",
                        "properties": {
                            "completed": {"type": "integer"},
                            "pending": {"type": "integer"},
                            "failed": {"type": "integer"},
                            "total": {"type": "integer"},
                        },
                    },
                    "tail_evidence": {},
                    "informed_retry": {"type": ["object", "null"]},
                    "attempt_lessons": {"type": "array", "items": {"type": "object"}},
                    "contract": {"type": "object"},
                    "contract_text": {"type": "string"},
                    "report": {"type": "string"},
                    "environment_changes": {"type": "array", "items": {"type": "string"}},
                    "constraint_pins": {"type": "object"},
                    "liveness": {"type": "object"},
                    "checkpoint_version": {"type": "integer"},
                    "validation_reason": {"type": "string"},
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
            "ConfirmResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "mode": {"type": "string"},
                    "safe": {"type": "boolean"},
                    "next_allowed_action": {"type": ["string", "null"]},
                    "report": {"type": "string"},
                },
                "required": ["run_id", "mode", "safe"],
            },
            "ListActionsResponse": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action_id": {"type": "string"},
                                "action_type": {"type": "string"},
                                "status": {"type": "string"},
                                "external_id": {"type": ["string", "null"]},
                                "side_effect_uncertain": {"type": "boolean"},
                                "outcome_unresolved": {
                                    "type": "boolean",
                                    "description": (
                                        "True when the ledger still holds the action as unsettled."
                                    ),
                                },
                            },
                        },
                    },
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
