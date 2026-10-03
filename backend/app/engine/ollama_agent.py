from __future__ import annotations
import os
import re
import json
import logging
import httpx
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

# Automatically load environment variables from .env if present
_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), ".env")
if not os.path.exists(_env_file):
    _env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
if os.path.exists(_env_file):
    try:
        with open(_env_file, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip("'\"")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
    except Exception:
        pass

from ..models.schemas import (
    MigrationPlan,
    FieldMapping,
    ClarificationQuestion,
    current_utc_iso,
)
from .profiler import InspectionTools
from .transforms import SUPPORTED_RULES

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "https://ollama.com/api")
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")

class OllamaPlannerAgent:
    """Autonomous Migration Planner powered by Ollama (Cloud or Local inference).
    Uses Ollama's native JSON mode to generate strictly validated MigrationPlans."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        host: Optional[str] = None,
        model: Optional[str] = None,
        inspection_tools: Optional[InspectionTools] = None
    ):
        self.api_key = api_key or os.getenv("OLLAMA_API_KEY", "")
        self.host = (host or DEFAULT_OLLAMA_HOST).rstrip("/")
        self.model = model or DEFAULT_OLLAMA_MODEL
        self.tools = inspection_tools or InspectionTools()
        self.last_call_info: Dict[str, Any] = {
            "status": "READY",
            "model": self.model,
            "host": self.host,
            "has_api_key": bool(self.api_key),
            "prompt_sent": None,
            "raw_response": None,
            "parsed_plan": None,
            "latency_ms": None,
            "timestamp": None,
            "error": None
        }

    def set_config(self, api_key: Optional[str] = None, host: Optional[str] = None, model: Optional[str] = None) -> None:
        if api_key is not None:
            self.api_key = api_key.strip()
            self.last_call_info["has_api_key"] = bool(self.api_key)
        if host is not None:
            self.host = host.rstrip("/")
            self.last_call_info["host"] = self.host
        if model is not None:
            self.model = model.strip()
            self.last_call_info["model"] = self.model

    def verify_connection(self) -> Dict[str, Any]:
        """Tests connectivity to Ollama Cloud or Local host."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # If pointing to cloud, check /tags or a lightweight chat ping
        url = f"{self.host}/tags" if not self.host.endswith("/chat") else self.host
        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.get(url, headers=headers)
                if res.status_code in (200, 404):
                    return {
                        "status": "SUCCESS",
                        "message": f"Connected to Ollama at {self.host}",
                        "model": self.model,
                        "has_key": bool(self.api_key)
                    }
                return {
                    "status": "ERROR",
                    "message": f"Ollama returned HTTP {res.status_code}: {res.text[:200]}",
                    "model": self.model
                }
        except Exception as e:
            return {
                "status": "ERROR",
                "message": f"Connection to Ollama failed: {str(e)}",
                "model": self.model
            }

    def test_inference(self, sample_prompt: Optional[str] = None) -> Dict[str, Any]:
        """Runs a direct test inference against Ollama to verify AI completion capability in UI."""
        prompt = sample_prompt or "You are an autonomous data migration engineer. Map the legacy field 'full_name' to target fields 'first_name' and 'last_name' with transformation rules. Return a JSON object with 'field_mappings' and 'rationale'."
        chat_url = f"{self.host}/chat" if not self.host.endswith("/chat") else self.host
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are an AI data migration assistant. Respond strictly in valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1}
        }

        import time
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=90.0) as client:
                res = client.post(chat_url, headers=headers, json=payload)
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
                if res.status_code == 200:
                    data = res.json()
                    raw_content = data.get("message", {}).get("content", "")
                    clean_content = raw_content.strip()
                    if clean_content.startswith("```json"):
                        clean_content = clean_content[7:]
                    elif clean_content.startswith("```"):
                        clean_content = clean_content[3:]
                    if clean_content.endswith("```"):
                        clean_content = clean_content[:-3]
                    clean_content = clean_content.strip()

                    parsed = None
                    try:
                        parsed = json.loads(clean_content)
                    except Exception:
                        match = re.search(r"(\{.*\})", clean_content, re.DOTALL)
                        if match:
                            try:
                                parsed = json.loads(match.group(1))
                            except Exception:
                                pass

                    info = {
                        "status": "SUCCESS",
                        "model": self.model,
                        "host": self.host,
                        "latency_ms": elapsed_ms,
                        "prompt_sent": prompt,
                        "raw_response": raw_content,
                        "parsed_json": parsed,
                        "timestamp": current_utc_iso()
                    }
                    self.last_call_info = info
                    return info
                else:
                    info = {
                        "status": "ERROR",
                        "model": self.model,
                        "host": self.host,
                        "latency_ms": elapsed_ms,
                        "prompt_sent": prompt,
                        "raw_response": res.text,
                        "error": f"Ollama HTTP {res.status_code}: {res.text[:300]}",
                        "timestamp": current_utc_iso()
                    }
                    self.last_call_info = info
                    return info
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            info = {
                "status": "ERROR",
                "model": self.model,
                "host": self.host,
                "latency_ms": elapsed_ms,
                "prompt_sent": prompt,
                "raw_response": None,
                "error": str(e),
                "timestamp": current_utc_iso()
            }
            self.last_call_info = info
            return info

    def generate_plan(
        self,
        plan_version: int = 1,
        source_schema: Optional[Dict[str, Any]] = None,
        target_schema: Optional[Dict[str, Any]] = None,
        records: Optional[List[Dict[str, Any]]] = None,
        allow_fallback: bool = False
    ) -> MigrationPlan:
        """Generates a MigrationPlan using Ollama AI with intelligent deterministic fallback.
        Ensures high availability and zero crashes even if the external LLM is slow, down, or rate-limited.
        """
        src_schema = source_schema or self.tools.source_schema
        tgt_schema = target_schema or self.tools.target_schema
        sample_records = records or self.tools.records[:10]
        profiles = self.tools.profile_all_columns()

        try:
            # Attempt autonomous LLM generation via Ollama
            plan = self._call_ollama_llm(
                plan_version=plan_version,
                source_schema=src_schema,
                target_schema=tgt_schema,
                sample_records=sample_records,
                profiles=profiles
            )
            if plan:
                return plan
        except Exception as e:
            if not allow_fallback:
                raise
            logger.warning(f"Ollama AI plan synthesis error ({e}). Generating high-confidence semantic plan fallback.")

        # Resilient Fallback: High-confidence semantic dynamic plan
        from .agent import MigrationPlannerAgent
        fallback_agent = MigrationPlannerAgent(inspection_tools=self.tools)
        plan = fallback_agent._generate_dynamic_plan(
            source_schema=src_schema,
            target_schema=tgt_schema,
            profiles=profiles,
            plan_version=plan_version
        )
        plan.title = f"Autonomous Plan v{plan_version} (Production Fallback)"
        plan.description = f"Autonomous semantic migration plan synthesized with rule constraints."
        return plan

    def _call_ollama_llm(
        self,
        plan_version: int,
        source_schema: Dict[str, Any],
        target_schema: Dict[str, Any],
        sample_records: List[Dict[str, Any]],
        profiles: Dict[str, Any]
    ) -> Optional[MigrationPlan]:
        """Constructs prompt and queries Ollama chat endpoint with format='json'."""
        system_prompt = (
            "You are a Principal Data Migration Architect. Your task is to inspect the source dataset profile "
            "and map it deterministically into the target schema contract.\n"
            "MAPPING INVARIANTS & STANDARDS:\n"
            "1. DATE & DATETIME: For target fields of type 'date' or 'datetime', ALWAYS use 'DATE_TO_ISO8601' "
            "to guarantee standard ISO-8601 format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ).\n"
            "2. ENUMS & CATEGORIES: For target fields with 'enum' data type or 'allowed_values' constraints, "
            "use 'ENUM_LOOKUP' with parameters={'mapping': {'<src_val>': '<allowed_val>'}, 'default': '<allowed_val>'} "
            "or 'DIRECT_COPY' if values already conform.\n"
            "3. PRIMARY KEYS: For deterministic primary keys (UUID), use 'UUID_V5_FROM_KEY' referencing the source natural key.\n"
            "4. CURRENCY & AMOUNTS: For financial or monetary values, use 'CURRENCY_TO_FLOAT'.\n"
            "5. STRINGS: Use 'TRIM_CLEAN' or 'SPLIT_NAME' where appropriate.\n"
            "6. You must select transformation rules strictly from the provided Supported Transformation Catalog.\n"
            "Never generate code. Output MUST be a valid JSON object matching the exact schema specified."
        )

        catalog_summary = [
            {"rule_id": r.rule_id, "name": r.name, "description": r.description, "optional_params": r.optional_params}
            for r in SUPPORTED_RULES
        ]

        tgt_schema_id = target_schema.get("schema_id", "target_custom") if target_schema else "target_custom"
        src_schema_id = source_schema.get("schema_id", "source_custom") if source_schema else "source_custom"

        user_content = {
            "instruction": "Generate an auditable MigrationPlan mapping the source schema to the target schema with strict type and invariant enforcement.",
            "plan_version": plan_version,
            "target_schema": {
                "schema_id": tgt_schema_id,
                "table_name": target_schema.get("table_name", "target_records"),
                "natural_key": target_schema.get("natural_key", target_schema.get("primary_key", "id")),
                "fields": target_schema.get("fields", [])
            },
            "source_schema": {
                "schema_id": src_schema_id,
                "fields": [f.get("name") for f in source_schema.get("fields", [])]
            },
            "source_profiles_summary": {
                col: {
                    "null_percentage": p.get("null_percentage", 0),
                    "distinct_count": p.get("distinct_count", 0),
                    "patterns": p.get("patterns_detected", [])
                }
                for col, p in profiles.items()
            },
            "sample_records": sample_records[:3],
            "supported_rules": catalog_summary,
            "expected_json_structure": {
                "plan_id": f"plan_v{plan_version}",
                "version": plan_version,
                "title": f"Migration to {target_schema.get('table_name', 'target')}",
                "description": "Autonomous migration plan synthesized by Ollama AI",
                "field_mappings": [
                    {
                        "target_field": "<target_column_name>",
                        "source_fields": ["<source_column_name>"],
                        "transformation": "<RULE_ID_FROM_CATALOG>",
                        "parameters": {},
                        "risk_level": "LOW | MEDIUM | HIGH",
                        "risk_rationale": "Clear technical rationale why this mapping was chosen and risks detected",
                        "notes": "Short note"
                    }
                ],
                "clarifications": [
                    {
                        "question_id": "clarify_<identifier>",
                        "field": "<affected_target_field>",
                        "question": "Specific policy question requiring human decision",
                        "options": ["Option 1 (Recommended)", "Option 2", "Quarantine records"],
                        "user_answer": "Option 1 (Recommended)"
                    }
                ]
            }
        }

        chat_url = f"{self.host}/chat" if not self.host.endswith("/chat") else self.host
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_content, indent=2)}
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1}
        }

        import time
        start_time = time.perf_counter()
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, connect=6.0)) as client:
                res = client.post(chat_url, headers=headers, json=payload)
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            self.last_call_info = {
                "status": "ERROR",
                "model": self.model,
                "host": self.host,
                "latency_ms": elapsed_ms,
                "prompt_sent": user_content,
                "raw_response": None,
                "error": str(e),
                "timestamp": current_utc_iso()
            }
            raise RuntimeError(f"Ollama AI Communication Error ({self.host}): {str(e)}")

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        if res.status_code != 200:
            logger.error(f"Ollama returned HTTP {res.status_code}: {res.text}")
            self.last_call_info = {
                "status": "ERROR",
                "model": self.model,
                "host": self.host,
                "latency_ms": elapsed_ms,
                "prompt_sent": user_content,
                "raw_response": res.text,
                "error": f"Ollama HTTP {res.status_code}: {res.text[:300]}",
                "timestamp": current_utc_iso()
            }
            raise RuntimeError(f"Ollama AI Error (HTTP {res.status_code}): {res.text[:300]}")

        data = res.json()
        raw_content = data.get("message", {}).get("content", "")
        if not raw_content:
            self.last_call_info = {
                "status": "ERROR",
                "model": self.model,
                "host": self.host,
                "latency_ms": elapsed_ms,
                "prompt_sent": user_content,
                "raw_response": "",
                "error": "Empty completion received from AI model",
                "timestamp": current_utc_iso()
            }
            raise RuntimeError(f"Ollama AI Error: Model '{self.model}' returned an empty message.")

        clean_content = raw_content.strip()
        if clean_content.startswith("```json"):
            clean_content = clean_content[7:]
        elif clean_content.startswith("```"):
            clean_content = clean_content[3:]
        if clean_content.endswith("```"):
            clean_content = clean_content[:-3]
        clean_content = clean_content.strip()

        try:
            parsed_dict = json.loads(clean_content)
        except Exception:
            match = re.search(r"(\{.*\})", clean_content, re.DOTALL)
            if match:
                parsed_dict = json.loads(match.group(1))
            else:
                self.last_call_info = {
                    "status": "ERROR",
                    "model": self.model,
                    "host": self.host,
                    "latency_ms": elapsed_ms,
                    "prompt_sent": user_content,
                    "raw_response": raw_content,
                    "error": "Failed to parse JSON MigrationPlan from response",
                    "timestamp": current_utc_iso()
                }
                raise RuntimeError(f"Ollama AI Error: Failed to parse valid JSON MigrationPlan from AI response: {clean_content[:200]}")

        self.last_call_info = {
            "status": "SUCCESS",
            "model": self.model,
            "host": self.host,
            "latency_ms": elapsed_ms,
            "prompt_sent": user_content,
            "raw_response": raw_content,
            "parsed_json": parsed_dict,
            "timestamp": current_utc_iso()
        }

        # Robustly parse and validate field mappings
        mappings: List[FieldMapping] = []
        for m in parsed_dict.get("field_mappings", []):
            tgt = str(m.get("target_field", "")).strip()
            if not tgt:
                continue

            raw_src = m.get("source_fields") or m.get("source_field") or []
            if isinstance(raw_src, str):
                src_list = [raw_src.strip()] if raw_src.strip() else []
            elif isinstance(raw_src, list):
                src_list = [str(x).strip() for x in raw_src if str(x).strip()]
            else:
                src_list = []

            trans = str(m.get("transformation") or m.get("transformation_rule") or "DIRECT_COPY").strip()
            risk = str(m.get("risk_level", "LOW")).strip().upper()
            if risk not in ("LOW", "MEDIUM", "HIGH"):
                risk = "LOW"

            raw_params = m.get("parameters")
            params = raw_params if isinstance(raw_params, dict) else {}

            mappings.append(FieldMapping(
                target_field=tgt,
                source_fields=src_list,
                transformation=trans,
                parameters=params,
                risk_level=risk,
                risk_rationale=m.get("risk_rationale") or m.get("rationale") or f"Autonomous Ollama AI mapping via {trans}",
                notes=str(m.get("notes", "Ollama Mapped")) if m.get("notes") is not None else "Ollama Mapped"
            ))

        # Ensure all required target fields from schema contract are covered
        covered_targets = {m.target_field for m in mappings}
        for tf in (target_schema or {}).get("fields", []):
            t_name = tf.get("name")
            if t_name and t_name not in covered_targets:
                # Provide an auto-completed mapping
                mappings.append(FieldMapping(
                    target_field=t_name,
                    source_fields=[],
                    transformation="LITERAL_VALUE" if tf.get("default") is not None else "DIRECT_COPY",
                    parameters={"value": tf.get("default")} if tf.get("default") is not None else {},
                    risk_level="MEDIUM" if not tf.get("nullable", True) else "LOW",
                    risk_rationale=f"Auto-completed unmapped target contract field '{t_name}'",
                    notes="AI Contract Completer"
                ))

        clarifications: List[ClarificationQuestion] = []
        for c in parsed_dict.get("clarifications", []):
            clarifications.append(ClarificationQuestion(
                question_id=c.get("question_id", f"clarify_{len(clarifications)+1}"),
                field=c.get("field", ""),
                question=c.get("question", "Policy ambiguity detected"),
                options=c.get("options", ["Accept", "Quarantine"]),
                user_answer=c.get("user_answer", c.get("options", ["Accept"])[0])
            ))

        now_iso = current_utc_iso()
        return MigrationPlan(
            plan_id=f"plan_v{plan_version}",
            version=plan_version,
            title=parsed_dict.get("title", f"Ollama Autonomous Plan v{plan_version}"),
            description=parsed_dict.get("description", "Generated using Ollama inference"),
            source_schema_id=src_schema_id,
            target_schema_id=tgt_schema_id,
            field_mappings=mappings,
            clarifications=clarifications,
            status="PROPOSED",
            approved_by=None,
            approved_at=None,
            created_at=now_iso,
            updated_at=now_iso
        )
