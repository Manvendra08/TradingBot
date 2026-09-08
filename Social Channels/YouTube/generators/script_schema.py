"""
Social Channels/YouTube/generators/script_schema.py
Pydantic data contracts for LLM generated YouTube script validation.
"""
from __future__ import annotations
from pydantic import BaseModel, Field


class SceneItem(BaseModel):
    scene_id: int
    segment_title: str
    headline_text: str
    metric_highlight: str
    spoken_text: str


class YouTubeScriptModel(BaseModel):
    video_title: str = Field(..., max_length=100)
    thumbnail_hook: str = Field(..., max_length=35)
    description_summary: str
    chapters: list[str]
    scenes: list[SceneItem]
    spoken_sebi_disclaimer: str
