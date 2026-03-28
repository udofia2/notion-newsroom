"""Workflow orchestration modules for Newsroom OS."""

from .agency_bridge import prepare_for_publication
from .context_hunter import run_context_hunter
from .narrative_auditor import run_narrative_audit
from .traffic_strategist import (
	create_pitch_page,
	detect_trending_stories,
	generate_followup_angles,
)

__all__ = [
	"prepare_for_publication",
	"create_pitch_page",
	"detect_trending_stories",
	"generate_followup_angles",
	"run_narrative_audit",
	"run_context_hunter",
]
