"""v0.20.0 results of ``alice_memory_commit`` on the default surface, pinned.

Written for the compact commit result (search-quality spec section 7, test TC1).
These two normalised results were produced by the code at main 040a2a10 (v0.20.0 plus
the merged per-project work, before ``commit_result.py`` existed) through
``call_mcp_tool`` on a fresh SQLite vault, with ``ALICE_MCP_COMMIT_RESULT`` unset,
and are not edited by hand. Ids become ``<id>`` and timestamps ``<ts>``. Run twice
on two vaults the normalised text is identical, so nothing else needed hiding.

``committed`` is a one-sentence fact written with no identity. ``confirmation_required``
is a write at confidence 0.7. The tests compare today's code, with the switch unset or
``full``, against these.
"""

from __future__ import annotations

import json

COMMITTED_FACT = {"title": "Billing deploy day", "canonical_text": "The team deploys the billing service on Thursdays."}
HELD_FACT = {
    "title": "Maybe",
    "canonical_text": "The team maybe deploys on Fridays.",
    "confidence": 0.7,
}

GOLDENS: dict[str, object] = json.loads(
    r'''
{
 "committed": {
  "memory": {
   "agent_profile_id": "assistant_default",
   "canonical_text": "The team deploys the billing service on Thursdays.",
   "commit_digest": null,
   "confidence": 0.9,
   "confirmation_id": null,
   "confirmation_status": "confirmed",
   "created_at": "<ts>",
   "created_by_agent_id": null,
   "deleted_at": null,
   "domain": "unknown",
   "evidence_count": null,
   "extracted_by_model": null,
   "first_seen_at": "<ts>",
   "id": "<id>",
   "independent_source_count": null,
   "last_confirmed_at": "<ts>",
   "last_reviewed_at": null,
   "last_seen_at": "<ts>",
   "memory_key": "agentic_memory.semantic.<id>",
   "memory_type": "semantic",
   "metadata_json": {
    "agentic_memory": {
     "agent_identity": null,
     "contradiction_refs": [],
     "conversation_excerpt": null,
     "idempotency_key": null,
     "intent": "explicit_remember",
     "kind": "agentic_memory_commit",
     "lifecycle_status": "auto_committed",
     "policy_decision": {
      "policy_decision": {
       "action": "memory.commit",
       "decision": "allowed",
       "effective_domains": [
        "unknown"
       ],
       "effective_project_scope": [],
       "effective_sensitivity_allowed": [
        "unknown"
       ],
       "permission_profile": "user_or_system",
       "reasons": [],
       "requested_domains": [
        "unknown"
       ],
       "requested_project_scope": [],
       "requested_sensitivity_allowed": [
        "unknown"
       ],
       "review_required": false,
       "trace_id": "policy-<id>",
       "workflow_type": null
      },
      "reason": "explicit_trusted_memory_commit",
      "reasons": [],
      "requires_confirmation": false,
      "requires_dashboard_review": false,
      "status": "committed",
      "trace_id": "policy-<id>",
      "write_mode": "commit"
     },
     "project_scope": [],
     "rationale": null,
     "request_fingerprint": "6f8dd50e18e84599ba909e6e52b028796edff14109d4095d374733be9eb04e47",
     "source_refs": [],
     "source_type": "direct_user_instruction",
     "status": "committed",
     "trace_id": "policy-<id>",
     "write_mode": "commit"
    },
    "policy_decision": {
     "action": "memory.commit",
     "decision": "allowed",
     "effective_domains": [
      "unknown"
     ],
     "effective_project_scope": [],
     "effective_sensitivity_allowed": [
      "unknown"
     ],
     "permission_profile": "user_or_system",
     "reasons": [],
     "requested_domains": [
      "unknown"
     ],
     "requested_project_scope": [],
     "requested_sensitivity_allowed": [
      "unknown"
     ],
     "review_required": false,
     "trace_id": "policy-<id>",
     "workflow_type": null
    },
    "project_scope": [],
    "review_required": false
   },
   "project_id": null,
   "project_scope": [],
   "promotion_eligibility": "promotable",
   "run_id": null,
   "salience": null,
   "sensitivity": "unknown",
   "source_event_ids": [],
   "status": "active",
   "summary": "The team deploys the billing service on Thursdays.",
   "superseded_by": null,
   "supersedes": null,
   "title": "Billing deploy day",
   "trust_class": "deterministic",
   "trust_reason": null,
   "updated_at": "<ts>",
   "user_id": "<id>",
   "valid_from": null,
   "valid_to": null,
   "value": {
    "intent": "explicit_remember",
    "source_refs": [],
    "text": "The team deploys the billing service on Thursdays."
   }
  },
  "policy_decision": {
   "policy_decision": {
    "action": "memory.commit",
    "decision": "allowed",
    "effective_domains": [
     "unknown"
    ],
    "effective_project_scope": [],
    "effective_sensitivity_allowed": [
     "unknown"
    ],
    "permission_profile": "user_or_system",
    "reasons": [],
    "requested_domains": [
     "unknown"
    ],
    "requested_project_scope": [],
    "requested_sensitivity_allowed": [
     "unknown"
    ],
    "review_required": false,
    "trace_id": "policy-<id>",
    "workflow_type": null
   },
   "reason": "explicit_trusted_memory_commit",
   "reasons": [],
   "requires_confirmation": false,
   "requires_dashboard_review": false,
   "status": "committed",
   "trace_id": "policy-<id>",
   "write_mode": "commit"
  },
  "receipt": "saved as a fact.",
  "status": "committed",
  "write_mode": "commit"
 },
 "confirmation_required": {
  "confirmation": {
   "agent_id": null,
   "confirmation_id": "confirm-<id>",
   "created_at": "<ts>",
   "domain": "unknown",
   "expires_at": "<ts>",
   "policy_reason": "medium_confidence_requires_confirmation",
   "proposed_text": "The team maybe deploys on Fridays.",
   "sensitivity": "unknown",
   "status": "pending"
  },
  "confirmation_id": "confirm-<id>",
  "memory": {
   "agent_profile_id": "assistant_default",
   "canonical_text": "The team maybe deploys on Fridays.",
   "commit_digest": null,
   "confidence": 0.7,
   "confirmation_id": "confirm-<id>",
   "confirmation_status": "unconfirmed",
   "created_at": "<ts>",
   "created_by_agent_id": null,
   "deleted_at": null,
   "domain": "unknown",
   "evidence_count": null,
   "extracted_by_model": null,
   "first_seen_at": "<ts>",
   "id": "<id>",
   "independent_source_count": null,
   "last_confirmed_at": null,
   "last_reviewed_at": null,
   "last_seen_at": "<ts>",
   "memory_key": "agentic_memory.semantic.<id>",
   "memory_type": "semantic",
   "metadata_json": {
    "agentic_memory": {
     "agent_identity": null,
     "confirmation": {
      "agent_id": null,
      "confirmation_id": "confirm-<id>",
      "created_at": "<ts>",
      "domain": "unknown",
      "expires_at": "<ts>",
      "policy_reason": "medium_confidence_requires_confirmation",
      "proposed_text": "The team maybe deploys on Fridays.",
      "sensitivity": "unknown",
      "status": "pending"
     },
     "contradiction_refs": [],
     "conversation_excerpt": null,
     "idempotency_key": null,
     "intent": "explicit_remember",
     "kind": "agentic_memory_commit",
     "lifecycle_status": "pending_inline_confirmation",
     "policy_decision": {
      "policy_decision": {
       "action": "memory.commit",
       "decision": "allowed",
       "effective_domains": [
        "unknown"
       ],
       "effective_project_scope": [],
       "effective_sensitivity_allowed": [
        "unknown"
       ],
       "permission_profile": "user_or_system",
       "reasons": [],
       "requested_domains": [
        "unknown"
       ],
       "requested_project_scope": [],
       "requested_sensitivity_allowed": [
        "unknown"
       ],
       "review_required": false,
       "trace_id": "policy-<id>",
       "workflow_type": null
      },
      "reason": "medium_confidence_requires_confirmation",
      "reasons": [
       "medium_confidence_requires_confirmation"
      ],
      "requires_confirmation": true,
      "requires_dashboard_review": false,
      "status": "confirmation_required",
      "trace_id": "policy-<id>",
      "write_mode": "confirm_inline"
     },
     "project_scope": [],
     "rationale": null,
     "request_fingerprint": "9d0ecca8ae13d2167ee6f043ac8fec8e8a90e6ea7ac1e4674ea2b6e76e54c202",
     "source_refs": [],
     "source_type": "direct_user_instruction",
     "status": "confirmation_required",
     "trace_id": "policy-<id>",
     "write_mode": "confirm_inline"
    },
    "policy_decision": {
     "action": "memory.commit",
     "decision": "allowed",
     "effective_domains": [
      "unknown"
     ],
     "effective_project_scope": [],
     "effective_sensitivity_allowed": [
      "unknown"
     ],
     "permission_profile": "user_or_system",
     "reasons": [],
     "requested_domains": [
      "unknown"
     ],
     "requested_project_scope": [],
     "requested_sensitivity_allowed": [
      "unknown"
     ],
     "review_required": false,
     "trace_id": "policy-<id>",
     "workflow_type": null
    },
    "project_scope": [],
    "review_required": false
   },
   "project_id": null,
   "project_scope": [],
   "promotion_eligibility": "promotable",
   "run_id": null,
   "salience": null,
   "sensitivity": "unknown",
   "source_event_ids": [],
   "status": "needs_review",
   "summary": "The team maybe deploys on Fridays.",
   "superseded_by": null,
   "supersedes": null,
   "title": "Maybe",
   "trust_class": "deterministic",
   "trust_reason": null,
   "updated_at": "<ts>",
   "user_id": "<id>",
   "valid_from": null,
   "valid_to": null,
   "value": {
    "intent": "explicit_remember",
    "source_refs": [],
    "text": "The team maybe deploys on Fridays."
   }
  },
  "policy_decision": {
   "policy_decision": {
    "action": "memory.commit",
    "decision": "allowed",
    "effective_domains": [
     "unknown"
    ],
    "effective_project_scope": [],
    "effective_sensitivity_allowed": [
     "unknown"
    ],
    "permission_profile": "user_or_system",
    "reasons": [],
    "requested_domains": [
     "unknown"
    ],
    "requested_project_scope": [],
    "requested_sensitivity_allowed": [
     "unknown"
    ],
    "review_required": false,
    "trace_id": "policy-<id>",
    "workflow_type": null
   },
   "reason": "medium_confidence_requires_confirmation",
   "reasons": [
    "medium_confidence_requires_confirmation"
   ],
   "requires_confirmation": true,
   "requires_dashboard_review": false,
   "status": "confirmation_required",
   "trace_id": "policy-<id>",
   "write_mode": "confirm_inline"
  },
  "receipt": "needs confirmation.",
  "status": "confirmation_required",
  "write_mode": "confirm_inline"
 }
}
'''
)
