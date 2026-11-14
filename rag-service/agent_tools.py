"""Allowlisted adapters for the agent owner; HTTP routing remains optional."""
from evidence import build_context, validate_draft
from reports import generate_report


class AgentTools:
    def __init__(self, index):
        self.index = index

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        if name == "search_regulations":
            if set(arguments) - {"query", "topK", "jurisdiction", "sourceKind", "legalAsOf"}:
                raise ValueError("Unknown search tool argument")
            return self.index.search(arguments["query"], arguments.get("topK", 3), arguments.get("jurisdiction", "KR"), arguments.get("sourceKind"), arguments.get("legalAsOf"))
        if name in {"build_evidence_context", "validate_report_draft", "generate_review_report"}:
            allowed = {"graph", "ruleResult", "query"} | ({"draft"} if name == "validate_report_draft" else set()) | ({"mode"} if name == "generate_review_report" else set())
            if set(arguments) - allowed:
                raise ValueError("Unknown evidence tool argument")
            context = build_context(self.index, arguments["graph"], arguments.get("ruleResult"), arguments.get("query"))
            if name == "generate_review_report":
                return generate_report(context, arguments.get("mode", "template"))
            return validate_draft(context, arguments["draft"]) if name == "validate_report_draft" else context
        raise ValueError("Unsupported tool")
