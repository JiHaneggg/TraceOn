"""Allowlisted adapters for the agent owner; HTTP routing remains optional."""
from evidence import build_context, validate_draft
from reports import generate_report
from evidence_lookup import EvidenceRepository, resolve_fact
from report_verification import verify_report
from evidence_fit import assessment_response
from agent_judge import openai_judge
from judge_debate import openai_debate


class AgentTools:
    def __init__(self, index, raw_root=None, archive_dir=None):
        self.index = index
        self.repository = EvidenceRepository(index, raw_root, archive_dir)

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        if name == "run_review_workflow":
            from agent_workflow import run_review_workflow
            if set(arguments) - {"graph", "ruleResult", "query", "corpusSha256", "mode", "maxRetries", "judgeMode"}:
                raise ValueError("Unknown workflow tool argument")
            judge_mode = arguments.get("judgeMode", "off")
            if judge_mode not in ("off", "openai", "debate"):
                raise ValueError("judgeMode must be off, openai or debate")
            return run_review_workflow(self.call, arguments["graph"], arguments["ruleResult"],
                                       arguments.get("query"), arguments.get("corpusSha256"),
                                       arguments.get("mode", "template"), arguments.get("maxRetries", 0),
                                       judge={"off": None, "openai": openai_judge, "debate": openai_debate}[judge_mode])
        if name == "get_citation":
            if set(arguments) - {"chunkId", "corpusSha256"}:
                raise ValueError("Unknown citation tool argument")
            return self.repository.citation(arguments["chunkId"], arguments.get("corpusSha256"))
        if name == "verify_review_report":
            if set(arguments) - {"graph", "ruleResult", "query", "report"}:
                raise ValueError("Unknown verification tool argument")
            return verify_report(self.repository, arguments["graph"], arguments.get("ruleResult"), arguments["report"], arguments.get("query"))
        if name == "search_regulations":
            if set(arguments) - {"query", "topK", "jurisdiction", "sourceKind", "legalAsOf"}:
                raise ValueError("Unknown search tool argument")
            return self.repository.index_for().search(arguments["query"], arguments.get("topK", 3), arguments.get("jurisdiction", "KR"), arguments.get("sourceKind"), arguments.get("legalAsOf"))
        if name in {"build_evidence_context", "validate_report_draft", "generate_review_report", "resolve_report_fact", "prepare_review_report", "assess_evidence_fit"}:
            allowed = {"graph", "ruleResult", "query", "corpusSha256"} | ({"draft"} if name == "validate_report_draft" else set()) | ({"mode"} if name in {"generate_review_report", "prepare_review_report"} else set())
            if name == "resolve_report_fact":
                allowed |= {"factId", "contextId"}
            if set(arguments) - allowed:
                raise ValueError("Unknown evidence tool argument")
            selected = (self.repository.index_for(arguments.get("corpusSha256")) if name == "resolve_report_fact"
                        else self.repository.index_for_generation(arguments.get("corpusSha256")))
            context = build_context(selected, arguments["graph"], arguments.get("ruleResult"), arguments.get("query"))
            if name == "assess_evidence_fit":
                return assessment_response(context)
            if name == "generate_review_report":
                return generate_report(context, arguments.get("mode", "template"))
            if name == "resolve_report_fact":
                return resolve_fact(context, arguments["factId"], arguments.get("contextId"))
            if name == "prepare_review_report":
                report = generate_report(context, arguments.get("mode", "template"))
                return {"context": context, "report": report, "verification": verify_report(self.repository,
                    arguments["graph"], arguments.get("ruleResult"), report, arguments.get("query"))}
            return validate_draft(context, arguments["draft"]) if name == "validate_report_draft" else context
        raise ValueError("Unsupported tool")
