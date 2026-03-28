"""Analytics connectors for newsroom workflows."""

from .google import fetch_realtime_story_views, get_page_traffic
from .plausible import get_page_traffic as get_page_traffic_plausible

__all__ = [
	"fetch_realtime_story_views",
	"get_page_traffic",
	"get_page_traffic_plausible",
]
