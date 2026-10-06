"""Deterministic EN/FR command router used when no LLM is configured (and in reproducible E2E tests).

It maps common operator commands to the same typed tools the LLM uses. It never fabricates data;
unknown requests get an honest answer explaining what it can do without an AI provider.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from scout.ai.factory import register_local_chat_handler
from scout.ai.provider import (
    AIUsage,
    ChatEvent,
    ChatTurn,
    TextDelta,
    ToolCallEvent,
    ToolCallRequest,
    ToolSpec,
    TurnComplete,
)


def _call(name: str, args: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=f"local_{uuid.uuid4().hex[:8]}", name=name, args=args)


def _columns_from_system(system: str) -> list[str]:
    m = re.search(r"visible columns: (.+)", system)
    return [c.strip() for c in m.group(1).split(",")] if m else []


def _selected(system: str) -> int:
    m = re.search(r"selected rows: (\d+)", system)
    return int(m.group(1)) if m else 0


def _has_filters(system: str) -> bool:
    return "active filters:" in system


def _rows_ref(text: str, system: str) -> dict[str, Any]:
    low = text.lower()
    if re.search(
        r"\b(these|those|selected|them|ces|celles?-ci|ceux-ci|la s[ée]lection|les s[ée]lectionn)", low
    ) and _selected(system):
        return {"target": "selection"}
    conds = parse_conditions(text, system)
    if conds:
        return {"target": "filter", "filter": {"match": "all", "conditions": conds}}
    if re.search(r"\b(those|these|them|ces|eux)\b", low) and _has_filters(system):
        return {"target": "current_view"}
    if re.search(r"\b(everyone|all|tous|toutes|every lead|entire list|whole list)\b", low):
        return {"target": "all"}
    return (
        {"target": "current_view"}
        if _has_filters(system)
        else {"target": "selection" if _selected(system) else "all"}
    )


def parse_conditions(text: str, system: str) -> list[dict[str, Any]]:
    """Extract filter conditions from phrases like 'ManyChat=true, SAFE email and score >85'."""
    low = text.lower()
    conds: list[dict[str, Any]] = []
    cols = _columns_from_system(system)
    for col in sorted(cols, key=len, reverse=True):
        cl = col.lower()
        if len(cl) < 3 or cl in (
            "email",
            "email status",
            "person",
            "company",
            "title",
            "website",
            "icp score",
            "confidence",
            "sources",
            "updated",
            "location",
            "company size",
            "industry",
        ):
            continue
        m = re.search(
            rf"{re.escape(cl)}\s*(=|==|:|is|est)?\s*(true|false|yes|no|oui|non|vrai|faux|unknown|inconnu)\b",
            low,
        )
        if m:
            v = m.group(2)
            if v in ("unknown", "inconnu"):
                conds.append({"field": col, "operator": "is_unknown"})
            else:
                conds.append(
                    {
                        "field": col,
                        "operator": "is_true" if v in ("true", "yes", "oui", "vrai") else "is_false",
                    }
                )
        elif re.search(rf"\bwith {re.escape(cl)}\b|\bavec {re.escape(cl)}\b", low):
            conds.append({"field": col, "operator": "is_true"})
    if re.search(r"\b(safe)\b", low) and re.search(r"email|e-mail|mail", low):
        conds.append({"field": "email_status", "operator": "eq", "value": "SAFE"})
    elif re.search(r"\brisky\b", low):
        conds.append({"field": "email_status", "operator": "eq", "value": "RISKY"})
    if re.search(r"(remove|exclude|without|sans|enl[eè]ve|retire)\w*\s+(the\s+|les\s+)?catch[- ]?all", low):
        conds.append({"field": "email_status", "operator": "neq", "value": "CATCH_ALL"})
    m = re.search(
        r"(?:icp\s*)?score\s*(>=|>|over|above|greater than|sup[ée]rieur [àa]|de plus de|<|under|below)\s*(\d+)",
        low,
    )
    if m:
        op = {
            ">": "gt",
            ">=": "gte",
            "over": "gt",
            "above": "gt",
            "greater than": "gt",
            "<": "lt",
            "under": "lt",
            "below": "lt",
        }.get(m.group(1), "gt")
        conds.append({"field": "icp_score", "operator": op, "value": float(m.group(2))})
    if re.search(r"\b(only|just|seulement|uniquement|show)\b.*\b(founders?|fondateurs?)\b", low):
        conds.append({"field": "role_family", "operator": "eq", "value": "founder"})
    if re.search(r"(missing|without|no|sans) (an? )?e?-?mails?", low):
        conds.append({"field": "email", "operator": "is_empty"})
    m = re.search(
        r"\b(?:in|à|a) (paris|lyon|marseille|bordeaux|lille|nantes|toulouse|nice|rennes|strasbourg|montpellier)\b",
        low,
    )
    if m:
        conds.append({"field": "city", "operator": "eq", "value": m.group(1).title()})
    return conds


def _quoted_or_after(text: str, pattern: str) -> str | None:
    m = re.search(pattern + r"\s*[\"“'«]([^\"”'»]+)[\"”'»]", text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(pattern + r"\s+(.+?)(?:\s+and\s+|\s+et\s+|[.,;!?]|$)", text, re.I)
    return m.group(1).strip() if m else None


def route(text: str, system: str) -> tuple[list[ToolCallRequest], str | None]:
    """Return (tool calls, direct reply)."""
    t = text.strip()
    low = t.lower()
    # ---- campaigns -----------------------------------------------------------------------
    if re.search(
        r"\b(same|this) campaign again|run (it|this|the campaign) again|relance\w* (la|cette) campagne|start (the )?same campaign",
        low,
    ):
        count = re.search(r"\b(\d[\d,. ]*)\b", t)
        args: dict[str, Any] = {}
        if count and re.search(r"\d", count.group(1)):
            try:
                args["count"] = int(re.sub(r"[^\d]", "", count.group(1)))
            except ValueError:
                pass
        return [_call("rerun_campaign", args)], None
    m = re.search(r"\brun (.+?) again\b", low)
    if m:
        return [_call("rerun_campaign", {"campaign_name": m.group(1).strip(" \"'")})], None
    if re.search(
        r"\b(another|more|encore|autres?)\b.*\b(like (these|those|them)|similaires?|comme (ceux|celles)-?(ci|là)?)",
        low,
    ):
        n = re.search(r"\b(\d[\d,. ]*\d|\d+)\b", t)
        more_count = int(re.sub(r"[^\d]", "", n.group(1))) if n else 100
        return [
            _call(
                "find_more_leads",
                {
                    "count": more_count,
                    "extra_request": None,
                    "exclude_existing_companies": bool(
                        re.search(r"compan\w+ (i|we) already|entreprises d[ée]j[àa]", low)
                    ),
                },
            )
        ], None
    if re.search(
        r"\b(different|another|other|autre)\b.*\b(decision makers?|d[ée]cideurs?|head of|director|directeur|cmo|ceo|founder)\b.*\b(compan|entreprises|list)",
        low,
    ):
        titles = []
        if "marketing" in low:
            titles = ["Head of Marketing", "Marketing Director", "CMO"]
        elif "sales" in low or "commercial" in low:
            titles = ["Head of Sales", "Sales Director"]
        else:
            titles = ["Founder", "CEO", "Managing Director"]
        return [
            _call(
                "find_decision_makers", {"titles": titles, "max_per_company": 1, "exclude_known_people": True}
            )
        ], None
    if re.search(r"^(find|get|search|discover|trouve|cherche|donne|liste)\w*\b", low) and re.search(
        r"\d|agenc|compan|entreprise|leads?|founders?|ceos?|startups?|dentist|restaurants?|people|contacts?",
        low,
    ):
        if re.search(
            r"(missing|without|no|sans) (an? )?e?-?mails?|find (their )?emails?|trouve.*(les )?e-?mails?", low
        ):
            return [_call("find_emails", {"rows": _rows_ref(t, system)})], None
        return [_call("create_campaign", {"request": t})], None
    if re.search(r"\b(pause)\b.*campa|^pause\b", low):
        return [_call("pause_campaign", {})], None
    if re.search(r"\b(resume|reprend\w*)\b", low):
        return [_call("resume_campaign", {})], None
    if re.search(r"\b(cancel|stop|annule\w*|arr[eê]te\w*)\b.*campa", low):
        return [_call("cancel_campaign", {})], None
    if re.search(r"(campaign status|progress|o[uù] en est|status of the campaign|how many qualified)", low):
        return [_call("get_campaign_status", {})], None
    # ---- lists ------------------------------------------------------------------------------
    m = re.search(
        r"(?:create|make|new|cr[ée]e\w*)\s+(?:a\s+|une\s+)?(?:new\s+)?(?:list|liste)\s+(?:called|named|nomm[ée]e?|appel[ée]e?)?\s*[\"“'«]?([^\"”'».]+?)[\"”'»]?(?:\s+and\s+(?:put|add)\s+(.+?)\s+(?:into|in|to)\s+it|\s+et\s+(?:mets|ajoute)\s+(.+?)\s+dedans)?\s*[.!]?$",
        t,
        re.I,
    )
    if m:
        name = m.group(1).strip()
        rest = m.group(2) or m.group(3)
        args = {"name": name}
        if rest:
            args["rows"] = _rows_ref(rest, system)
        return [_call("create_list", args)], None
    m = re.search(
        r"(?:put|add|move|mets|ajoute|d[ée]place)\s+(.*?)\s*(?:into|to|in|dans)\s+(?:the\s+|la\s+liste\s+|my\s+)?[\"“'«]?([^\"”'».]+?)[\"”'»]?(?:\s+list)?\s*[.!]?$",
        t,
        re.I,
    )
    if m:
        who, target = m.group(1), m.group(2).strip()
        rows = _rows_ref(who or "those", system)
        if re.match(r"(move|d[ée]place)", low):
            return [_call("move_between_lists", {"rows": rows, "to_list_name": target})], None
        return [_call("add_to_list", {"rows": rows, "list_name": target, "create_if_missing": True})], None
    m = re.search(r"rename (?:this |the )?list (?:to|as) [\"“']?([^\"”'.]+)", t, re.I) or re.search(
        r"renomme\w* (?:cette |la )?liste en [\"“']?([^\"”'.]+)", t, re.I
    )
    if m:
        return [_call("rename_list", {"new_name": m.group(1).strip()})], None
    if re.search(r"\bduplicate (this |the )?list|duplique\w* (cette |la )?liste", low):
        return [_call("duplicate_list", {})], None
    if re.search(r"\barchive (this |the )?list|archive\w* (cette |la )?liste", low):
        return [_call("archive_list", {})], None
    if re.search(r"\bdelete (this |the )?list|supprime\w* (cette |la )?liste", low):
        return [_call("delete_list", {})], None
    if re.search(
        r"(remove|retire|enl[eè]ve)\w*\s+(these|those|them|selected|ces)\b.*(from|de) (the |this |la |cette )?(list|liste)",
        low,
    ):
        return [_call("remove_from_list", {"rows": _rows_ref(t, system)})], None
    # ---- columns ----------------------------------------------------------------------------
    m = re.search(
        r"add (?:a )?column (?:called|named) [\"“']?(.+?)[\"”']?\s+(?:and|that|to|with|which)\s+(.+)$",
        t,
        re.I,
    ) or re.search(
        r"ajoute\w* (?:une )?colonne (?:appel[ée]e|nomm[ée]e) [\"“']?(.+?)[\"”']?\s+(?:et|qui|pour)\s+(.+)$",
        t,
        re.I,
    )
    if m:
        return [_call("create_column", {"name": m.group(1).strip(), "instruction": m.group(2).strip()})], None
    m = re.search(
        r"add (?:a )?column (?:called|named)?\s*[\"“']?([^\"”'.]+)[\"”']?\.?$", t, re.I
    ) or re.search(r"ajoute\w* (?:une )?colonne\s*[\"“']?([^\"”'.]+)", t, re.I)
    if m:
        name = m.group(1).strip()
        return [_call("create_column", {"name": name[:80], "instruction": name})], None
    m = re.search(r"^add (?:whether|if) (?:they |their site |the site |their website )?(.+?)[.!?]?$", t, re.I)
    if m:
        what = m.group(1).strip()
        mm = re.search(r"mention(?:s)?\s+(.+)$", what, re.I)
        name = f"Mentions {mm.group(1).strip()}" if mm else what[:60].capitalize()
        return [_call("create_column", {"name": name[:80], "instruction": f"whether they {what}"})], None
    m = re.search(r"^(?:add|find)\s+(?:their|the|une?|leur)\s+(.+?)[.!?]?$", t, re.I)
    if m and not re.search(r"emails?\b", m.group(1), re.I):
        what = m.group(1).strip()
        return [_call("create_column", {"name": what[:60].capitalize(), "instruction": t})], None
    m = re.search(
        r"(?:enrich|refresh|enrichis|rafra[iî]chis)\w*\s+(?:the\s+|la\s+)?(?:column\s+|colonne\s+)?[\"“']?([^\"”'.]+?)[\"”']?(?:\s+column)?[.!]?$",
        t,
        re.I,
    )
    if m and not re.search(r"contacts? older", low):
        return [
            _call(
                "enrich_column" if low.startswith(("enrich", "enrichis")) else "refresh_column",
                {"column_name": m.group(1).strip()},
            )
        ], None
    m = re.search(r"(?:delete|remove|supprime\w*) (?:the )?column [\"“']?([^\"”'.]+)", t, re.I)
    if m:
        return [_call("delete_column", {"column_name": m.group(1).strip()})], None
    m = re.search(r"hide (?:the )?([^.]+?) column|masque\w* (?:la )?colonne ([^.]+)", t, re.I)
    if m:
        return [_call("hide_column", {"column": (m.group(1) or m.group(2)).strip(), "hidden": True})], None
    # ---- emails -----------------------------------------------------------------------------
    if re.search(r"(find|trouve\w*)\s+(the\s+)?(missing\s+)?e-?mails?|missing emails", low):
        return [
            _call(
                "find_emails",
                {
                    "rows": _rows_ref(t, system)
                    if not re.search("missing", low)
                    else {
                        "target": "filter",
                        "filter": {
                            "match": "all",
                            "conditions": [{"field": "email", "operator": "is_empty"}],
                        },
                    }
                },
            )
        ], None
    if re.search(r"(verify|re-?verify|v[ée]rifi\w*)", low) and re.search(r"e-?mails?", low):
        statuses = ["RISKY"] if "risky" in low else (["CATCH_ALL"] if "catch" in low else [])
        return [_call("verify_emails", {"statuses": statuses})], None
    # ---- filtering / sorting ------------------------------------------------------------------
    if re.search(
        r"^(only|just|show|keep|filter|remove|exclude|garde|montre|affiche|filtre|enl[eè]ve|retire)\b|\bonly keep\b|\bshow only\b",
        low,
    ):
        conds = parse_conditions(t, system)
        if conds:
            mode = "add" if re.search(r"\b(only keep|keep only|also|aussi|garde)\b", low) else "replace"
            return [
                _call("filter_table", {"filter": {"match": "all", "conditions": conds}, "mode": mode})
            ], None
    m = re.search(r"sort by ([a-z _]+?)(?: (asc|desc|ascending|descending))?$|trie\w* par ([a-z _]+)", low)
    if m:
        fld = (m.group(1) or m.group(3)).strip()
        direction = "asc" if (m.group(2) or "").startswith("asc") else "desc"
        return [_call("sort_table", {"sort": [{"field": fld, "direction": direction}]})], None
    # ---- export / import / misc ---------------------------------------------------------------------
    if re.search(r"\bexport\w*\b", low):
        return [
            _call(
                "export_leads",
                {
                    "columns": "all" if "all columns" in low else "visible",
                    "format": "json" if "json" in low else "csv",
                },
            )
        ], None
    if re.search(r"\bimport\w*\b", low):
        return [_call("import_leads", {})], None
    if re.search(r"why .*scor|pourquoi .*score|explain .*score", low):
        return [_call("explain_score", {})], None
    if re.search(r"which campaigns|quelles? campagnes?|when did i first|history|historique", low):
        return [_call("get_lead_history", {})], None
    if (
        re.search(r"(where|source).*(come from|vient|provenance)|view sources?|sources?\b", low)
        and _selected(system) == 1
    ):
        return [_call("get_sources", {})], None
    if re.search(r"\b(suppress|do not contact|opt[- ]?out|blacklist)\b", low):
        return [_call("suppress_leads", {"rows": _rows_ref(t, system)})], None
    if re.search(r"\b(my lists|show lists|quelles listes|mes listes)\b", low):
        return [_call("get_lists", {})], None
    return [], (
        "I can run that once an AI provider is configured. Without it I understand direct commands like "
        "“Find 200 marketing agencies in Lyon”, “Create a list called Hot Leads”, “Add a column ManyChat”, "
        "“Only keep SAFE emails”, “Put these into Hot Leads” or “Export”."
    )


def summarize(results: list[tuple[ToolCallRequest, dict[str, Any]]]) -> str:
    lines = []
    for call, res in results:
        if "error" in res:
            err = res["error"]
            lines.append(
                f"I couldn't {call.name.replace('_', ' ')}: {err.get('message')}"
                + (f" ({err['hint']})" if err.get("hint") else "")
            )
            continue
        if res.get("status") == "awaiting_confirmation":
            lines.append("Please confirm the action above.")
            continue
        n = call.name
        if n == "create_campaign" or n == "rerun_campaign":
            lines.append(
                f"Campaign started — I'll keep searching until {res.get('target', 0):,} qualified leads. New leads appear as they qualify."
            )
        elif n == "find_more_leads":
            lines.append(
                f"Looking for {res.get('target', 0):,} more leads like these, excluding everyone you've already seen."
            )
        elif n == "find_decision_makers":
            lines.append(
                f"Searching {res.get('companies', 0):,} existing companies for new decision makers (known people excluded)."
            )
        elif n == "create_list":
            lines.append(
                f"{'Created' if res.get('created') else 'Using existing'} list “{res.get('name')}”"
                + (f" with {res.get('added', 0):,} leads." if res.get("added") else ".")
            )
        elif n == "add_to_list":
            lines.append(f"Added {res.get('added', 0):,} leads to “{res.get('list')}”.")
        elif n == "move_between_lists":
            lines.append(f"Moved {res.get('moved', 0):,} leads to “{res.get('to')}”.")
        elif n == "remove_from_list":
            lines.append(f"Removed {res.get('removed', 0):,} leads from the list (their history is kept).")
        elif n == "create_column":
            cov = res.get("coverage") or {}
            lines.append(
                f"Created “{res.get('name')}” · {res.get('resolver')}"
                + (f" · {cov.get('cached', 0):,}/{cov.get('total', 0):,} already crawled" if cov else "")
                + "."
            )
        elif n == "filter_table":
            lines.append(f"{res.get('matching_rows', 0):,} rows match.")
        elif n in ("enrich_column", "refresh_column", "find_emails", "verify_emails"):
            lines.append(f"Queued {res.get('queued', 0):,} rows.")
        elif n == "export_leads":
            lines.append("Your export is downloading.")
        elif n == "get_campaign_status":
            st = res.get("stats") or {}
            lines.append(
                f"{res.get('name')}: {res.get('status')} — {st.get('qualified', 0):,}/{res.get('target', 0):,} qualified."
                + (f" {res.get('stop_reason')}" if res.get("stop_reason") else "")
            )
        elif n == "explain_score":
            if res.get("icp_score") is None:
                lines.append("This lead hasn't been scored yet.")
            else:
                lines.append(f"Score {res['icp_score']}: " + "; ".join(res.get("explanation", [])[:6]) + ".")
        elif n == "get_lead_history":
            camps = ", ".join(c["name"] for c in res.get("campaigns", [])) or "none"
            lines.append(f"First seen {str(res.get('first_seen'))[:10]}. Campaigns: {camps}.")
        elif n == "get_lists":
            lines.append(
                "Lists: "
                + ", ".join(f"{x['name']} ({x['count']:,})" for x in res.get("lists", [])[:12])
                + "."
            )
        else:
            lines.append("Done.")
    return " ".join(lines) or "Done."


async def local_chat(system: str, turns: list[ChatTurn], tools: list[ToolSpec]) -> AsyncIterator[ChatEvent]:
    last = turns[-1] if turns else None
    usage = AIUsage(model="local")
    if last is not None and last.role == "tool":
        text = summarize(last.tool_results)
        yield TextDelta(text)
        yield TurnComplete(turn=ChatTurn(role="assistant", text=text), usage=usage)
        return
    user_text = last.text if last is not None else ""
    calls, reply = route(user_text, system)
    if reply:
        yield TextDelta(reply)
    for c in calls:
        yield ToolCallEvent(c)
    yield TurnComplete(turn=ChatTurn(role="assistant", text=reply or "", tool_calls=calls), usage=usage)


register_local_chat_handler(local_chat)
